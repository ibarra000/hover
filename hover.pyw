"""Hover: copy a word or code symbol, press the hotkey, read a succinct definition."""

import ctypes
import re
import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable
from urllib.parse import urlparse

import ollama
import pystray
from ddgs import DDGS
from ddgs.exceptions import DDGSException
from PIL import Image, ImageDraw
from pynput import keyboard

HOTKEY = "<ctrl>+<alt>+h"
MODEL = "qwen3:4b-instruct-2507-q4_K_M"
OPTIONS = {"temperature": 0.3, "num_predict": 160, "num_ctx": 4096}
SEARCH_RESULTS = 3
MAX_TERM_CHARS = 200
WEB_URL = re.compile(r'https?://[^\x00-\x20"\x7f]+')  # no quotes/whitespace that could break the shell handler
HIDE_AFTER_MS = 20_000
CURSOR_OFFSET = 16
WRAP = "4i"  # inches, so the card scales with display DPI
LOADING = "Looking up…"
NO_WEB_NOTE = "no web results · model only"
EMPTY_CLIPBOARD = "Clipboard has no text. Copy something, then press the hotkey."
BG, FG, MUTED, ACCENT = "#202020", "#ffffff", "#9d9d9d", "#60cdff"  # Windows 11 dark flyout
TITLE_FONT, BODY_FONT = ("Segoe UI Variable Display Semibold", 13), ("Segoe UI Variable Text", 10)
DWM_ATTRIBUTES = ((33, 2), (34, 0x3A3A3A))  # rounded corners; grey border (COLORREF)
SYSTEM_PROMPT = (
    "You are a terse dictionary and documentation lookup. The user copied a term: an English word "
    "or phrase, or a programming identifier, command, or type. Reply in plain text with no markdown "
    "and no preamble, in at most 3 short lines. Line 1 says what it is: part of speech, or language "
    "or library and kind (function, class, type, keyword, command). Then give a succinct definition; "
    "for code include the signature or usage. Base the answer on the search results when they are "
    "relevant and never invent APIs."
)


def clean(text: str) -> str:
    """Collapse whitespace and cap the length so a stray paragraph stays a lookup."""
    return " ".join(text.split())[:MAX_TERM_CHARS]


def explain(term: str) -> tuple[str, str]:
    """Ground the term in web snippets, then have the model write a succinct definition."""
    try:
        results = DDGS().text(term, max_results=SEARCH_RESULTS)
    except DDGSException:
        results = []  # offline or no hits: the popup marks the answer as model-only
    snippets = "\n".join(f"- {r['title']}: {r['body']}" for r in results) or "(no search results)"
    reply = ollama.chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Term: {term}\n\nSearch results:\n{snippets}"},
        ],
        think=False,  # Ollama turns thinking on by default for models that support it
        keep_alive=-1,  # stay loaded forever
        options=OPTIONS,
    )
    link = next((r["href"] for r in results if WEB_URL.fullmatch(r["href"])), "")
    return (reply.message.content or "").strip(), link


def lookup(term: str) -> tuple[str, str]:
    """explain() for the UI: any failure becomes a readable message instead of a silently dead thread."""
    try:
        return explain(term)
    except Exception as error:  # Ollama not running, model not pulled, ...
        return f"Error: {error}", ""


def warm_up() -> str:
    """Load the model and pin it in memory. Returns an error message, or "" on success."""
    try:
        ollama.generate(model=MODEL, keep_alive=-1, options=OPTIONS)  # an empty prompt just loads it
        return ""
    except Exception as error:
        return f"Model not ready: {error}"


def clipboard_term(root: tk.Misc) -> str:
    try:
        return clean(root.clipboard_get())
    except tk.TclError:  # clipboard is empty or holds non-text
        return ""


def tray_image() -> Image.Image:
    """A blank page with a folded corner."""
    image = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(image)
    draw.polygon([(12, 4), (40, 4), (52, 16), (52, 60), (12, 60)], fill="white", outline="gray", width=3)
    draw.line([(40, 4), (40, 16), (52, 16)], fill="gray", width=3)
    return image


