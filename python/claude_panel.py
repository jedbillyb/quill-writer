"""LibreOffice Writer sidebar panel for Claude.

Implements the UNO sidebar factory (``com.sun.star.ui.XUIElementFactory``) and a
native AWT panel: a chat log, an input box, Send, and Apply/Reject buttons that
appear when Claude proposes an edit (preview-then-apply).

The heavy lifting lives in the tested backbone modules:
  * sidecar_client.SidecarClient  — runs the Claude agent sidecar
  * writer_ops                    — UNO document read/edit operations

Sidecar callbacks arrive on a background thread; everything that touches UNO or
the UI is marshalled onto the LibreOffice main thread via theAsyncCallback.
"""

import os
import sys
import threading
import traceback

# LibreOffice loads this component module without its own directory on sys.path,
# so make sibling modules (sidecar_client, writer_ops) importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uno
import unohelper

from com.sun.star.ui import (
    XUIElementFactory, XUIElement, XToolPanel, XSidebarPanel, LayoutSize,
)
from com.sun.star.ui.UIElementType import TOOLPANEL as UET_TOOLPANEL
from com.sun.star.awt import (
    XActionListener, XCallback, XWindowListener, XKeyListener,
)
from com.sun.star.awt.Key import RETURN as KEY_RETURN
from com.sun.star.awt.KeyModifier import SHIFT as MOD_SHIFT

import sidecar_client
import writer_ops

IMPL_NAME = "org.jed.claudewriter.PanelFactory"
RESOURCE_URL = "private:resource/toolpanel/ClaudeWriterFactory/ClaudePanel"

# ----------------------------------------------------------------------------
# Configuration (python path for the sidecar venv, optional model override)
# ----------------------------------------------------------------------------
def _ext_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_sidecar(ctx):
    """Return (python_path, script_path, env) for launching the sidecar.

    The installed extension (in LibreOffice's cache) does not contain the sidecar
    venv, so look for a Python 3.10+ with claude-agent-sdk in these places, in
    order: $CLAUDE_WRITER_PYTHON, ~/.config/claude-writer/python (a file holding
    the path), the extension's own .venv, then the documented source-tree venv.
    """
    ext = _ext_dir()
    script = os.path.join(ext, "sidecar", "agent_main.py")

    candidates = []
    if os.environ.get("CLAUDE_WRITER_PYTHON"):
        candidates.append(os.environ["CLAUDE_WRITER_PYTHON"])
    cfg = os.path.expanduser("~/.config/claude-writer/python")
    if os.path.exists(cfg):
        try:
            with open(cfg) as fh:
                candidates.append(fh.read().strip())
        except OSError:
            pass
    candidates += [
        os.path.join(ext, ".venv", "bin", "python"),
        os.path.expanduser("~/claude-writer/.venv/bin/python"),
        os.path.expanduser("~/.claude-writer/.venv/bin/python"),
    ]
    python_path = next((p for p in candidates if p and os.path.exists(p)), "python3")

    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # force Claude Code subscription auth
    return python_path, script, env


# ----------------------------------------------------------------------------
# Main-thread marshalling
# ----------------------------------------------------------------------------
class _MainThreadCaller(unohelper.Base, XCallback):
    """Runs a queued Python callable on the LibreOffice main thread."""

    def __init__(self, ctx):
        # The /singletons/...theAsyncCallback path resolves to None in an
        # extension context; create the service explicitly instead.
        self._async = ctx.getServiceManager().createInstanceWithContext(
            "com.sun.star.awt.AsyncCallback", ctx
        )
        self._lock = threading.Lock()
        self._queue = []

    def post(self, func):
        with self._lock:
            self._queue.append(func)
        if self._async is not None:
            self._async.addCallback(self, None)
        else:  # degraded fallback: run inline (not main-thread, last resort)
            self.notify(None)

    def notify(self, _data):
        with self._lock:
            func = self._queue.pop(0) if self._queue else None
        if func is not None:
            try:
                func()
            except Exception:
                traceback.print_exc()


