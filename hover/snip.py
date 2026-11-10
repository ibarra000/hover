"""Built-in region capture: freeze every screen, drag a rectangle, crop. The Text-Grab FullscreenGrab pattern (MIT)."""

import tkinter as tk
from collections.abc import Callable

import win32api
import win32con
from PIL import Image, ImageEnhance, ImageGrab, ImageTk

DIM = 0.55  # brightness of the frozen screen outside the selection
MIN_SIZE = 6  # ignore accidental clicks
OUTLINE = "#60cdff"


def virtual_origin() -> tuple[int, int]:
    """Top-left of the desktop spanning all monitors (negative when a monitor sits left of or above the main one)."""
    return win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN), win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)


def normalize(x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int, int] | None:
    """A drag in any direction as (left, top, right, bottom), or None if it's too small to mean anything."""
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    return (left, top, right, bottom) if right - left >= MIN_SIZE and bottom - top >= MIN_SIZE else None


def start(root: tk.Tk, on_done: Callable[[Image.Image], None]) -> None:
    """Show the overlay; on_done gets the cropped image when the user finishes dragging (Esc cancels)."""
    frozen = ImageGrab.grab(all_screens=True)
    left, top = virtual_origin()
    overlay = tk.Toplevel(root)
    overlay.overrideredirect(True)
    overlay.attributes("-topmost", True)
    overlay.geometry(f"{frozen.width}x{frozen.height}+{left}+{top}")
    canvas = tk.Canvas(overlay, highlightthickness=0, cursor="crosshair")
    canvas.pack(fill="both", expand=True)
    overlay.photos = [ImageTk.PhotoImage(ImageEnhance.Brightness(frozen).enhance(DIM))]  # keep references alive
    canvas.create_image(0, 0, image=overlay.photos[0], anchor="nw")
    bright = canvas.create_image(0, 0, anchor="nw")
    box = canvas.create_rectangle(0, 0, 0, 0, outline=OUTLINE, width=2)
    drag: dict[str, int] = {}

    def press(event: tk.Event) -> None:
        drag.update(x=event.x, y=event.y)

    def move(event: tk.Event) -> None:
        if "x" not in drag:
            return
        region = normalize(drag["x"], drag["y"], event.x, event.y)
        canvas.coords(box, drag["x"], drag["y"], event.x, event.y)
        if region:  # show the selection at full brightness so it's obvious what will be captured
            overlay.photos[1:] = [ImageTk.PhotoImage(frozen.crop(region))]
            canvas.itemconfigure(bright, image=overlay.photos[1])
            canvas.coords(bright, region[0], region[1])

    def release(event: tk.Event) -> None:
        region = normalize(drag.get("x", 0), drag.get("y", 0), event.x, event.y)
        overlay.destroy()
        if region:
            on_done(frozen.crop(region))

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", move)
    canvas.bind("<ButtonRelease-1>", release)
    overlay.bind("<Escape>", lambda _: overlay.destroy())
    overlay.focus_force()
