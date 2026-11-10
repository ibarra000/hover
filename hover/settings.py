"""User settings: built-in defaults merged with %APPDATA%\\Hover\\settings.json."""

import json
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Hover"
SETTINGS_FILE = DATA_DIR / "settings.json"

DEFINE_PROMPT = (
    "You are a terse dictionary and documentation lookup. The user copied a term: an English word or phrase, "
    "or a programming identifier, command, or type. Reply in plain text, no preamble, in at most 3 short lines. "
    "Line 1 says what it is: part of speech, or language or library and kind (function, class, type, keyword, "
    "command). Then give a succinct definition; for code include the signature or usage. Base the answer on the "
    "search results when they are relevant and never invent APIs; if they don't support an answer, start with "
    "'Not sure:'. Items arrive one at a time and may also be a screenshot or a question about earlier items: "
    "describe screenshots the same way, and answer questions using the conversation so far."
)
EXPLAIN_PROMPT = (
    "You explain code, error messages, and technical text the user copied or captured. In at most 4 short lines: "
    "say what it is, what it does or means, and the most likely fix or next step for errors. Use `backticks` for "
    "identifiers. Base the answer on the search results when relevant; if unsure, start with 'Not sure:'."
)
COMPRESS_PROMPT = (
    "You compress context. Ignore the format of earlier replies. Merge everything the user has copied in this "
    "conversation, including the latest item and any screenshots, into one dense paragraph of at most 3 sentences "
    "that keeps every key fact, name, number and code identifier. Plain text, no labels, no preamble."
)
TRANSLATE_PROMPT = (
    "Translate the latest item into {language}. If it is already in {language}, translate it into English. Reply "
    "with the translation only, then one short line with any nuance worth knowing."
)
DEFAULTS = {  # written to SETTINGS_FILE on first run; edit that file to change any of these
    "mode": "define",
    "modes": {
        "define": {"prompt": DEFINE_PROMPT, "search": True},
        "explain": {"prompt": EXPLAIN_PROMPT, "search": True},
        "compress": {"prompt": COMPRESS_PROMPT, "search": False},
        "translate": {"prompt": TRANSLATE_PROMPT, "search": False},
    },
    "hotkeys": {"lookup": "<ctrl>+<alt>+h", "hover": "<ctrl>+<alt>+j", "snip": "<ctrl>+<alt>+s"},
    "provider": "ollama",
    "ollama_host": "http://127.0.0.1:11434",
    "base_url": "",
    "api_key_env": "OPENAI_API_KEY",
    "model": "qwen3:4b-instruct-2507-q4_K_M",
    "vision_model": "qwen3-vl:4b-instruct",
    "temperature": 0.3,
    "max_tokens": 200,
    "search_results": 3,
    "max_chars": 2000,
    "context_messages": 12,
    "hide_after_seconds": 30,
    "language": "Spanish",
    "offline": False,
    "copy_fallback": True,
    "ctrl_c_fallback": False,
    "glossary": True,
    "api_port": 0,
}


def save(settings: dict) -> None:
    """Write atomically, so a crash or a simultaneous edit in Notepad can't leave a truncated file."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2), "utf-8")
    os.replace(temporary, SETTINGS_FILE)


def migrate(raw: dict) -> dict:
    """Upgrade a v0.3 settings file: "prompts" -> "modes" (adding the new built-in modes), "hotkey" -> "hotkeys"."""
    migrated = dict(raw)
    if "prompts" in migrated and "modes" not in migrated:
        old = {name: {"prompt": prompt, "search": name == "define"} for name, prompt in migrated.pop("prompts").items()}
        migrated["modes"] = {**DEFAULTS["modes"], **old}
    if "hotkey" in migrated and "hotkeys" not in migrated:
        migrated["hotkeys"] = {**DEFAULTS["hotkeys"], "lookup": migrated.pop("hotkey")}
    return migrated


def load() -> dict:
    """The defaults, overridden by whatever the user put in settings.json (created on first run)."""
    if not SETTINGS_FILE.exists():
        save(DEFAULTS)
    raw = json.loads(SETTINGS_FILE.read_text("utf-8"))
    migrated = migrate(raw)
    if migrated != raw:
        save(migrated)
    return {**DEFAULTS, **migrated, "hotkeys": {**DEFAULTS["hotkeys"], **migrated.get("hotkeys", {})}}


def update(**changes: object) -> None:
    """Change top-level keys in settings.json, keeping everything else the user wrote."""
    raw = json.loads(SETTINGS_FILE.read_text("utf-8")) if SETTINGS_FILE.exists() else dict(DEFAULTS)
    save({**migrate(raw), **changes})


def safe_load() -> tuple[dict, str]:
    """(settings, error). A malformed file falls back to the defaults *with web search off* (fail closed: the user
    may have chosen strict offline in the file we can't read) and reports why, so the app keeps working."""
    try:
        loaded = load()
        if not isinstance(loaded["modes"], dict) or not all(isinstance(m, dict) for m in loaded["modes"].values()):
            raise TypeError('"modes" must map names to {"prompt": ...} objects')
        return loaded, ""
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:  # OSError: file locked by an editor
        return {**DEFAULTS, "offline": True}, f"settings.json error: {error}"
