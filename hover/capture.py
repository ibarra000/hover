"""Getting the user's text without making them copy it: UI Automation first, a clipboard-preserving copy second.

Approach ported from MIT sources: selection-hook docs/WINDOWS.md (UIA -> Ctrl+Insert -> Ctrl+C, sequence-number
change detection, clipboard backup/restore with retries) and PowerToys ClipboardHelper.cs (release held modifiers
before sending keys).
"""

import contextlib
import io
import os
import threading
import time
from collections.abc import Callable, Iterator
from functools import partial

import pywintypes
import win32api
import win32clipboard
import win32con
import win32gui
import win32process
from PIL import Image, ImageGrab
from pynput.keyboard import Controller, Key

UIA_TIMEOUT = 0.4
MODIFIER_WAIT = 0.5
COPY_WAIT = 0.4
OPEN_RETRIES, OPEN_RETRY_DELAY = 20, 0.05  # clipboard managers and RDP can hold the clipboard for a while
MAX_IMAGE_PX = 1280
HOVER_BOX = (300, 100)  # half-width, half-height of the OCR crop around the cursor
MODIFIER_KEYS = (win32con.VK_CONTROL, win32con.VK_MENU, win32con.VK_SHIFT, win32con.VK_LWIN, win32con.VK_RWIN)
TERMINALS = {"windowsterminal.exe", "openconsole.exe", "conhost.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
             "wezterm-gui.exe", "alacritty.exe", "mintty.exe"}
GENERIC_CONTROLS = {"PaneControl", "WindowControl", "DocumentControl", "GroupControl", "CustomControl"}
HANDLE_FORMATS = {win32con.CF_BITMAP, win32con.CF_METAFILEPICT, win32con.CF_PALETTE, win32con.CF_ENHMETAFILE,
                  win32con.CF_OWNERDISPLAY, win32con.CF_DSPBITMAP, win32con.CF_DSPENHMETAFILE, win32con.CF_HDROP}
PRIVATE_CLIPBOARD_FORMATS = ("ExcludeClipboardContentFromMonitorProcessing", "CanIncludeInClipboardHistory",
                             "CanUploadToCloudClipboard")  # documented opt-outs; a zero DWORD means "don't"
UNRESTORABLE = {win32con.CF_HDROP, win32con.CF_ENHMETAFILE, win32con.CF_METAFILEPICT}  # copied files, vector images
keyboard = Controller()
clipboard_operation = threading.Lock()  # one synthetic copy/paste at a time, or backups capture each other's data


def with_timeout(task: Callable[[], str], seconds: float) -> str:
    """Run task on a worker thread; "" if it fails or takes too long (UIA can hang on a busy app)."""
    result = [""]

    def run() -> None:
        with contextlib.suppress(Exception):  # UIA raises COM errors for apps without accessibility support
            result[0] = task()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    return result[0] or ""


def uia_selection(max_chars: int) -> str:
    import uiautomation as auto

    with auto.UIAutomationInitializerInThread():
        control = auto.GetFocusedControl()
        for _ in range(4):  # the focused element is often a child of the one exposing TextPattern
            if not control:
                return ""
            pattern = control.GetPattern(auto.PatternId.TextPattern)
            if pattern:
                return "".join(r.GetText(max_chars) for r in pattern.GetSelection()).strip()[:max_chars]
            control = control.GetParentControl()
    return ""


def uia_text_at_cursor(max_chars: int) -> str:
    import uiautomation as auto

    with auto.UIAutomationInitializerInThread():
        x, y = auto.GetCursorPos()
        control = auto.ControlFromPoint(x, y)
        if not control:
            return ""
        pattern = control.GetPattern(auto.PatternId.TextPattern)
        if pattern:
            text_range = pattern.RangeFromPoint(x, y)
            text_range.ExpandToEnclosingUnit(auto.TextUnit.Paragraph)
            if text := text_range.GetText(max_chars).strip():
                return text
        name = (control.Name or "").strip()
        return name if len(name) > 1 and control.ControlTypeName not in GENERIC_CONTROLS else ""


def foreground_process() -> str:
    """Executable name of the focused window, lowercase ("" if it can't be read, e.g. an elevated app)."""
    try:
        _, pid = win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow())
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        return os.path.basename(win32process.GetModuleFileNameEx(handle, 0)).lower()
    except pywintypes.error:
        return ""


@contextlib.contextmanager
def open_clipboard() -> Iterator[None]:
    for attempt in range(OPEN_RETRIES):  # another app may be holding the clipboard for a moment
        try:
            win32clipboard.OpenClipboard()
            break
        except pywintypes.error:
            if attempt == OPEN_RETRIES - 1:
                raise
            time.sleep(OPEN_RETRY_DELAY)
    try:
        yield
    finally:
        win32clipboard.CloseClipboard()


def backup_clipboard() -> dict[int, object]:
    """Every clipboard format we can faithfully restore (GDI-handle formats are re-synthesized by Windows)."""
    formats: dict[int, object] = {}
    with open_clipboard():
        fmt = win32clipboard.EnumClipboardFormats(0)
        while fmt:
            if fmt not in HANDLE_FORMATS:
                with contextlib.suppress(pywintypes.error, TypeError):  # private/delay-rendered formats can't be read
                    formats[fmt] = win32clipboard.GetClipboardData(fmt)
            fmt = win32clipboard.EnumClipboardFormats(fmt)
    return formats


