"""Wiring: tray menu, global hotkeys, and the capture -> answer -> card flow."""

import ctypes
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from functools import partial
from pathlib import Path

import pystray
import win32gui
from PIL import Image, ImageDraw, ImageTk
from pynput import keyboard

from hover import capture, ocr, settings, snip
from hover.llm import Answer, Session, Shot, split_inline_mode, warm_up
from hover.store import Glossary
from hover.ui import Popup, footer_text

GLOSSARY_FILE = settings.DATA_DIR / "glossary.db"
EXPORT_FILE = Path.home() / "Documents" / "Hover glossary.csv"
STARTUP_LINK = Path(os.environ.get("APPDATA", Path.home())) / "Microsoft/Windows/Start Menu/Programs/Startup/Hover.lnk"
EMPTY = "Context cleared. Select text, copy something, or snip (Ctrl+Alt+S), then press the hotkey."
ACTION_POLL_MS = 30


def tray_image() -> Image.Image:
    """A blank page with a folded corner."""
    image = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(image)
    draw.polygon([(12, 4), (40, 4), (52, 16), (52, 60), (12, 60)], fill="white", outline="gray", width=3)
    draw.line([(40, 4), (40, 16), (52, 16)], fill="gray", width=3)
    return image


def launch_command() -> tuple[str, str]:
    """(target, arguments) that start this app: the frozen Hover.exe, or pythonw running the launcher."""
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return str(pythonw), f'"{Path(__file__).resolve().parent.parent / "launch.pyw"}"'


def set_autostart(enabled: bool) -> None:
    if not enabled:
        STARTUP_LINK.unlink(missing_ok=True)
        return
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()  # the tray menu runs on its own thread
    target, arguments = launch_command()
    link = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(STARTUP_LINK))
    link.TargetPath, link.Arguments, link.WorkingDirectory = target, arguments, str(Path(target).parent)
    link.Save()


def in_background(task: Callable[[], object]) -> None:
    threading.Thread(target=task, daemon=True).start()


