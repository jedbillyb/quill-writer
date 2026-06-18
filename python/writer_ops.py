"""UNO document operations, executed inside the LibreOffice process.

These run in-process against the live document model, so they are the only code
that touches UNO. They perform the *actual* apply; the preview-then-apply gating
lives in the panel, which calls apply_* only after the user clicks Apply.
"""

import uno
from com.sun.star.beans import PropertyValue  # noqa: F401  (kept for callers)


def get_desktop(ctx):
    smgr = ctx.getServiceManager()
    return smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)


def current_text_doc(ctx):
    """Return the active Writer document model, or None if it isn't a text doc."""
    desktop = get_desktop(ctx)
    comp = desktop.getCurrentComponent()
    if comp is None:
        return None
    if comp.supportsService("com.sun.star.text.TextDocument"):
        return comp
    return None


def get_document_text(doc):
    if doc is None:
        return ""
    return doc.getText().getString()


def get_selection(doc):
    """Return the currently selected text ('' if no/empty selection)."""
    if doc is None:
        return ""
    controller = doc.getCurrentController()
    selection = controller.getSelection()
    if selection is None or not hasattr(selection, "getCount"):
        return ""
    if selection.getCount() == 0:
        return ""
    text_range = selection.getByIndex(0)
    return text_range.getString()


def apply_replace_selection(doc, new_text):
    """Replace the current selection with new_text, as one undoable step."""
    if doc is None:
        raise RuntimeError("No active Writer document.")
    controller = doc.getCurrentController()
    selection = controller.getSelection()
    if selection is None or selection.getCount() == 0:
        raise RuntimeError("Nothing is selected to replace.")
    undo = doc.getUndoManager()
    undo.enterUndoContext("Claude: replace selection")
    try:
        selection.getByIndex(0).setString(new_text)
    finally:
        undo.leaveUndoContext()


def apply_insert_at_cursor(doc, text):
    """Insert text at the view cursor position, as one undoable step."""
    if doc is None:
        raise RuntimeError("No active Writer document.")
    controller = doc.getCurrentController()
    view_cursor = controller.getViewCursor()
    body = doc.getText()
    undo = doc.getUndoManager()
    undo.enterUndoContext("Claude: insert text")
    try:
        body.insertString(view_cursor.getStart(), text, False)
    finally:
        undo.leaveUndoContext()
