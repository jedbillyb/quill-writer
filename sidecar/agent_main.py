"""Claude agent sidecar for the LibreOffice Writer extension.

Runs in its own system/venv Python (3.10+) with ``claude-agent-sdk`` installed.
Authenticates off the local Claude Code CLI login (the user's subscription) — no
ANTHROPIC_API_KEY required.

Protocol (newline-delimited JSON over stdin/stdout):

  panel -> sidecar (stdin)
    {"kind":"user","text":"..."}             a chat message / instruction
    {"kind":"op_result","id":N,"ok":true,"data":{...}}   reply to a document op
    {"kind":"shutdown"}

  sidecar -> panel (stdout)
    {"kind":"ready"}                          connected and ready
    {"kind":"assistant","text":"..."}         a chunk of assistant text
    {"kind":"op","id":N,"op":"...","args":{}} a document operation request
    {"kind":"turn_done"}                       assistant finished the turn
    {"kind":"error","message":"..."}

Diagnostics go to stderr only, never stdout (stdout is the protocol channel).
"""

import asyncio
import json
import os
import sys
import threading

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
)

import writer_tools

SYSTEM_PROMPT = """You are Claude, an AI writing assistant embedded directly in \
LibreOffice Writer. You help the user write, edit, and understand the document \
they are working on.

You have tools to read and edit the live document:
- To rewrite or fix the user's selected text: call get_selection, then \
replace_selection with the improved text.
- To generate new text or continue writing: call insert_at_cursor.
- To summarise or answer questions about the whole document: call \
get_document_text.

Every edit you propose is shown to the user for approval before it changes their \
document, so make your best single attempt rather than asking permission in chat. \
Keep your chat replies short and conversational; put the actual writing into the \
edit tools, not into the chat. If the user just wants to talk or asks a question, \
answer directly without editing."""


class Bridge:
    """Multiplexes the stdin/stdout pipe between the asyncio agent loop and the
    panel: delivers user messages to the loop and resolves document-op futures."""

    def __init__(self, loop):
        self.loop = loop
        self.user_queue: asyncio.Queue = asyncio.Queue()
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._out_lock = threading.Lock()

    def send(self, obj):
        line = json.dumps(obj, ensure_ascii=False)
        with self._out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    async def call_op(self, op, args):
        with self._id_lock:
            self._next_id += 1
            op_id = self._next_id
        fut = self.loop.create_future()
        self._pending[op_id] = fut
        self.send({"kind": "op", "id": op_id, "op": op, "args": args})
        try:
            return await fut
        finally:
            self._pending.pop(op_id, None)

    def _dispatch(self, msg):
        kind = msg.get("kind")
        if kind == "op_result":
            fut = self._pending.get(msg.get("id"))
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif kind == "user":
            self.user_queue.put_nowait(msg)
        elif kind == "shutdown":
            self.user_queue.put_nowait({"kind": "shutdown"})

    def start_reader(self):
        def reader():
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.loop.call_soon_threadsafe(self._dispatch, msg)
            # stdin closed -> ask the loop to shut down
            self.loop.call_soon_threadsafe(
                self.user_queue.put_nowait, {"kind": "shutdown"}
            )

        threading.Thread(target=reader, name="stdin-reader", daemon=True).start()


async def run_turn(client, bridge, text):
    await client.query(text)
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text:
                    bridge.send({"kind": "assistant", "text": block.text})
        if isinstance(message, ResultMessage):
            break
    bridge.send({"kind": "turn_done"})


async def main():
    loop = asyncio.get_running_loop()
    bridge = Bridge(loop)
    bridge.start_reader()

    server = create_sdk_mcp_server("writer", "1.0.0", writer_tools.build_tools(bridge))
    options = ClaudeAgentOptions(
        mcp_servers={"writer": server},
        allowed_tools=writer_tools.tool_names("writer"),
        permission_mode="bypassPermissions",  # our own tools; user gates edits in the UI
        system_prompt=SYSTEM_PROMPT,
        model=os.environ.get("CLAUDE_WRITER_MODEL") or None,
        setting_sources=[],  # do not load filesystem tools / project CLAUDE.md
    )

    client = ClaudeSDKClient(options=options)
    try:
        await client.connect()
    except Exception as exc:  # surface auth / CLI errors to the panel
        bridge.send({"kind": "error", "message": f"Could not start Claude: {exc}"})
        return

    bridge.send({"kind": "ready"})

    while True:
        msg = await bridge.user_queue.get()
        if msg.get("kind") == "shutdown":
            break
        try:
            await run_turn(client, bridge, msg.get("text", ""))
        except Exception as exc:
            bridge.send({"kind": "error", "message": str(exc)})

    try:
        await client.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
