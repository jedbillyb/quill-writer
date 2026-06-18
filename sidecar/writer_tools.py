"""In-process MCP tools exposed to Claude.

These tools are deliberately thin: they do NOT touch the LibreOffice document
directly (they run in a separate process from the UNO document model). Each tool
serialises a document operation request over the pipe to the in-LibreOffice
panel via ``bridge.call_op`` and returns the panel's reply to Claude.

Edit tools (replace_selection / insert_at_cursor) are gated by the panel's
preview-then-apply UI: the panel only resolves the op once the user clicks
Apply or Reject, so the tool result faithfully tells Claude what happened.
"""

from claude_agent_sdk import tool


def build_tools(bridge):
    """Return the list of SDK MCP tools, closed over the given Bridge."""

    @tool(
        "get_document_text",
        "Get the full plain text of the current LibreOffice Writer document. "
        "Use this to summarise or answer questions about the whole document.",
        {},
    )
    async def get_document_text(args):
        reply = await bridge.call_op("get_document_text", {})
        text = (reply.get("data") or {}).get("text", "")
        return {"content": [{"type": "text", "text": text or "[document is empty]"}]}

    @tool(
        "get_selection",
        "Get the text the user currently has selected in the document. "
        "Returns a marker if nothing is selected. Use before rewriting a selection.",
        {},
    )
    async def get_selection(args):
        reply = await bridge.call_op("get_selection", {})
        text = (reply.get("data") or {}).get("text", "")
        return {"content": [{"type": "text", "text": text or "[no text selected]"}]}

    @tool(
        "replace_selection",
        "Replace the user's currently selected text with new text. The change is "
        "shown to the user for approval before it is applied; the result tells you "
        "whether the user accepted it.",
        {"text": str},
    )
    async def replace_selection(args):
        reply = await bridge.call_op("replace_selection", {"text": args.get("text", "")})
        return {"content": [{"type": "text", "text": _edit_result(reply)}]}

    @tool(
        "insert_at_cursor",
        "Insert new text at the current cursor position (use to generate or continue "
        "writing). The change is shown to the user for approval before it is applied.",
        {"text": str},
    )
    async def insert_at_cursor(args):
        reply = await bridge.call_op("insert_at_cursor", {"text": args.get("text", "")})
        return {"content": [{"type": "text", "text": _edit_result(reply)}]}

    return [get_document_text, get_selection, replace_selection, insert_at_cursor]


def tool_names(server="writer"):
    return [
        f"mcp__{server}__get_document_text",
        f"mcp__{server}__get_selection",
        f"mcp__{server}__replace_selection",
        f"mcp__{server}__insert_at_cursor",
    ]


def _edit_result(reply):
    if not reply.get("ok"):
        return f"The edit could not be applied: {reply.get('error', 'unknown error')}."
    data = reply.get("data") or {}
    if data.get("applied"):
        return "The user approved and the edit was applied to the document."
    return "The user rejected the edit; the document was left unchanged."
