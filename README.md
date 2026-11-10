# Hover

**Select, hover over, copy, or snip anything on Windows, press a hotkey, and get a short grounded answer at your cursor.**
Local models (via [Ollama](https://ollama.com)) do the thinking. A web search grounds definitions and shows where the
answer came from. Each lookup builds on the last until you clear the context, and every term you look up goes into a
personal glossary you can export to Anki.

| Hotkey | What it explains |
|---|---|
| **Ctrl+Alt+H** | The text you've **selected**; no copying needed. If nothing is selected, whatever is on the clipboard (text or a screenshot). |
| **Ctrl+Alt+J** | Whatever is **under the mouse**. Nothing needs selecting. |
| **Ctrl+Alt+S** | A region you **snip**: drag a box on a frozen screen. Windows OCR plus the vision model read it. |

In the card:
- Answers **stream** in as they're generated.
- Type a **follow-up** in the box and press Enter.
- **Copy** (Ctrl+C), **Replace** the original selection (Ctrl+R), **Speak** (Ctrl+T), or **Pin** it open (Ctrl+P).
- Esc or clicking elsewhere hides it.
- The footer shows a **privacy badge**: `local` means nothing left your PC, `web · site ↗` means a search was used (click it to open the source).

## Install

1. Install the model runtime and the two models (~5.8 GB; about 7.5 GB of VRAM while loaded):
   ```powershell
   winget install Ollama.Ollama
   ollama pull qwen3:4b-instruct-2507-q4_K_M
   ollama pull qwen3-vl:4b-instruct
   ```
2. Download `hover-x.y.z-win64.msi` from [Releases](../../releases) and run it. Python isn't needed. The MSI isn't
   code-signed, so SmartScreen may warn you; choose **More info → Run anyway**.
3. Launch **Hover** from the desktop and look for the blank-page tray icon. To start it at login, use tray →
   **Start with Windows**.

## Modes

Pick the default mode from the tray. You can also give any mode its own hotkey, or put `>>mode:` at the start of copied
text (for example `>>translate: Guten Morgen`).

| Mode | Does | Web search |
|---|---|---|
| **Define** | A 1–3 line definition of a word or code symbol, with "Not sure:" when the sources don't back it up | yes |
| **Explain** | What code or an error message means, and the likely fix | yes |
| **Compress** | Merges everything in the current context into one dense paragraph | no |
| **Translate** | Into `language` (or into English if it's already in that language) | no |

**Running context:** each lookup is a follow-up to the earlier ones, and the footer counts them. To start fresh, press
the hotkey with nothing selected and an empty clipboard (Win+V → Clear all), or use tray → **Clear context**.

## Settings

Tray → **Settings** opens `%APPDATA%\Hover\settings.json`. Changes apply on the next lookup; hotkeys, models and the
API need a restart (Quit, then relaunch). If the file has a mistake, the card shows the error and Hover keeps running
on defaults with **web search off**.

| Key | Default | What it does |
|---|---|---|
| `hotkeys` | `lookup` Ctrl+Alt+H, `hover` Ctrl+Alt+J, `snip` Ctrl+Alt+S | pynput syntax, e.g. `<ctrl>+<shift>+<space>` |
| `mode` / `modes` | `define` / 4 modes | each mode is `{"prompt", "search", optional "hotkey", "model", "vision_model"}`. **Add one to add a mode.** |
| `provider` | `ollama` | or `openai` for any OpenAI-compatible server (LM Studio, OpenRouter, OpenAI, …) |
| `ollama_host` | `http://127.0.0.1:11434` | use Ollama on another machine |
| `base_url`, `api_key_env` | `""`, `OPENAI_API_KEY` | for `openai`: the endpoint, and the **name** of the environment variable holding the key (keys are never stored in the file) |
| `model`, `vision_model` | qwen3 4B instruct, qwen3-vl 4B instruct | default text and screenshot models |
| `temperature`, `max_tokens` | `0.3`, `200` | |
| `offline` | `false` | strict offline: never search the web (also in the tray) |
| `language` | `Spanish` | target language for Translate |
| `copy_fallback` | `true` | when an app doesn't expose its selection, copy it briefly and restore your clipboard |
| `glossary` | `true` | save looked-up terms |
| `search_results`, `max_chars`, `context_messages`, `hide_after_seconds` | `3`, `2000`, `12`, `30` | |
| `api_port` | `0` (off) | local API, see below |

## Glossary → Anki

Every looked-up term (80 characters or fewer) is saved to `%APPDATA%\Hover\glossary.db`, and the footer shows
"looked up 3×" when you keep coming back to one. Tray → **Export glossary to Anki** writes
`Documents\Hover glossary.csv`, ready for Anki's File → Import.

## Local API (off by default)

Set `api_port` (e.g. `8765`) and restart. Then scripts, AutoHotkey, or a Stream Deck can trigger lookups. API calls use
their own throwaway context and never see yours.

```powershell
$token = Get-Content "$env:APPDATA\Hover\api_token"
Invoke-RestMethod -Method Post http://127.0.0.1:8765/ask -ContentType application/json `
  -Headers @{ Authorization = "Bearer $token" } -Body '{"text": "idempotent", "mode": "define"}'
```

The API listens on 127.0.0.1 only. It requires the token, and it refuses browser requests (`Origin` header) and other
`Host` names (DNS rebinding).

## Privacy

- Screenshots, OCR and both models stay on your PC.
- In Define and Explain, the **first 200 characters** of text you look up go to public search engines (through
  [`ddgs`](https://pypi.org/project/ddgs/)). The badge says `web` whenever that happened. Turn on **Strict offline** to
  stop it entirely.
- Selection capture reads text through Windows UI Automation. In apps that don't support it, Hover copies briefly and
  then restores your previous clipboard, images and formatting included. Text Hover puts on the clipboard is marked so
  it stays out of Win+V history and cloud clipboard.
- Hover only reads anything when you press a hotkey.

## Run from source / test / release

```powershell
uv sync
uvw run launch.pyw                                    # no console window
uv run pytest --cov=hover                             # 63 tests
```

To release: bump `version` in `pyproject.toml`, then `git tag vX.Y.Z && git push origin vX.Y.Z`. GitHub Actions tests,
builds the MSI with cx_Freeze, and publishes it.

## Credits

Ideas and techniques come from studying these open-source projects. Code is only adapted from the MIT-licensed ones:

- [PowerToys](https://github.com/microsoft/PowerToys) (MIT): releasing held modifiers before synthetic keys, the paste-back approach, and Windows OCR language selection
- [selection-hook](https://github.com/0xfullex/selection-hook) (MIT): UI Automation → Ctrl+Insert → Ctrl+C selection capture with clipboard-sequence detection and restore
- [Text-Grab](https://github.com/TheJoeFin/Text-Grab) (MIT): the frozen-screen snip overlay
- [ClipboardConqueror](https://github.com/aseichter2007/ClipboardConqueror) (MIT): inline mode prefixes in copied text
- Design references only (GPL, no code used): [WritingTools](https://github.com/theJayTea/WritingTools), [pot](https://github.com/pot-app/pot-desktop), [Easydict](https://github.com/tisfeng/Easydict)

MIT licensed. See [LICENSE](LICENSE).