# ----------------------------------------------------------------------------
# Panel
# ----------------------------------------------------------------------------
class ClaudePanel(unohelper.Base, XActionListener):
    def __init__(self, ctx, parent_window):
        self.ctx = ctx
        self.smgr = ctx.getServiceManager()
        self.parent = parent_window
        self.main = _MainThreadCaller(ctx)
        self.pending_edit = None  # (done_callback, op, args)
        self.container = None
        self._controls = {}
        self._resize_listener = None
        try:
            self._build_ui()
        except Exception:
            self._build_error_ui(traceback.format_exc())
            return
        try:
            self._start_sidecar()
        except Exception as exc:
            self._set_status(f"Could not start Claude: {exc}")

    # -- UI construction ---------------------------------------------------
    def _create(self, service):
        return self.smgr.createInstanceWithContext(service, self.ctx)

    def _build_ui(self):
        self._toolkit = self._create("com.sun.star.awt.Toolkit")
        container = self._create("com.sun.star.awt.UnoControlContainer")
        container.setModel(self._create("com.sun.star.awt.UnoControlContainerModel"))
        container.createPeer(self._toolkit, self.parent)
        self.container = container

        self._controls["log"] = self._add_edit("log", multiline=True, readonly=True)
        self._controls["input"] = self._add_edit("input", multiline=True, readonly=False)
        self._controls["send"] = self._add_button("send", "Send")
        self._controls["apply"] = self._add_button("apply", "Apply")
        self._controls["reject"] = self._add_button("reject", "Reject")
        self._controls["status"] = self._add_label("status", "Starting Claude…")

        # Enter sends (Shift+Enter inserts a newline).
        self._key_listener = _EnterListener(self)
        self._controls["input"].addKeyListener(self._key_listener)

        # Fill the parent, become visible, and re-lay-out whenever resized.
        psz = self.parent.getPosSize()
        container.setPosSize(0, 0, psz.Width, psz.Height, 15)
        self._resize_listener = _ResizeListener(self)
        self.parent.addWindowListener(self._resize_listener)
        self._show_actions(False)
        self._relayout()
        container.setVisible(True)

    def _build_error_ui(self, message):
        """Last-resort UI so build failures show text instead of a blank panel."""
        try:
            self._toolkit = self._create("com.sun.star.awt.Toolkit")
            container = self._create("com.sun.star.awt.UnoControlContainer")
            container.setModel(self._create("com.sun.star.awt.UnoControlContainerModel"))
            container.createPeer(self._toolkit, self.parent)
            self.container = container
            err = self._add_edit("err", multiline=True, readonly=True)
            err.getModel().setPropertyValue("Text",
                                            "Claude panel failed to load:\n\n" + message)
            psz = self.parent.getPosSize()
            container.setPosSize(0, 0, psz.Width, psz.Height, 15)
            err.setPosSize(4, 4, max(40, psz.Width - 8), max(40, psz.Height - 8), 15)
            container.setVisible(True)
        except Exception:
            traceback.print_exc()

    def _add_control(self, name, ctrl_service, model_service, props):
        model = self._create(model_service)
        for key, value in props.items():
            model.setPropertyValue(key, value)
        ctl = self._create(ctrl_service)
        ctl.setModel(model)
        self.container.addControl(name, ctl)
        ctl.createPeer(self._toolkit, self.container.getPeer())
        return ctl

    def _add_edit(self, name, multiline, readonly):
        return self._add_control(
            name, "com.sun.star.awt.UnoControlEdit",
            "com.sun.star.awt.UnoControlEditModel",
            {"MultiLine": multiline, "ReadOnly": readonly,
             "VScroll": multiline, "AutoVScroll": multiline})

    def _add_button(self, name, label):
        ctl = self._add_control(
            name, "com.sun.star.awt.UnoControlButton",
            "com.sun.star.awt.UnoControlButtonModel", {"Label": label})
        ctl.setActionCommand(name)
        ctl.addActionListener(self)
        return ctl

    def _add_label(self, name, text):
        return self._add_control(
            name, "com.sun.star.awt.UnoControlFixedText",
            "com.sun.star.awt.UnoControlFixedTextModel", {"Label": text})

    def _relayout(self):
        if self.container is None or "status" not in self._controls:
            return
        size = self.container.getPosSize()
        w, h = size.Width, size.Height
        if w <= 0 or h <= 0:
            size = self.parent.getPosSize()
            w, h = size.Width, size.Height
        pad = 6
        x = pad
        cw = max(40, w - 2 * pad)
        status_h = 18
        btn_h = 28
        input_h = 64
        actions_visible = self.pending_edit is not None
        actions_h = btn_h + pad if actions_visible else 0

        y = pad
        self._controls["status"].setPosSize(x, y, cw, status_h, 15)
        y += status_h + pad
        log_h = max(60, h - (status_h + input_h + btn_h + actions_h + 5 * pad))
        self._controls["log"].setPosSize(x, y, cw, log_h, 15)
        y += log_h + pad
        if actions_visible:
            bw = (cw - pad) // 2
            self._controls["apply"].setPosSize(x, y, bw, btn_h, 15)
            self._controls["reject"].setPosSize(x + bw + pad, y, bw, btn_h, 15)
            y += actions_h
        self._controls["input"].setPosSize(x, y, cw, input_h, 15)
        y += input_h + pad
        self._controls["send"].setPosSize(x, y, cw, btn_h, 15)

    def _show_actions(self, visible):
        # While an edit is pending, Apply/Reject appear and the bottom button
        # becomes "Improve" (sends the input box as revision feedback).
        for n in ("apply", "reject"):
            self._controls[n].setVisible(visible)
        self._controls["send"].getModel().setPropertyValue(
            "Label", "Improve" if visible else "Send")

    # -- sidecar -----------------------------------------------------------
    def _start_sidecar(self):
        python_path, script, env = _resolve_sidecar(self.ctx)
        self.client = sidecar_client.SidecarClient(
            python_path, script, env=env, cwd=_ext_dir(),
            on_ready=lambda: self.main.post(self._on_ready),
            on_assistant=lambda t: self.main.post(lambda: self._append_log("Claude", t)),
            on_turn_done=lambda: self.main.post(lambda: self._set_status("Ready")),
            on_error=lambda m: self.main.post(lambda: self._on_error(m)),
            read_op=self._read_op,            # runs on reader thread; UNO read is tolerant
            request_edit=self._request_edit,  # marshalled to main thread below
        )
        try:
            self.client.start()
        except Exception as exc:
            self._set_status(f"Could not start Claude: {exc}")

    def _on_ready(self):
        self._set_status("Ready")

    def _on_error(self, msg):
        self._set_status("Error")
        self._append_log("System", msg)

    # document ops (read ops kept simple; edits marshalled & user-gated)
    def _read_op(self, op, args):
        doc = writer_ops.current_text_doc(self.ctx)
        if op == "get_document_text":
            return {"text": writer_ops.get_document_text(doc)}
        if op == "get_selection":
            return {"text": writer_ops.get_selection(doc)}
        return {}

    _EDIT_LABEL = {
        "replace_selection": "rewrite the selected text",
        "replace_document": "rewrite the whole document",
        "insert_at_cursor": "insert text",
    }

    def _request_edit(self, op, args, done):
        # Called on the reader thread -> hop to the main thread to write the
        # preview into the document (highlighted) and show Apply/Reject.
        def show():
            try:
                doc = writer_ops.current_text_doc(self.ctx)
                info = writer_ops.start_preview(doc, op, args.get("text", ""))
            except Exception as exc:
                done(False, error=str(exc))
                self._set_status(str(exc))
                return
            self.pending_edit = (done, info)
            self._set_status("Claude wants to "
                             + self._EDIT_LABEL.get(op, "edit")
                             + " (highlighted). Apply, Reject, or type feedback "
                             + "and press Improve/Enter.")
            self._show_actions(True)
            self._relayout()
        self.main.post(show)

    # -- UI events ---------------------------------------------------------
    def actionPerformed(self, ev):
        cmd = ev.ActionCommand
        if cmd == "send":
            self._on_submit()
        elif cmd == "apply":
            self._resolve_edit(True)
        elif cmd == "reject":
            self._resolve_edit(False)

    def _on_submit(self):
        # The bottom button / Enter means Improve while an edit is pending,
        # otherwise a normal chat message.
        if self.pending_edit is not None:
            self._improve_edit()
        else:
            self._do_send()

    def _do_send(self):
        text = self._controls["input"].getText().strip()
        if not text or not self.client.is_running():
            return
        self._controls["input"].setText("")
        self._append_log("You", text)
        self._set_status("Claude is working…")
        self.client.send_user(text)

    def _resolve_edit(self, apply_it):
        if self.pending_edit is None:
            return
        done, info = self.pending_edit
        self.pending_edit = None
        self._show_actions(False)
        self._relayout()
        doc = writer_ops.current_text_doc(self.ctx)
        try:
            if apply_it:
                writer_ops.accept_preview(doc, info["handle"])
                self._set_status("Edit applied")
            else:
                writer_ops.reject_preview(doc, info["handle"],
                                          info["original"], info["kind"])
                self._set_status("Edit rejected")
            done(apply_it)
        except Exception as exc:
            done(False, error=str(exc))
            self._set_status(f"Edit failed: {exc}")

    def _improve_edit(self):
        """Revert the current preview and ask Claude to revise it with feedback."""
        if self.pending_edit is None:
            return
        feedback = self._controls["input"].getText().strip()
        if not feedback:
            self._set_status("Type what to change, then click Improve.")
            return
        done, info = self.pending_edit
        self.pending_edit = None
        self._show_actions(False)
        self._relayout()
        self._controls["input"].setText("")
        self._append_log("You (improve)", feedback)
        try:
            doc = writer_ops.current_text_doc(self.ctx)
            writer_ops.reject_preview(doc, info["handle"],
                                      info["original"], info["kind"])
            self._set_status("Claude is revising…")
            done(False, feedback=feedback)
        except Exception as exc:
            done(False, error=str(exc))
            self._set_status(f"Could not revise: {exc}")

    # -- helpers -----------------------------------------------------------
    def _append_log(self, who, text):
        log = self._controls["log"]
        existing = log.getText()
        sep = "\n\n" if existing else ""
        log.setText(f"{existing}{sep}{who}: {text}")

    def _set_status(self, text):
        self._controls["status"].setText(text)

    def disposing(self, _ev):
        pass

    def dispose(self):
        try:
            self.client.stop()
        except Exception:
            pass

    def get_window(self):
        return self.container


