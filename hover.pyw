"""Hover: copy a word or code symbol, press the hotkey, read a succinct definition."""

import ctypes
import io
import json
import os
import re
import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import ollama
import pystray
from ddgs import DDGS
from ddgs.exceptions import DDGSException
from PIL import Image, ImageDraw, ImageGrab
from pynput import keyboard

SETTINGS_FILE = Path(os.environ.get("APPDATA", Path.home())) / "Hover" / "settings.json"
NUM_CTX = 8192  # fixed: changing it would force both models to reload
MAX_QUERY_CHARS = 200  # only this much of the copied text is ever sent to search engines
MAX_TITLE_CHARS = 60
MAX_IMAGE_PX = 1280
WEB_URL = re.compile(r'https?://[^\x00-\x20"\x7f]+')  # no quotes/whitespace that could break the shell handler
CURSOR_OFFSET = 16
WRAP = "4i"  # inches, so the card scales with display DPI
LOADING = "Looking up…"
NO_WEB_NOTE = "model only"
EMPTY_CLIPBOARD = "Context cleared. Copy text or snip a screenshot (Win+Shift+S), then press the hotkey."
BG, FG, MUTED, ACCENT = "#202020", "#ffffff", "#9d9d9d", "#60cdff"  # Windows 11 dark flyout
TITLE_FONT, BODY_FONT = ("Segoe UI Variable Display Semibold", 13), ("Segoe UI Variable Text", 10)
DWM_ATTRIBUTES = ((33, 2), (34, 0x3A3A3A))  # rounded corners; grey border (COLORREF)
DEFINE_PROMPT = (
    "You are a terse dictionary and documentation lookup. The user copied a term: an English word "
    "or phrase, or a programming identifier, command, or type. Reply in plain text with no markdown "
    "and no preamble, in at most 3 short lines. Line 1 says what it is: part of speech, or language "
    "or library and kind (function, class, type, keyword, command). Then give a succinct definition; "
    "for code include the signature or usage. Base the answer on the search results when they are "
    "relevant and never invent APIs. Items arrive one at a time and may also be a screenshot or a "
    "question about earlier items: describe screenshots the same way, and answer questions using the "
    "conversation so far."
)
COMPRESS_PROMPT = (
    "You compress context. Ignore the format of earlier replies. Merge everything the user has copied in "
    "this conversation, including the latest item and any screenshots, into one dense paragraph of at most "
    "3 sentences that keeps every key fact, name, number and code identifier. Plain text, no labels, no "
    "markdown, no preamble."
)
WHOLE_CONVERSATION = "Apply your instructions to everything copied in this conversation so far, not just this item."
DEFAULTS = {  # written to SETTINGS_FILE on first run; edit that file to change any of these
    "hotkey": "<ctrl>+<alt>+h",
    "mode": "define",
    "model": "qwen3:4b-instruct-2507-q4_K_M",
    "vision_model": "qwen3-vl:4b-instruct",
    "temperature": 0.3,
    "max_tokens": 160,
    "search_results": 3,
    "max_chars": 2000,
    "context_messages": 12,
    "hide_after_seconds": 20,
    "prompts": {"define": DEFINE_PROMPT, "compress": COMPRESS_PROMPT},
}


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), "utf-8")


def load_settings() -> dict:
    """The defaults, overridden by whatever the user put in settings.json (created on first run)."""
    if not SETTINGS_FILE.exists():
        save_settings(DEFAULTS)
    return {**DEFAULTS, **json.loads(SETTINGS_FILE.read_text("utf-8"))}


def current_mode() -> str:
    """The saved mode, or "" while settings.json can't be parsed (the tray menu must never crash)."""
    try:
        return load_settings()["mode"]
    except ValueError:
        return ""


def set_mode(name: str) -> None:
    save_settings({**load_settings(), "mode": name})


def model_options(settings: dict) -> dict:
    return {"temperature": settings["temperature"], "num_predict": settings["max_tokens"], "num_ctx": NUM_CTX}


def clean(text: str, limit: int) -> str:
    """Collapse whitespace and cap the length."""
    return " ".join(text.split())[:limit]


def search(term: str, count: int) -> list[dict[str, str]]:
    try:
        return DDGS().text(term[:MAX_QUERY_CHARS], max_results=count)
    except DDGSException:
        return []  # offline or no hits: the popup marks the answer as model-only


