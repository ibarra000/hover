# Hover

Copy a word or a code symbol, or snip a screenshot (Win+Shift+S), then press **Ctrl+Alt+H**. A small card at
your cursor tells you what it is. For text, Hover searches the web and a small local model (via
[Ollama](https://ollama.com)) condenses the top results into one to three lines, with a link to the source.
Screenshots go to a second, always-loaded vision model. Windows only.

Each new thing you copy continues the same conversation, so you can snip some code, then copy
"what does line 2 do?". The card's footer shows how many items are in context. To start fresh, press the hotkey
with an empty clipboard (Win+V → **Clear all**) or choose **Clear context** from the tray menu.

## Install

1. Install the model runtime (it starts at login by default) and pull the text and vision models (~5.8 GB in total, about 7.5 GB of VRAM while loaded):
   ```powershell
   winget install Ollama.Ollama
   ollama pull qwen3:4b-instruct-2507-q4_K_M
   ollama pull qwen3-vl:4b-instruct
   ```
2. Download `hover-x.y.z-win64.msi` from [Releases](../../releases) and run it. Python isn't needed.
   The MSI isn't code-signed, so SmartScreen may warn: choose **More info → Run anyway**.
3. Launch **Hover** from the desktop and look for the blank-page tray icon.

Esc, a click, or clicking elsewhere hides the card, and it hides by itself after 20 s. Clicking the link opens the source.
Quit from the tray icon. To start Hover with Windows, copy the desktop shortcut into `shell:startup`.

## Run from source

```powershell
uv sync
uvw run hover.pyw    # no console window
```

## Release

Bump `version` in `pyproject.toml`, then `git tag vX.Y.Z && git push origin vX.Y.Z`.
GitHub Actions runs the tests, builds the MSI with cx_Freeze (`uv run cxfreeze bdist_msi`), and publishes the release.

## Configure

Edit the constants at the top of `hover.pyw`:

- `HOTKEY`: pynput syntax, e.g. `"<ctrl>+<shift>+<space>"`.
- `MODEL`: any Ollama model works. Hover always sends `think=False`, so thinking-capable models answer directly.
  Run `ollama pull <model>` first. Alternatives:
  - `hf.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF:Q8_0`: about 3× faster and half the size, but less accurate (1.2 GB).
  - `qwen3.5:0.8b`: smallest, and the least accurate in testing (~1 GB).

- `VISION_MODEL`: any Ollama vision model, e.g. `qwen3-vl:2b-instruct` (lighter, weaker at small text).

Both models stay loaded in memory (`keep_alive=-1`). Check them with `ollama ps`.

## Privacy

Copied text is sent to public search engines (through [`ddgs`](https://pypi.org/project/ddgs/)).
Screenshots never leave your PC, and both models run locally. Hover only reads the clipboard when you press the hotkey, so don't press it with a password copied.

## Test

```powershell
uv run pytest --cov=hover --cov-report=term-missing
```