def restore_clipboard(formats: dict[int, object]) -> None:
    with open_clipboard():
        win32clipboard.EmptyClipboard()
        for fmt, data in formats.items():
            with contextlib.suppress(pywintypes.error, TypeError):  # skip a format rather than lose the rest
                win32clipboard.SetClipboardData(fmt, data)


def clipboard_text() -> str:
    with open_clipboard():
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
    return ""


def set_clipboard_text(text: str) -> None:
    """Put text on the clipboard briefly, marked so Win+V history and cloud clipboard don't keep a copy."""
    with open_clipboard():
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        for name in PRIVATE_CLIPBOARD_FORMATS:
            win32clipboard.SetClipboardData(win32clipboard.RegisterClipboardFormat(name), b"\0\0\0\0")


def wait_for_modifiers_released(timeout: float = MODIFIER_WAIT) -> None:
    """A held Ctrl+Alt from the hotkey would turn our Ctrl+C into Ctrl+Alt+C (WritingTools #207's "©©")."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(win32api.GetAsyncKeyState(vk) & 0x8000 for vk in MODIFIER_KEYS):
        time.sleep(0.01)


def tap(key: object) -> None:
    with keyboard.pressed(Key.ctrl):
        keyboard.press(key)
        keyboard.release(key)


def clipboard_changed(before: int, wait: float = COPY_WAIT) -> bool:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if win32clipboard.GetClipboardSequenceNumber() != before:
            return True
        time.sleep(0.01)
    return False


def clipboard_has_any(formats: set[int]) -> bool:
    with open_clipboard():
        return any(win32clipboard.IsClipboardFormatAvailable(fmt) for fmt in formats)


def copy_selection(allow_ctrl_c: bool) -> str:
    """Copy the selection with synthetic keys, read it, then put the user's clipboard back exactly as it was.

    Gives up (returns "") rather than risk the user's clipboard: if another copy/paste is in progress, or if the
    clipboard holds copied files or vector images, which can't be restored byte-for-byte.
    """
    if not clipboard_operation.acquire(blocking=False):
        return ""
    try:
        if clipboard_has_any(UNRESTORABLE):
            return ""
        wait_for_modifiers_released()
        backup = backup_clipboard()
        before = win32clipboard.GetClipboardSequenceNumber()
        try:
            tap(Key.insert)  # Ctrl+Insert copies almost everywhere and never sends SIGINT to a terminal
            changed = clipboard_changed(before)
            if not changed and allow_ctrl_c:
                tap("c")
                changed = clipboard_changed(before)
            return clipboard_text().strip() if changed else ""
        finally:  # even if reading fails, or a slow app copied late, the user gets their clipboard back
            if win32clipboard.GetClipboardSequenceNumber() != before:
                restore_clipboard(backup)
    finally:
        clipboard_operation.release()


def selected_text(copy_fallback: bool, max_chars: int, ctrl_c_fallback: bool = False) -> str:
    """The highlighted text in the focused app (capped at max_chars), or "" if nothing is selected."""
    if text := with_timeout(partial(uia_selection, max_chars), UIA_TIMEOUT):
        return text
    if not copy_fallback:
        return ""
    # Ctrl+C is opt-in: in VS Code/JetBrains terminals it would interrupt the running program.
    allow_ctrl_c = ctrl_c_fallback and foreground_process() not in TERMINALS
    return copy_selection(allow_ctrl_c)[:max_chars]


def png_bytes(image: Image.Image) -> bytes:
    image = image.copy()
    image.thumbnail((MAX_IMAGE_PX, MAX_IMAGE_PX))  # big snips stay fast
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def clipboard_item(limit: int) -> str | bytes:
    """The clipboard's text (whitespace collapsed, capped), else a copied screenshot as PNG bytes, else "".

    Text wins because Excel, Word and PowerPoint put both text and a picture on the clipboard when you copy.
    """
    if text := " ".join(clipboard_text().split())[:limit]:
        return text
    image = ImageGrab.grabclipboard()
    return png_bytes(image) if isinstance(image, Image.Image) else ""


def cursor_crop() -> bytes:
    """A screenshot of the area around the mouse, for apps that expose no text to UI Automation."""
    x, y = win32api.GetCursorPos()
    dx, dy = HOVER_BOX
    return png_bytes(ImageGrab.grab(bbox=(x - dx, y - dy, x + dx, y + dy), all_screens=True))


def paste_text(text: str, window: int) -> bool:
    """Replace the selection in `window` with text, then restore the user's clipboard (PowerToys' approach).
    Returns False, pasting nothing, if that window can't be brought back to the front."""
    with contextlib.suppress(pywintypes.error):  # Windows may refuse; checked below
        win32gui.SetForegroundWindow(window)
    time.sleep(0.15)
    if not window or win32gui.GetForegroundWindow() != window:
        return False  # never paste into whatever happens to have focus
    if foreground_process() in TERMINALS:
        text = " ".join(text.split())  # a newline pasted into a shell would run the line
    with clipboard_operation:
        backup = backup_clipboard()
        try:
            set_clipboard_text(text)
            wait_for_modifiers_released()
            tap("v")
            time.sleep(0.3)  # let the target app read the clipboard before we put the old contents back
            return True
        finally:
            restore_clipboard(backup)
