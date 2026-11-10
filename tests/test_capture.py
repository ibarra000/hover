import io
import time


import pytest
from PIL import Image
from pynput.keyboard import Key

from hover import capture

TEXT = 13  # CF_UNICODETEXT
HTML = 49300  # some registered format


class FakeClipboard:
    """Stands in for win32clipboard so tests never touch the real clipboard."""

    def __init__(self, contents):
        self.contents, self.sequence, self.open = dict(contents), 1, False

    def OpenClipboard(self):  # noqa: N802 (pywin32 names)
        self.open = True

    def CloseClipboard(self):  # noqa: N802
        self.open = False

    def EnumClipboardFormats(self, after):  # noqa: N802
        formats = list(self.contents)
        index = formats.index(after) + 1 if after else 0
        return formats[index] if index < len(formats) else 0

    def GetClipboardData(self, fmt):  # noqa: N802
        return self.contents[fmt]

    def SetClipboardData(self, fmt, data):  # noqa: N802
        self.contents[fmt] = data
        self.sequence += 1

    def EmptyClipboard(self):  # noqa: N802
        self.contents = {}
        self.sequence += 1

    def RegisterClipboardFormat(self, name):  # noqa: N802
        return 50000 + sum(map(ord, name))

    def IsClipboardFormatAvailable(self, fmt):  # noqa: N802
        return fmt in self.contents

    def GetClipboardSequenceNumber(self):  # noqa: N802
        return self.sequence


@pytest.fixture
def clipboard(monkeypatch):
    fake = FakeClipboard({TEXT: "the user's clipboard", HTML: b"<b>rich</b>"})
    monkeypatch.setattr(capture, "win32clipboard", fake)
    monkeypatch.setattr(capture, "wait_for_modifiers_released", lambda timeout=0: None)
    return fake


def app_copies(clipboard, selection, on=(Key.insert, "c"), taps=None):
    """Simulate the focused app: copying `selection` when it receives one of the keys in `on`."""
    def tap(key):
        if taps is not None:
            taps.append(key)
        if key in on and selection:
            clipboard.contents = {TEXT: selection}
            clipboard.sequence += 1

    return tap


def test_copy_selection_reads_the_selection_and_restores_every_format(monkeypatch, clipboard):
    taps = []
    monkeypatch.setattr(capture, "tap", app_copies(clipboard, "selected words", taps=taps))

    assert capture.copy_selection(allow_ctrl_c=False) == "selected words"
    assert taps == [Key.insert]  # Ctrl+Insert worked, so Ctrl+C was never sent
    assert clipboard.contents == {TEXT: "the user's clipboard", HTML: b"<b>rich</b>"}


def test_ctrl_c_is_only_sent_when_allowed(monkeypatch, clipboard):
    taps = []
    monkeypatch.setattr(capture, "tap", app_copies(clipboard, "from ctrl c", on=("c",), taps=taps))
    assert capture.copy_selection(allow_ctrl_c=True) == "from ctrl c"
    assert taps == [Key.insert, "c"]

    taps.clear()
    assert capture.copy_selection(allow_ctrl_c=False) == ""
    assert taps == [Key.insert]  # Ctrl+C would interrupt a program in a terminal, so it's opt-in


def test_nothing_selected_leaves_the_clipboard_alone(monkeypatch, clipboard):
    monkeypatch.setattr(capture, "tap", app_copies(clipboard, ""))
    before = clipboard.sequence
    assert capture.copy_selection(allow_ctrl_c=True) == ""
    assert clipboard.sequence == before


def test_copy_refuses_when_files_are_on_the_clipboard_or_another_copy_runs(monkeypatch, clipboard):
    monkeypatch.setattr(capture, "tap", lambda key: pytest.fail("sent keys"))
    clipboard.contents[15] = ("C:\\file.txt",)  # CF_HDROP: copied files can't be restored byte-for-byte
    assert capture.copy_selection(allow_ctrl_c=True) == ""

    del clipboard.contents[15]
    with capture.clipboard_operation:
        assert capture.copy_selection(allow_ctrl_c=True) == ""