class Popup:
    """Borderless, always-on-top card at the cursor; Esc, a click, focus loss, or a timeout hides it."""

    def __init__(self, root: tk.Tk) -> None:
        self.root, self.url = root, ""
        self._timer: str | None = None
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=BG, padx=18, pady=14)
        self.title = self._label(TITLE_FONT, 0)
        self.body = self._label(BODY_FONT, 4)
        self.link = self._label(BODY_FONT, 8, cursor="hand2")
        self.link.bind("<Button-1>", lambda _: self.url and webbrowser.open(self.url))
        self.body.bind("<Button-1>", lambda _: self.hide())
        root.bind("<Escape>", lambda _: self.hide())
        root.bind("<FocusOut>", lambda _: self.hide())

    def _label(self, font: tuple[str, int], gap: int, **options: str) -> tk.Label:
        label = tk.Label(self.root, font=font, fg=FG, bg=BG, wraplength=WRAP, justify="left", **options)
        label.pack(anchor="w", pady=(gap, 0))
        return label

    def _round_corners(self) -> None:
        """Windows 11 rounded corners and border via DWM (ignored on Windows 10)."""
        for attribute, value in DWM_ATTRIBUTES:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(self.root.wm_frame(), 16), attribute, ctypes.byref(ctypes.c_int(value)), 4
            )

    def show(self, title: str, body: str) -> None:
        self.title["text"], self.body["text"], self.link["text"], self.url = title, body, "", ""
        x, y = self.root.winfo_pointerxy()
        self._place(x + CURSOR_OFFSET, y + CURSOR_OFFSET)
        self.root.deiconify()
        self._round_corners()
        self.root.focus_force()
        self._restart_timer()

    def fill(self, title: str, body: str, url: str) -> None:
        if title != self.title["text"]:
            return  # a newer lookup has replaced this one
        self.body["text"], self.url = body, url
        self.link.configure(text=f"{urlparse(url).hostname} ↗" if url else NO_WEB_NOTE, fg=ACCENT if url else MUTED)
        self._place(self.root.winfo_x(), self.root.winfo_y())  # the answer is taller than "Looking up…"
        self._restart_timer()

    def hide(self) -> None:
        self.root.withdraw()

    def _place(self, x: int, y: int) -> None:
        """Move to (x, y), nudged back inside the virtual desktop (all monitors) so the card isn't clipped."""
        r = self.root
        r.update_idletasks()
        x = min(x, r.winfo_vrootx() + r.winfo_vrootwidth() - r.winfo_reqwidth())
        y = min(y, r.winfo_vrooty() + r.winfo_vrootheight() - r.winfo_reqheight())
        r.geometry(f"+{x}+{y}")

    def _restart_timer(self) -> None:
        if self._timer:
            self.root.after_cancel(self._timer)
        self._timer = self.root.after(HIDE_AFTER_MS, self.hide)


def main() -> None:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
    root = tk.Tk()
    popup = Popup(root)

    def in_background(task: Callable[[], None]) -> None:
        threading.Thread(target=task, daemon=True).start()

    def load_model() -> None:
        if error := warm_up():
            root.after(0, popup.show, "Hover", error)

    def on_hotkey() -> None:
        term = clipboard_term(root)
        if not term:
            popup.show("Hover", EMPTY_CLIPBOARD)
            return
        popup.show(term, LOADING)
        in_background(lambda: root.after(0, popup.fill, term, *lookup(term)))

    def quit_app(icon: pystray.Icon) -> None:
        icon.stop()
        root.after(0, root.destroy)

    icon = pystray.Icon("hover", tray_image(), "Hover", pystray.Menu(pystray.MenuItem("Quit", quit_app)))
    in_background(icon.run)
    hotkeys = keyboard.GlobalHotKeys({HOTKEY: lambda: root.after(0, on_hotkey)})  # hook thread only hands off
    root.after(0, hotkeys.start)  # start once Tk is looping, so the first hand-off can't fail
    root.after(0, in_background, load_model)
    root.mainloop()


if __name__ == "__main__":
    main()
