# Claude Assistant for LibreOffice Writer

A LibreOffice Writer extension that adds a native **Claude** sidebar: chat with
Claude, rewrite the selected text, generate or continue writing, and summarise
the document — with every edit previewed for **Apply / Reject** before it
touches your document.

It authenticates off your existing **Claude Code login (subscription)** via the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python) — **no
Anthropic API key required.**

## How it works

```
LibreOffice (Writer)                         agent sidecar (Python 3.10+ venv)
  Claude sidebar panel  ── JSON over a pipe ──  claude-agent-sdk
  owns the document (UNO)                        in-process MCP server "writer"
  applies edits on approval                      drives Claude via Claude Code login
```

The extension exposes the document to Claude as **MCP tools**
(`get_document_text`, `get_selection`, `replace_selection`, `insert_at_cursor`).
Claude calls them; edit tools are gated by the sidebar's preview-then-apply UI.
The agent runs in a separate Python process (the *sidecar*) because LibreOffice's
bundled Python is usually too old for the SDK; the two talk over the subprocess
pipe. Document edits are applied in-process via UNO as single undoable steps.

## Prerequisites

1. **Claude Code CLI**, installed and logged in:
   ```
   claude        # then /login if needed
   ```
2. **LibreOffice** with Python scripting (standard on Linux).
3. A **Python 3.10+ environment with `claude-agent-sdk`** for the sidecar.

## Install

```bash
# 1. Build the extension package
./build.sh

# 2. Create the sidecar venv next to the extension sources, with the SDK
uv venv .venv            # or: python3 -m venv .venv
uv pip install -r requirements.txt   # or: .venv/bin/pip install -r requirements.txt

# 3. Install into LibreOffice
unopkg add claude-writer.oxt
#   (or Tools > Extension Manager > Add…)
```

Open Writer; the **Claude** deck appears in the sidebar (or Tools > "Claude
Assistant (toggle sidebar)").

### Pointing at a different Python

By default the panel launches `<extension>/.venv/bin/python`. Override with:

```bash
export CLAUDE_WRITER_PYTHON=/path/to/python3   # has claude-agent-sdk installed
```

Optional model override: `export CLAUDE_WRITER_MODEL=claude-opus-4-8`.

## Usage

- **Rewrite**: select text, ask e.g. *"make this more formal"* → Claude reads the
  selection and proposes a replacement; **Apply** or **Reject**.
- **Generate / continue**: place the cursor, ask *"continue this paragraph"* →
  Claude proposes an insertion to approve.
- **Summarise**: ask *"summarise this document"* → Claude reads the full text.
- **Chat**: ask anything; Claude answers in the panel without editing.

Applied edits are single undo steps — **Ctrl+Z** reverts cleanly.

## Project layout

| Path | Role |
|------|------|
| `python/claude_panel.py` | UNO sidebar factory + panel UI, thread marshalling |
| `python/sidecar_client.py` | Launches/streams the sidecar, routes document ops |
| `python/writer_ops.py` | UNO document read/edit operations |
| `sidecar/agent_main.py` | Agent loop: MCP server + `ClaudeSDKClient` |
| `sidecar/writer_tools.py` | The `mcp__writer__*` tool definitions |
| `*.xcu`, `description.xml`, `META-INF/manifest.xml` | Extension manifests |

## Testing the backbone (no GUI)

```bash
cd sidecar
../.venv/bin/python fake_panel_test.py ../.venv/bin/python "Make my selection formal."
```

This drives the real sidecar with a faked document and prints the tools Claude
called and the edits it proposed — verifying Claude Code auth and the MCP
round-trip without LibreOffice.

## Status

v0.1 — backbone (sidecar, MCP tools, document ops, preview-then-apply wiring)
is implemented and the agent round-trip is verified headlessly. The sidebar GUI
layer needs interactive testing inside LibreOffice.