def test_selected_text_prefers_ui_automation_and_caps_length(monkeypatch):
    monkeypatch.setattr(capture, "uia_selection", lambda limit: "from uia"[:limit])
    monkeypatch.setattr(capture, "copy_selection", lambda allow_ctrl_c: pytest.fail("copied"))
    assert capture.selected_text(True, 4) == "from"

    monkeypatch.setattr(capture, "uia_selection", lambda limit: "")
    assert capture.selected_text(False, 100) == ""
    monkeypatch.setattr(capture, "copy_selection", lambda allow_ctrl_c: f"ctrl_c={allow_ctrl_c}")
    monkeypatch.setattr(capture, "foreground_process", lambda: "code.exe")
    assert capture.selected_text(True, 100) == "ctrl_c=False"
    assert capture.selected_text(True, 100, ctrl_c_fallback=True) == "ctrl_c=True"
    monkeypatch.setattr(capture, "foreground_process", lambda: "windowsterminal.exe")
    assert capture.selected_text(True, 100, ctrl_c_fallback=True) == "ctrl_c=False"


def test_with_timeout_returns_empty_on_slow_or_failing_tasks():
    assert capture.with_timeout(lambda: "fast", 1) == "fast"
    assert capture.with_timeout(lambda: time.sleep(0.5) or "slow", 0.05) == ""
    assert capture.with_timeout(lambda: 1 / 0, 1) == ""


def test_clipboard_item_prefers_text_then_screenshots(monkeypatch, clipboard):
    monkeypatch.setattr(capture.ImageGrab, "grabclipboard", lambda: Image.new("RGB", (4000, 100)))
    clipboard.contents = {TEXT: "  std::unique_ptr \n  rest "}  # e.g. Excel puts text *and* a picture
    assert capture.clipboard_item(200) == "std::unique_ptr rest"
    assert capture.clipboard_item(5) == "std::"

    clipboard.contents = {}
    assert Image.open(io.BytesIO(capture.clipboard_item(200))).width == capture.MAX_IMAGE_PX
    monkeypatch.setattr(capture.ImageGrab, "grabclipboard", lambda: None)
    assert capture.clipboard_item(200) == ""


def test_paste_text_pastes_then_restores_the_clipboard(monkeypatch, clipboard):
    pasted = []
    monkeypatch.setattr(capture, "tap", lambda key: pasted.append((key, clipboard.contents.get(TEXT))))
    monkeypatch.setattr(capture.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(capture.win32gui, "SetForegroundWindow", lambda window: None)
    monkeypatch.setattr(capture.win32gui, "GetForegroundWindow", lambda: 123)
    monkeypatch.setattr(capture, "foreground_process", lambda: "notepad.exe")

    assert capture.paste_text("the answer\nline two", window=123)
    assert pasted == [("v", "the answer\nline two")]
    assert clipboard.contents == {TEXT: "the user's clipboard", HTML: b"<b>rich</b>"}

    monkeypatch.setattr(capture, "foreground_process", lambda: "windowsterminal.exe")
    capture.paste_text("echo one\necho two", window=123)
    assert pasted[-1] == ("v", "echo one echo two")  # no newline reaches a shell

    assert not capture.paste_text("nope", window=999)  # focus went elsewhere: paste nothing
    assert len(pasted) == 2


def test_png_bytes_shrinks_big_images_without_touching_the_original():
    original = Image.new("RGB", (3000, 1500))
    png = capture.png_bytes(original)
    assert Image.open(io.BytesIO(png)).size == (1280, 640)
    assert original.size == (3000, 1500)


def test_foreground_process_is_a_lowercase_exe_name_or_empty():
    name = capture.foreground_process()
    assert name == "" or (name == name.lower() and name.endswith(".exe"))


def test_cursor_crop_is_a_png_around_the_mouse(monkeypatch):
    monkeypatch.setattr(capture.win32api, "GetCursorPos", lambda: (500, 400))
    boxes = []
    monkeypatch.setattr(capture.ImageGrab, "grab", lambda bbox, all_screens: boxes.append(bbox) or Image.new("RGB", (600, 200)))
    assert Image.open(io.BytesIO(capture.cursor_crop())).size == (600, 200)
    assert boxes == [(200, 300, 800, 500)]


def test_real_uia_calls_return_text_without_raising():
    assert isinstance(capture.with_timeout(lambda: capture.uia_selection(100), 2), str)
    assert isinstance(capture.with_timeout(lambda: capture.uia_text_at_cursor(100), 2), str)
