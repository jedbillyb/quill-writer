"""Manages the Claude agent sidecar subprocess from inside LibreOffice.

GUI-agnostic: it launches the sidecar, reads its newline-JSON stdout on a
background thread, and invokes callbacks. The panel supplies the callbacks and
is responsible for marshalling any UNO / UI work onto the LibreOffice main
thread (callbacks here fire on the reader thread).

Document operations are split:
  * read ops (get_document_text, get_selection) -> ``read_op(op, args) -> dict``
  * edit ops (replace_selection, insert_at_cursor) -> ``request_edit(op, args, done)``
    where the panel calls ``done(applied: bool, error: str | None)`` after the
    user accepts or rejects the previewed change.
"""

import json
import subprocess
import threading

READ_OPS = ("get_document_text", "get_selection")
EDIT_OPS = ("replace_selection", "insert_at_cursor")


class SidecarClient:
    def __init__(
        self,
        python_path,
        script_path,
        *,
        on_ready=None,
        on_assistant=None,
        on_turn_done=None,
        on_error=None,
        read_op=None,
        request_edit=None,
        env=None,
        cwd=None,
    ):
        self.python_path = python_path
        self.script_path = script_path
        self.on_ready = on_ready or (lambda: None)
        self.on_assistant = on_assistant or (lambda text: None)
        self.on_turn_done = on_turn_done or (lambda: None)
        self.on_error = on_error or (lambda msg: None)
        self.read_op = read_op or (lambda op, args: {})
        self.request_edit = request_edit or (lambda op, args, done: done(False))
        self.env = env
        self.cwd = cwd
        self._proc = None
        self._out_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self._proc = subprocess.Popen(
            [self.python_path, self.script_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=self.env,
            cwd=self.cwd,
        )
        threading.Thread(target=self._read_loop, name="sidecar-reader",
                         daemon=True).start()

    def is_running(self):
        return self._proc is not None and self._proc.poll() is None

    def stop(self):
        if not self.is_running():
            return
        try:
            self._write({"kind": "shutdown"})
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()

    # -- sending -----------------------------------------------------------
    def send_user(self, text):
        self._write({"kind": "user", "text": text})

    def _write(self, obj):
        if self._proc is None or self._proc.stdin is None:
            return
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with self._out_lock:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()

    def _send_op_result(self, op_id, ok, data=None, error=None):
        msg = {"kind": "op_result", "id": op_id, "ok": ok}
        if data is not None:
            msg["data"] = data
        if error is not None:
            msg["error"] = error
        self._write(msg)

    # -- receiving ---------------------------------------------------------
    def _read_loop(self):
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._handle(msg)
        finally:
            code = self._proc.poll() if self._proc else None
            if code not in (0, None):
                stderr = ""
                try:
                    stderr = self._proc.stderr.read() or ""
                except Exception:
                    pass
                self.on_error(f"Claude assistant stopped unexpectedly. {stderr.strip()[-300:]}")

    def _handle(self, msg):
        kind = msg.get("kind")
        if kind == "ready":
            self.on_ready()
        elif kind == "assistant":
            self.on_assistant(msg.get("text", ""))
        elif kind == "turn_done":
            self.on_turn_done()
        elif kind == "error":
            self.on_error(msg.get("message", "Unknown error"))
        elif kind == "op":
            self._handle_op(msg)

    def _handle_op(self, msg):
        op = msg.get("op")
        op_id = msg.get("id")
        args = msg.get("args", {})
        if op in READ_OPS:
            try:
                data = self.read_op(op, args)
                self._send_op_result(op_id, True, data=data)
            except Exception as exc:
                self._send_op_result(op_id, False, error=str(exc))
        elif op in EDIT_OPS:
            def done(applied, error=None):
                if error is not None:
                    self._send_op_result(op_id, False, error=error)
                else:
                    self._send_op_result(op_id, True, data={"applied": bool(applied)})
            try:
                self.request_edit(op, args, done)
            except Exception as exc:
                self._send_op_result(op_id, False, error=str(exc))
        else:
            self._send_op_result(op_id, False, error=f"Unknown op: {op}")
