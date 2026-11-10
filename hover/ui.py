"""The card: a borderless, always-on-top popup at the cursor that streams answers and takes follow-ups."""

import contextlib
import ctypes
import re
import tkinter as tk
import webbrowser
from collections.abc import Callable
from functools import partial
from urllib.parse import urlparse

import win32api
from pynput.keyboard import Controller, Key

BG, FG, MUTED, ACCENT, LOCAL = "#202020", "#ffffff", "#9d9d9d", "#60cdff", "#6ccb5f"  # Windows 11 dark flyout
FIELD, CODE_BG, EDGE = "#2d2d2d", "#2b2b2b", "#3a3a3a"
TITLE_FONT = ("Segoe UI Variable Display Semibold", 13)
BODY_FONT, SMALL_FONT = ("Segoe UI Variable Text", 10), ("Segoe UI Variable Text", 9)
BOLD_FONT, CODE_FONT = ("Segoe UI Variable Text Semibold", 10), ("Cascadia Mono", 9)
ICON_FONT, MIC = ("Segoe Fluent Icons", 11), ""  # the Windows 11 microphone glyph
DWM_ATTRIBUTES = ((33, 2), (34, 0x3A3A3A))  # rounded corners; grey border (COLORREF)
WM_NCLBUTTONDOWN, HTCAPTION = 0x00A1, 2
CURSOR_OFFSET = 16
WIDTH_CHARS, MAX_LINES, MAX_TITLE_CHARS = 56, 18, 70
LISTEN_MS = 10_000  # the card ignores focus loss this long after the mic opens Windows voice typing
PIN_SWITCH_MS = 500  # ...and this long while it switches between card and window
LOADING = "Looking up…"
MARKDOWN = re.compile(r"```[^\n]*\n?(.*?)```|`([^`\n]+)`|\*\*(.+?)\*\*", re.S)
BULLET = re.compile(r"(?m)^(\s*)[-*] ")


def segments(text: str) -> list[tuple[str, str]]:
    """Light markdown as (text, tag) runs, where tag is "code", "bold", or "" for plain text."""
    text = BULLET.sub(r"\1• ", text)
    runs, position = [], 0
    for match in MARKDOWN.finditer(text):
        block, code, bold = match.groups()
        runs.append((text[position:match.start()], ""))
        runs.append((bold, "bold") if bold is not None else (block if block is not None else code, "code"))
        position = match.end()
    runs.append((text[position:], ""))
    return [(run, tag) for run, tag in runs if run]


def fit(x: int, y: int, width: int, height: int, area: tuple[int, int, int, int]) -> tuple[int, int]:
    """Position a card near (x, y) inside a monitor's work area, flipping to the other side of the cursor at edges."""
    left, top, right, bottom = area
    if x + width > right:
        x -= width + 2 * CURSOR_OFFSET
    if y + height > bottom:
        y -= height + 2 * CURSOR_OFFSET
    return max(left, min(x, right - width)), max(top, min(y, bottom - height))


def work_area(x: int, y: int) -> tuple[int, int, int, int]:
    """The usable area (minus the taskbar) of the monitor containing (x, y)."""
    return win32api.GetMonitorInfo(win32api.MonitorFromPoint((x, y), 2))["Work"]  # 2 = MONITOR_DEFAULTTONEAREST


def footer_text(link: str, mode: str, turns: int, count: int) -> str:
    """Privacy badge first: "web · host ↗" when a search was used, "local" when nothing left the PC."""
    parts = [f"web · {urlparse(link).hostname} ↗" if link else "local", mode]
    parts += [f"{turns} in context"] if turns > 1 else []
    parts += [f"looked up {count}×"] if count > 1 else []
    return " · ".join(parts)


def start_dictation() -> None:
    """Win+H: Windows' built-in voice typing, which types into whatever has focus."""
    keyboard = Controller()
    with keyboard.pressed(Key.cmd):
        keyboard.tap("h")