class Session:
    """One running conversation: each hotkey press is a follow-up until the context is cleared."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.clear()

    def clear(self) -> None:
        with self._lock:
            self.history: list[dict[str, str]] = []  # text only: no snippets or image bytes are kept
            self._last: tuple[tuple[str, str | bytes], str, str] | None = None  # (mode, item), answer, link

    @property
    def turns(self) -> int:
        return len(self.history) // 2

    def ask(self, item: str | bytes, settings: dict) -> tuple[str, str]:
        """Answer with the current mode's prompt. Text goes to the text model, grounded in web snippets in
        define mode; a screenshot goes straight to the vision model."""
        mode = settings["mode"]
        with self._lock:
            if self._last and self._last[0] == (mode, item):
                return self._last[1], self._last[2]  # same clipboard again: don't add a duplicate turn
            if isinstance(item, bytes):
                model, note, results = settings["vision_model"], "[screenshot]", []
                current = {"role": "user", "content": "Screenshot", "images": [item]}
            else:
                model, note = settings["model"], f"Copied: {item}"
                results = search(item, settings["search_results"]) if mode == "define" else []
                snippets = "\n".join(f"- {r['title']}: {r['body']}" for r in results) or "(no search results)"
                task = f"Search results:\n{snippets}" if mode == "define" else WHOLE_CONVERSATION
                current = {"role": "user", "content": f"{note}\n\n{task}"}
            transcript = [{"role": "user", "content": "\n\n".join(m["content"] for m in self.history)}] if self.history else []
            past = self.history if mode == "define" else transcript  # one block, so earlier reply formats aren't copied
            reply = ollama.chat(
                model=model,
                messages=[{"role": "system", "content": settings["prompts"][mode]}, *past, current],
                think=False,  # Ollama turns thinking on by default for models that support it
                keep_alive=-1,  # stay loaded forever
                options=model_options(settings),
            )
            answer = (reply.message.content or "").strip()
            link = next((r["href"] for r in results if WEB_URL.fullmatch(r["href"])), "")
            turn = [{"role": "user", "content": note}, {"role": "assistant", "content": answer}]
            self.history = [*self.history, *turn][-settings["context_messages"]:]
            self._last = ((mode, item), answer, link)
            return answer, link


def lookup(session: Session, item: str | bytes, settings: dict) -> tuple[str, str]:
    """Session.ask for the UI: any failure becomes a readable message instead of a silently dead thread."""
    try:
        return session.ask(item, settings)
    except Exception as error:  # Ollama not running, model not pulled, bad setting, ...
        return f"Error: {error}", ""


def warm_up(settings: dict) -> str:
    """Load both models and pin them in memory. Returns an error message, or "" on success."""
    options = model_options(settings)
    try:
        ollama.generate(model=settings["model"], keep_alive=-1, options=options)  # an empty prompt just loads it
        blank = png(Image.new("RGB", (64, 64)))  # a real image also warms the vision encoder (~15 s cold)
        ollama.generate(
            model=settings["vision_model"], prompt=".", images=[blank], keep_alive=-1, options={**options, "num_predict": 1}
        )
        return ""
    except Exception as error:
        return f"Model not ready: {error}"


def png(image: Image.Image) -> bytes:
    image.thumbnail((MAX_IMAGE_PX, MAX_IMAGE_PX))  # big snips stay fast
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def clipboard_item(root: tk.Misc, limit: int) -> str | bytes:
    """A snipped screenshot as PNG bytes, else the clipboard's cleaned text, else ""."""
    image = ImageGrab.grabclipboard()
    if isinstance(image, Image.Image):
        return png(image)
    try:
        return clean(root.clipboard_get(), limit)
    except tk.TclError:  # clipboard is empty or holds something else
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
        self.root, self.url, self.hide_ms = root, "", DEFAULTS["hide_after_seconds"] * 1000
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

    def fill(self, title: str, body: str, url: str, turns: int, mode: str) -> None:
        if title != self.title["text"]:
            return  # a newer lookup has replaced this one
        self.body["text"], self.url = body, url
        source = f"{urlparse(url).hostname} ↗" if url else NO_WEB_NOTE if mode == "define" else mode
        footer = source + (f" · {turns} in context" if turns > 1 else "")
        self.link.configure(text=footer, fg=ACCENT if url else MUTED)
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
        self._timer = self.root.after(self.hide_ms, self.hide)


def main() -> None:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
    root = tk.Tk()
    popup, session = Popup(root), Session()
    try:
        startup = load_settings()
    except ValueError:  # malformed settings.json: run on defaults; the first lookup shows the error
        startup = DEFAULTS

    def in_background(task: Callable[[], None]) -> None:
        threading.Thread(target=task, daemon=True).start()

    def load_model() -> None:
        if error := warm_up(startup):
            root.after(0, popup.show, "Hover", error)

    def on_hotkey() -> None:
        try:
            settings = load_settings()  # re-read every time, so edits apply without a restart
        except ValueError as error:
            popup.show("Hover", f"settings.json error: {error}")
            return
        popup.hide_ms = settings["hide_after_seconds"] * 1000
        item = clipboard_item(root, settings["max_chars"])
        if not item:
            session.clear()
            popup.show("Hover", EMPTY_CLIPBOARD)
            return
        title = item[:MAX_TITLE_CHARS] if isinstance(item, str) else "Screenshot"
        popup.show(title, LOADING)
        in_background(
            lambda: root.after(0, popup.fill, title, *lookup(session, item, settings), session.turns, settings["mode"])
        )

    def mode_item(name: str) -> pystray.MenuItem:
        return pystray.MenuItem(name.title(), lambda: set_mode(name), checked=lambda _: current_mode() == name, radio=True)

    def quit_app(icon: pystray.Icon) -> None:
        icon.stop()
        root.after(0, root.destroy)

    icon = pystray.Icon("hover", tray_image(), "Hover", pystray.Menu(
        *map(mode_item, startup["prompts"]),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Clear context", session.clear),
        pystray.MenuItem("Settings", lambda: os.startfile(SETTINGS_FILE)),
        pystray.MenuItem("Quit", quit_app),
    ))
    in_background(icon.run)
    hotkeys = keyboard.GlobalHotKeys({startup["hotkey"]: lambda: root.after(0, on_hotkey)})  # hook thread only hands off
    root.after(0, hotkeys.start)  # start once Tk is looping, so the first hand-off can't fail
    root.after(0, in_background, load_model)
    root.mainloop()


if __name__ == "__main__":
    main()
