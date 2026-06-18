"""Headless end-to-end test: acts as the panel, drives the real sidecar.

Verifies Claude Code auth works (no API key) and that the writer MCP tools
round-trip through the pipe. Document state is faked in this process.
"""

import json
import subprocess
import sys
import threading

DOC = {
    "full": "The meeting was good. We talked about stuff. Action items unclear.",
    "selection": "We talked about stuff.",
}


def handle_op(op, args):
    if op == "get_document_text":
        return {"text": DOC["full"]}
    if op == "get_selection":
        return {"text": DOC["selection"]}
    if op == "replace_selection":
        # auto-approve in the test, record the proposed text
        DOC["last_replace"] = args.get("text", "")
        return {"applied": True}
    if op == "insert_at_cursor":
        DOC["last_insert"] = args.get("text", "")
        return {"applied": True}
    return {}


def main():
    py = sys.argv[1]
    prompt = sys.argv[2] if len(sys.argv) > 2 else "Make my selected sentence more formal."
    proc = subprocess.Popen(
        [py, "agent_main.py"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
        bufsize=1,
    )

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    assistant = []
    ops_seen = []
    done = threading.Event()

    def reader():
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            kind = msg.get("kind")
            if kind == "ready":
                print("[sidecar ready — Claude Code auth OK]", file=sys.stderr)
                send({"kind": "user", "text": prompt})
            elif kind == "op":
                ops_seen.append(msg["op"])
                print(f"[op] {msg['op']} {msg.get('args', {})}", file=sys.stderr)
                send({"kind": "op_result", "id": msg["id"], "ok": True,
                      "data": handle_op(msg["op"], msg.get("args", {}))})
            elif kind == "assistant":
                assistant.append(msg["text"])
            elif kind == "turn_done":
                send({"kind": "shutdown"})
                done.set()
                break
            elif kind == "error":
                print(f"[ERROR] {msg['message']}", file=sys.stderr)
                done.set()
                break

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    done.wait(timeout=180)
    proc.wait(timeout=10)

    print("\n=== RESULT ===")
    print("tools called:", ops_seen)
    print("assistant:", " ".join(assistant).strip()[:400])
    if "last_replace" in DOC:
        print("proposed replacement:", DOC["last_replace"])
    if "last_insert" in DOC:
        print("proposed insert:", DOC["last_insert"])


if __name__ == "__main__":
    main()