class Popup:
    """Esc, a click outside, or the timer hides it unless pinned. Enter in the box asks a follow-up and Shift+Tab
    switches mode. Drag it by any empty area; pinned, it becomes a normal window that Windows Snap can arrange."""

    def __init__(self, root: tk.Tk, on_follow_up: Callable[[str], None], on_replace: Callable[[str], None],
                 on_hide: Callable[[], None], on_cycle: Callable[[], None]) -> None:
        self.root, self.on_follow_up, self.on_replace, self.on_hide = root, on_follow_up, on_replace, on_hide
        self.request, self.answer, self.url, self.pinned, self.replaceable = 0, "", "", False, False
        self.hide_ms, self.anchor, self.moved, self.holding_open = 30_000, (0, 0), False, False
        self._timer: str | None = None
        self._hold_timer: str | None = None
        root.withdraw()
        root.title("Hover")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=BG, padx=18, pady=14)
        root.protocol("WM_DELETE_WINDOW", self.hide)  # a pinned card's X hides it; the app keeps running
        self.title = tk.Label(root, font=TITLE_FONT, fg=FG, bg=BG, anchor="w", justify="left", wraplength="4.6i")
        self.title.pack(fill="x")
        footer = tk.Frame(root, bg=BG)
        footer.pack(side="bottom", fill="x", pady=(8, 0))
        field = tk.Frame(root, bg=FIELD, highlightthickness=1, highlightbackground=EDGE)
        field.pack(side="bottom", fill="x", pady=(6, 0))
        self.body = tk.Text(root, font=BODY_FONT, fg=FG, bg=BG, width=WIDTH_CHARS, height=1, wrap="word", bd=0,
                            highlightthickness=0, padx=0, pady=6, cursor="arrow", selectbackground=EDGE,
                            spacing2=2, state="disabled")
        self.body.tag_configure("bold", font=BOLD_FONT)
        self.body.tag_configure("code", font=CODE_FONT, background=CODE_BG)
        self.body.tag_configure("muted", foreground=MUTED)
        self.body.pack(fill="both", expand=True)  # fills a pinned window the user resized or snapped
        self.entry = tk.Entry(field, font=BODY_FONT, fg=FG, bg=FIELD, insertbackground=FG, relief="flat",
                              highlightthickness=0)
        self.entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(6, 0))
        self.entry.bind("<Return>", self._follow_up)
        self.entry.bind("<Key>", lambda _: self._restart_timer(), add="+")  # don't hide while the user types
        self._button(field, MIC, self.dictate, font=ICON_FONT, bg=FIELD)
        self.status = tk.Label(footer, font=SMALL_FONT, fg=MUTED, bg=BG, cursor="hand2")
        self.status.pack(side="left")
        self.status.bind("<Button-1>", lambda _: self.url and webbrowser.open(self.url))
        self.pin_button = self._button(footer, "Pin", self.toggle_pin)
        for label, action in (("Replace", self.replace), ("Copy", self.copy)):
            self._button(footer, label, action)
        for keys, action in (("<Control-c>", partial(self.copy, from_keyboard=True)), ("<Control-r>", self.replace),
                             ("<Control-m>", self.dictate), ("<Control-p>", self.toggle_pin), ("<Escape>", self.hide)):
            root.bind(keys, lambda _, act=action: act())
        root.bind("<Shift-Tab>", lambda _: on_cycle() or "break")  # "break": skip Tk's focus traversal
        root.bind("<FocusOut>", lambda _: root.after(50, self._hide_if_unfocused))
        self._handles = (root, self.title, footer)  # empty areas of the card that drag it
        root.bind("<ButtonPress-1>", self._drag)

    def _button(self, parent: tk.Frame, label: str, action: Callable[[], None], font: tuple = SMALL_FONT,
                bg: str = BG) -> tk.Label:
        button = tk.Label(parent, text=label, font=font, fg=MUTED, bg=bg, cursor="hand2", padx=6)
        button.pack(side="right")
        button.bind("<Button-1>", lambda _: action())
        button.bind("<Enter>", lambda _: button.configure(fg=ACCENT))
        button.bind("<Leave>", lambda _: button.configure(fg=MUTED))
        return button

    def _write(self, runs: list[tuple[str, str]], replace: bool) -> None:
        self.body.configure(state="normal")
        if replace:
            self.body.delete("1.0", "end")
        for text, tag in runs:
            self.body.insert("end", text, tag)
        self.body.configure(state="disabled")
        self.body.configure(height=min(MAX_LINES, max(1, self.body.count("1.0", "end", "displaylines", return_ints=True))))
        self._place()

    def show(self, title: str, request: int, keep_position: bool = False, status: str = "") -> None:
        self.request, self.answer, self.url, self.replaceable = request, "", "", False
        self.title["text"] = title if len(title) <= MAX_TITLE_CHARS else title[:MAX_TITLE_CHARS] + "…"
        self.status.configure(text=status, fg=MUTED)
        if not keep_position:
            x, y = self.root.winfo_pointerxy()
            self.anchor, self.moved = (x + CURSOR_OFFSET, y + CURSOR_OFFSET), False
        self.root.deiconify()  # map first: a withdrawn Text measures every word on its own line
        self.root.lift()  # a pinned window may be behind others
        self._write([(LOADING, "muted")], replace=True)
        self._round_corners()
        self.root.focus_force()
        self.entry.focus_set()
        self._restart_timer()

    def append(self, request: int, chunk: str) -> None:
        """Stream a piece of the answer in (ignored if a newer lookup has replaced this one)."""
        if request != self.request or not chunk:
            return
        self._write([(chunk, "")], replace=not self.answer)
        self.answer += chunk
        self._restart_timer()  # a long answer shouldn't vanish mid-stream

    def finish(self, request: int, text: str, footer: str, link: str) -> None:
        """Re-render the complete answer with markdown and show the footer."""
        if request != self.request:
            return
        self.answer, self.url = text, link
        self.replaceable = bool(text) and footer != "error"  # never paste error text or a half-streamed answer
        self._write(segments(text) or [("(empty answer)", "muted")], replace=True)
        self.status.configure(text=footer, fg=ACCENT if link else LOCAL if footer.startswith("local") else MUTED)
        self._restart_timer()

    def notice(self, title: str, text: str) -> None:
        self.show(title, self.request + 1)
        self.finish(self.request, text, "", "")
        self.replaceable = False  # never paste a notice into the user's document

    def copy(self, from_keyboard: bool = False) -> None:
        focused = self.root.focus_get()
        if from_keyboard and ((focused is self.entry and self.entry.selection_present())
                              or (focused is self.body and self.body.tag_ranges("sel"))):
            return  # Ctrl+C on a selection inside the card: let Tk copy just that
        self.root.clipboard_clear()
        self.root.clipboard_append(self.answer)
        self.status.configure(text="copied")

    def replace(self) -> None:
        if self.replaceable:
            self.hide()
            self.on_replace(self.answer)

    def dictate(self) -> None:
        """Speak a follow-up: Windows voice typing fills the box, Enter sends it."""
        self.entry.focus_set()
        self.hold_open(LISTEN_MS)  # the voice typing panel may take focus for a moment
        start_dictation()
        self._restart_timer()

    def toggle_pin(self) -> None:
        """Pinned: a normal window (title bar, Snap, Snap Layouts, resizing). Unpinned: the borderless card."""
        self.pinned = not self.pinned
        self.pin_button.configure(text="Unpin" if self.pinned else "Pin")
        self.hold_open(PIN_SWITCH_MS)  # withdrawing the window below loses focus for a moment
        x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
        self.root.withdraw()  # Windows applies a frame change when the window is mapped again
        self.root.overrideredirect(not self.pinned)
        self.root.attributes("-topmost", not self.pinned)
        if not self.pinned:
            self.root.geometry("")  # forget a size the user dragged or snapped: fit the answer again
            self.root.geometry(f"+{x}+{y}")
        self.moved = True  # stay where it is either way
        self.root.deiconify()
        self._round_corners()
        self.root.focus_force()
        self._restart_timer()

    def hold_open(self, milliseconds: int) -> None:
        """Don't hide when focus leaves the card for the next `milliseconds`."""
        if self._hold_timer:
            self.root.after_cancel(self._hold_timer)
        self.holding_open = True
        self._hold_timer = self.root.after(milliseconds, self._stop_holding_open)

    def _stop_holding_open(self) -> None:
        self.holding_open, self._hold_timer = False, None

    def hide(self) -> None:
        self.root.withdraw()
        self.on_hide()

    def _follow_up(self, _: tk.Event) -> str:
        if question := self.entry.get().strip():
            self.entry.delete(0, "end")
            self.on_follow_up(question)
        return "break"

    def _hide_if_unfocused(self) -> None:
        if not (self.pinned or self.holding_open) and self.root.focus_get() is None and self.root.state() == "normal":
            self.hide()

    def _drag(self, event: tk.Event) -> None:
        """Hand the drag to Windows as if the title bar were grabbed: native moving, and Snap when pinned."""
        if event.widget not in self._handles:
            return
        self.moved = True  # stay where the user drops it while the answer keeps streaming
        ctypes.windll.user32.ReleaseCapture()
        ctypes.windll.user32.SendMessageW(int(self.root.wm_frame(), 16), WM_NCLBUTTONDOWN, HTCAPTION, 0)

    def _round_corners(self) -> None:
        """Windows 11 rounded corners and border via DWM (ignored on Windows 10)."""
        with contextlib.suppress(AttributeError, OSError, ValueError):  # not a real window yet, or no DWM
            hwnd = int(self.root.wm_frame(), 16)
            for attribute, value in DWM_ATTRIBUTES:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(ctypes.c_int(value)), 4)

    def _place(self) -> None:
        if self.pinned or self.moved:
            return  # the user chose where it goes
        self.root.update_idletasks()
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x, y = fit(*self.anchor, width, height, work_area(*self.anchor))
        self.root.geometry(f"+{x}+{y}")

    def _restart_timer(self) -> None:
        if self._timer:
            self.root.after_cancel(self._timer)
        self._timer = None if self.pinned else self.root.after(self.hide_ms, self.hide)