class _EnterListener(unohelper.Base, XKeyListener):
    """Enter submits (Send or Improve); Shift+Enter inserts a newline."""

    def __init__(self, panel):
        self._panel = panel

    def keyPressed(self, ev):
        pass

    def keyReleased(self, ev):
        if ev.KeyCode == KEY_RETURN and not (ev.Modifiers & MOD_SHIFT):
            try:
                self._panel._on_submit()
            except Exception:
                traceback.print_exc()

    def disposing(self, ev):
        pass


class _ResizeListener(unohelper.Base, XWindowListener):
    """Keeps the panel container filling the parent and re-lays-out on resize."""

    def __init__(self, panel):
        self._panel = panel

    def windowResized(self, ev):
        try:
            self._panel.container.setPosSize(0, 0, ev.Width, ev.Height, 15)
            self._panel._relayout()
        except Exception:
            traceback.print_exc()

    def windowMoved(self, ev):
        pass

    def windowShown(self, ev):
        pass

    def windowHidden(self, ev):
        pass

    def disposing(self, ev):
        pass


# ----------------------------------------------------------------------------
# UI element wrappers
# ----------------------------------------------------------------------------
class PanelUIElement(unohelper.Base, XUIElement, XToolPanel, XSidebarPanel):
    def __init__(self, ctx, frame, panel):
        self.ctx = ctx
        self._frame = frame
        self._panel = panel

    # XUIElement
    def getFrame(self):
        return self._frame

    def getResourceURL(self):
        return RESOURCE_URL

    def getType(self):
        return UET_TOOLPANEL

    def getRealInterface(self):
        return self  # also implements XToolPanel

    # XToolPanel
    def createAccessible(self, _parent):
        return self._panel.get_window().getAccessibleContext()

    def getWindow(self):
        return self._panel.get_window()

    # XSidebarPanel
    def getHeightForWidth(self, _width):
        return LayoutSize(0, -1, 0)  # flexible height

    def getMinimalWidth(self):
        return 320


class PanelFactory(unohelper.Base, XUIElementFactory):
    def __init__(self, ctx):
        self.ctx = ctx

    def createUIElement(self, resource_url, args):
        frame = None
        parent = None
        for arg in args:
            if arg.Name == "Frame":
                frame = arg.Value
            elif arg.Name == "ParentWindow":
                parent = arg.Value
        panel = ClaudePanel(self.ctx, parent)
        return PanelUIElement(self.ctx, frame, panel)


# ----------------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------------
g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    PanelFactory, IMPL_NAME, ("com.sun.star.ui.UIElementFactory",)
)
