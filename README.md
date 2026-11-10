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

## Modes & settings

Pick a mode from the tray menu:
- **Define**: the 1–3 line definition, grounded in a web search.
- **Compress**: merges everything copied in the current context into one dense paragraph. It never searches the web.
  Switching modes keeps the context, so press the hotkey again on the same clipboard to compress what you've collected.

Tray → **Settings** opens `%APPDATA%\Hover\settings.json` in your editor. Changes apply on the next hotkey press;
only `hotkey` needs a restart (Quit, then relaunch).

| Key | Default | What it does |
|---|---|---|
| `hotkey` | `<ctrl>+<alt>+h` | pynput syntax, e.g. `<ctrl>+<shift>+<space>` |
| `mode` | `define` | which of `prompts` to use (the tray sets this) |
| `model` / `vision_model` | `qwen3:4b-instruct-2507-q4_K_M` / `qwen3-vl:4b-instruct` | any Ollama models (run `ollama pull` first) |
| `temperature`, `max_tokens` | `0.3`, `160` | creativity and answer length |
| `search_results` | `3` | web snippets per define lookup |
| `max_chars` | `2000` | how much copied text is used (only the first 200 characters are ever searched) |
| `context_messages` | `12` | how much running context is kept (2 per item) |
| `hide_after_seconds` | `20` | when the card hides itself |
| `prompts` | `define`, `compress` | the system prompt for each mode. **Add a key to add a mode** (restart to see it in the tray) |

Delete a key to restore its default. Delete the file to reset everything.

Other models that work: `hf.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF:Q8_0` (about 3× faster, less accurate) and
`qwen3-vl:2b-instruct` (a lighter vision model). Hover always sends `think=False`, so thinking-capable models answer directly.
Both models stay loaded in memory (`keep_alive=-1`). Check them with `ollama ps`.

## Privacy

Copied text is sent to public search engines (through [`ddgs`](https://pypi.org/project/ddgs/)).
Screenshots never leave your PC, and both models run locally. Hover only reads the clipboard when you press the hotkey, so don't press it with a password copied.

## Test

```powershell
uv run pytest --cov=hover --cov-report=term-missing
```