class App:
    def __init__(self, root: tk.Misc | None = None) -> None:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor: coordinates match screenshots on every screen
        self.root = root or tk.Tk()
        self.settings, self.startup_error = settings.safe_load()
        self.session, self.glossary = Session(), Glossary(GLOSSARY_FILE)
        self.popup = Popup(self.root, on_follow_up=self.follow_up, on_replace=self.replace, on_hide=self.cancel_current,
                           on_cycle=self.cycle_mode)
        self.icon = ImageTk.PhotoImage(tray_image())  # kept referenced: the pinned card's title bar and taskbar icon
        self.root.iconphoto(True, self.icon)
        self.request, self.cancel, self.window = 0, threading.Event(), 0
        self.item: tuple[str | bytes, str] | None = None  # the card's looked-up item and its mode (not follow-ups)
        self.actions: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()  # hotkey thread -> Tk thread

    # --- settings -------------------------------------------------------------------------------------------
    def fresh_settings(self) -> dict | None:
        """Re-read settings.json so edits apply immediately; a broken file shows its error instead."""
        current, error = settings.safe_load()
        if error:
            self.popup.notice("Hover", error)
            return None
        return current

    def current(self, key: str) -> object:
        return settings.safe_load()[0][key]

    # --- hotkeys (called on the Tk thread) ---------------------------------------------------------------
    def lookup(self, mode: str | None = None) -> None:
        """Selection first, then the clipboard (text or screenshot); nothing at all clears the context."""
        self.window = win32gui.GetForegroundWindow()
        if not (current := self.fresh_settings()):
            return

        def grab() -> str | bytes:
            limit = current["max_chars"]
            selection = capture.selected_text(current["copy_fallback"], limit, current["ctrl_c_fallback"])
            return selection or capture.clipboard_item(limit)

        self.capture_then_ask(grab, mode, current)

    def hover(self) -> None:
        """Explain what's under the mouse: UI Automation text, or OCR of the area around the cursor."""
        self.window = win32gui.GetForegroundWindow()
        if current := self.fresh_settings():
            under_cursor = partial(capture.uia_text_at_cursor, current["max_chars"])
            self.capture_then_ask(lambda: capture.with_timeout(under_cursor, 0.8) or capture.cursor_crop(), None, current)

    def capture_then_ask(self, grab: Callable[[], str | bytes], mode: str | None, current: dict) -> None:
        """Capture off the Tk thread (it can wait on the clipboard), then ask; capture failures show in the card."""
        def run() -> None:
            try:
                item = grab()
            except Exception as error:  # clipboard locked by another app, screen grab refused, ...
                self.root.after(0, self.popup.notice, "Hover", f"Couldn't read the selection or clipboard: {error}")
                return
            self.root.after(0, self.ask, item, mode, current)

        in_background(run)

    def snip(self) -> None:
        self.window = win32gui.GetForegroundWindow()
        if current := self.fresh_settings():
            snip.start(self.root, lambda image: self.ask(capture.png_bytes(image), None, current))

    def follow_up(self, question: str) -> None:
        if current := self.fresh_settings():
            self.ask(question, None, current, keep_position=True, save=False)  # questions aren't glossary terms

    def replace(self, text: str) -> None:
        def paste() -> None:
            if not capture.paste_text(text, self.window):
                self.root.after(0, self.popup.notice, "Hover", "Couldn't return to the original window, so nothing "
                                "was pasted. Use Copy instead.")

        in_background(paste)

    def cancel_current(self) -> None:
        self.cancel.set()

    def cycle_mode(self) -> None:
        """Shift+Tab in the card: make the next mode the default and answer the current item again in it."""
        if not (current := self.fresh_settings()):
            return
        item, shown = self.item or ("", current["mode"])
        names = list(current["modes"])
        mode = names[(names.index(shown) + 1) % len(names)] if shown in names else names[0]
        settings.update(mode=mode)
        if not item:
            self.popup.notice("Hover", f"Mode: {mode}")
            return
        self.ask(item, mode, {**current, "mode": mode}, keep_position=True)

    # --- the lookup flow ----------------------------------------------------------------------------------
    def begin(self, title: str, mode: str, current: dict, keep_position: bool) -> tuple[int, threading.Event]:
        """Show the card for a new lookup and cancel whatever was still streaming (Tk thread)."""
        self.cancel.set()
        self.cancel, self.request = threading.Event(), self.request + 1
        self.popup.hide_ms = current["hide_after_seconds"] * 1000
        self.popup.show(title, self.request, keep_position, status=f"{mode} · Shift+Tab: next mode")
        return self.request, self.cancel

    def ask(self, item: str | bytes, mode: str | None, current: dict, keep_position: bool = False,
            save: bool = True) -> None:
        if not item:
            self.cancel.set()
            self.session.clear()  # quick: the session lock is never held while streaming
            self.item = None
            self.popup.notice("Hover", EMPTY)
            return
        if isinstance(item, str):
            inline, item = split_inline_mode(item, current["modes"])
            mode = inline or mode
        mode = mode or current["mode"]
        if save:  # a lookup, not a follow-up question: this is what Shift+Tab re-answers
            self.item = (item, mode)  # without any ">>mode:" prefix, so Shift+Tab can change the mode
        item, title = (Shot(item), "Screenshot") if isinstance(item, bytes) else (item, item)
        request, cancel = self.begin(title, mode, current, keep_position)
        in_background(partial(self.answer, item, mode, current, request, cancel, save=save))

    def remember(self, item: str | Shot, result: Answer, current: dict, save: bool) -> int:
        """Add a looked-up term to the glossary; a database problem never costs the user their answer."""
        if not (save and current["glossary"] and isinstance(item, str)):
            return 0
        try:
            return self.glossary.add(item, result.mode, result.text, result.link)
        except sqlite3.Error:
            return 0

    def answer(self, item: str | Shot, mode: str | None, current: dict, request: int,
               cancel: threading.Event, save: bool = True) -> None:
        """Worker: OCR screenshots, stream the answer into the card, record it in the glossary."""
        try:
            if isinstance(item, Shot) and not item.text:
                item = Shot(item.png, ocr.read_text(item.png))
            result = self.session.ask(item, current, mode, cancel=cancel,
                                      on_token=lambda chunk: self.root.after(0, self.popup.append, request, chunk))
            if result.cancelled:
                return
            count = self.remember(item, result, current, save)
            footer = footer_text(result.link, result.mode, self.session.turns, count)
            self.root.after(0, self.popup.finish, request, result.text, footer, result.link)
        except Exception as error:  # Ollama down, model missing, bad mode name, ...: show it, don't swallow it
            self.root.after(0, self.popup.finish, request, f"Error: {error}", "error", "")

    # --- tray -------------------------------------------------------------------------------------------
    def export_glossary(self) -> None:
        try:
            EXPORT_FILE.parent.mkdir(parents=True, exist_ok=True)  # e.g. a OneDrive-redirected Documents folder
            cards = self.glossary.export_anki(EXPORT_FILE)
        except (OSError, sqlite3.Error) as error:
            self.root.after(0, self.popup.notice, "Hover", f"Couldn't export the glossary: {error}")
            return
        subprocess.Popen(["explorer", f"/select,{EXPORT_FILE}"])
        self.root.after(0, self.popup.notice, "Glossary exported", f"{cards} cards → {EXPORT_FILE}")

    def tray_menu(self) -> pystray.Menu:
        def mode_item(name: str) -> pystray.MenuItem:
            return pystray.MenuItem(name.title(), lambda: settings.update(mode=name),
                                    checked=lambda _: self.current("mode") == name, radio=True)

        return pystray.Menu(
            *map(mode_item, self.settings["modes"]),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Strict offline", lambda: settings.update(offline=not self.current("offline")),
                             checked=lambda _: bool(self.current("offline"))),
            pystray.MenuItem("Start with Windows", lambda: set_autostart(not STARTUP_LINK.exists()),
                             checked=lambda _: STARTUP_LINK.exists()),
            pystray.MenuItem("Clear context", self.session.clear),
            pystray.MenuItem("Export glossary to Anki", self.export_glossary),
            pystray.MenuItem("Settings", lambda: os.startfile(settings.SETTINGS_FILE)),
            pystray.MenuItem("Quit", self.quit),
        )

    def hotkey_map(self, current: dict) -> dict[str, Callable[[], None]]:
        """Global hotkeys -> actions. The keyboard hook thread only drops the action in a queue: it must never block
        (root.after from another thread waits for Tk, and Windows unhooks slow low-level hooks)."""
        actions = {"lookup": self.lookup, "hover": self.hover, "snip": self.snip}
        bound = {combo: actions[name] for name, combo in current["hotkeys"].items() if name in actions and combo}
        bound |= {config["hotkey"]: partial(self.lookup, name)
                  for name, config in current["modes"].items() if config.get("hotkey")}
        return {combo: partial(self.actions.put, action) for combo, action in bound.items()}

    def run_queued_actions(self) -> None:
        """Tk thread: run whatever the hotkeys queued, then check again shortly."""
        while not self.actions.empty():
            self.actions.get_nowait()()
        self.root.after(ACTION_POLL_MS, self.run_queued_actions)

    def start_hotkeys(self) -> None:
        try:
            listener = keyboard.GlobalHotKeys(self.hotkey_map(self.settings))
        except ValueError as error:  # a typo in a hotkey: fall back to the defaults and say so
            self.popup.notice("Hover", f"Bad hotkey in settings.json ({error}); using defaults.")
            listener = keyboard.GlobalHotKeys(self.hotkey_map(settings.DEFAULTS))
        listener.start()

    def quit(self, icon: pystray.Icon) -> None:
        icon.stop()
        self.root.after(0, self.root.destroy)

    def run(self) -> None:
        icon = pystray.Icon("hover", tray_image(), "Hover", self.tray_menu())
        in_background(icon.run)
        self.root.after(0, self.start_hotkeys)
        self.root.after(0, self.run_queued_actions)
        if self.startup_error:
            self.root.after(0, self.popup.notice, "Hover", self.startup_error)

        def load_models() -> None:
            if error := warm_up(self.settings):
                self.root.after(0, self.popup.notice, "Hover", error)

        self.root.after(0, in_background, load_models)
        self.root.mainloop()


def main() -> None:
    App().run()
