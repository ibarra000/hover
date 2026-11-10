"""The running conversation: modes, web grounding, streaming answers from Ollama or any OpenAI-compatible server."""

import base64
import contextlib
import os
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from urllib.parse import urlparse

import ollama
from ddgs import DDGS
from ddgs.exceptions import DDGSException
from openai import OpenAI

NUM_CTX = 8192  # fixed: changing it would force the models to reload
STREAM_TIMEOUT = 120  # seconds without data before a hung server is given up on (a cold model load takes ~15 s)
MAX_QUERY_CHARS = 200  # only this much of the copied text is ever sent to search engines
MIN_OCR_CHARS = 20  # below this, OCR found no real text and the image speaks for itself
WEB_URL = re.compile(r'https?://[^\x00-\x20"\x7f]+')  # no quotes/whitespace that could break the shell handler
INLINE_MODE = re.compile(r"^>>\s*(\w+)\s*:\s*", re.S)  # ">>translate: bonjour" picks a mode for one item
WHOLE_CONVERSATION = "Apply your instructions to everything copied in this conversation so far, not just this item."


@dataclass(frozen=True)
class Shot:
    """A screenshot (PNG bytes) plus whatever Windows OCR read from it."""

    png: bytes
    text: str = ""


@dataclass(frozen=True)
class Answer:
    text: str
    link: str  # first web source, or "" when the answer is local only
    mode: str
    cancelled: bool = False


def search(term: str, count: int) -> list[dict[str, str]]:
    try:
        return DDGS().text(term[:MAX_QUERY_CHARS], max_results=count)
    except DDGSException:
        return []  # offline or no hits: the popup marks the answer as local


def first_safe_link(results: list[dict[str, str]]) -> str:
    """The first plain http(s) result URL that parses to a real host ("" if none)."""
    for result in results:
        href = result.get("href", "")
        with contextlib.suppress(ValueError):  # e.g. "http://[" passes the regex but isn't a URL
            if WEB_URL.fullmatch(href) and urlparse(href).hostname:
                return href
    return ""


def model_options(settings: dict) -> dict:
    return {"temperature": settings["temperature"], "num_predict": settings["max_tokens"], "num_ctx": NUM_CTX}


def split_inline_mode(text: str, modes: dict) -> tuple[str | None, str]:
    """(mode, text) for ">>mode: text" when mode exists, else (None, text)."""
    match = INLINE_MODE.match(text)
    if match and match.group(1) in modes:
        return match.group(1), text[match.end():]
    return None, text


def openai_messages(messages: list[dict]) -> list[dict]:
    """Ollama-style messages (with optional "images" bytes) -> OpenAI chat format."""
    converted = []
    for message in messages:
        images = message.get("images") or []
        if not images:
            converted.append({"role": message["role"], "content": message["content"]})
            continue
        parts = [{"type": "text", "text": message["content"]}]
        parts += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64.b64encode(png).decode()}"}}
                  for png in images]
        converted.append({"role": message["role"], "content": parts})
    return converted


def stream_chat(settings: dict, model: str, messages: list[dict]) -> Iterator[str]:
    """Yield answer text chunks from the configured provider."""
    if settings["provider"] == "openai":
        client = OpenAI(base_url=settings["base_url"] or None, api_key=os.environ.get(settings["api_key_env"], "none"),
                        timeout=STREAM_TIMEOUT)
        stream = client.chat.completions.create(
            model=model, messages=openai_messages(messages), stream=True,
            temperature=settings["temperature"], max_tokens=settings["max_tokens"],
        )
        for chunk in stream:
            if chunk.choices:
                yield chunk.choices[0].delta.content or ""
        return
    client = ollama.Client(host=settings["ollama_host"], timeout=STREAM_TIMEOUT)
    for chunk in client.chat(
        model=model, messages=messages, stream=True,
        think=False,  # Ollama turns thinking on by default for models that support it
        keep_alive=-1,  # stay loaded forever
        options=model_options(settings),
    ):
        yield chunk.message.content or ""


def warm_up(settings: dict) -> str:
    """Load both Ollama models (and the vision encoder) and pin them in memory. Returns an error or ""."""
    if settings["provider"] != "ollama":
        return ""
    from hover.capture import png_bytes  # local import: capture pulls in Windows-only modules
    from PIL import Image

    client, options = ollama.Client(host=settings["ollama_host"]), model_options(settings)
    try:
        client.generate(model=settings["model"], keep_alive=-1, options=options)  # an empty prompt just loads it
        blank = png_bytes(Image.new("RGB", (64, 64)))  # a real image also warms the vision encoder (~15 s cold)
        client.generate(model=settings["vision_model"], prompt=".", images=[blank], keep_alive=-1,
                        options={**options, "num_predict": 1})
        return ""
    except Exception as error:  # Ollama not running, model not pulled, ...
        return f"Model not ready: {error}"


def cache_key(item: str | Shot, settings: dict, mode: str) -> tuple:
    """Same item + same answer-affecting settings = same answer, so edits to settings.json still take effect."""
    relevant = (settings["model"], settings["vision_model"], settings["language"], settings["offline"],
                settings["provider"], repr(settings["modes"].get(mode)))
    return mode, item, relevant


def trimmed(history: list[dict[str, str]], keep: int) -> list[dict[str, str]]:
    """The newest `keep` messages, in whole user/assistant pairs (0 keeps nothing)."""
    keep = max(0, keep) - max(0, keep) % 2
    return history[max(0, len(history) - keep):] if keep else []


class Session:
    """One running conversation: each lookup is a follow-up until the context is cleared.

    The lock is only held to read or commit the history, never while searching or streaming, so clearing the
    context or starting a new lookup never waits on a slow server.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0  # bumped by clear(), so a stream that started before a clear doesn't commit
        self.history: list[dict[str, str]] = []  # text only: no snippets or image bytes are kept
        self._last: tuple[tuple, Answer] | None = None

    def clear(self) -> None:
        with self._lock:
            self.history, self._last, self._generation = [], None, self._generation + 1

    @property
    def turns(self) -> int:
        return len(self.history) // 2

    def _prompt(self, item: str | Shot, settings: dict, mode: str) -> tuple[str, str, dict, list[dict]]:
        """(model, history note, current message, search results) for this item."""
        config = settings["modes"][mode]
        if isinstance(item, Shot):
            ocr = f"\n\nOCR transcript (may lack indentation):\n{item.text}" if len(item.text) >= MIN_OCR_CHARS else ""
            note = f"[screenshot]{ocr}"
            return config.get("vision_model", settings["vision_model"]), note, \
                {"role": "user", "content": f"Screenshot{ocr}", "images": [item.png]}, []
        searching = config.get("search", False) and not settings["offline"]
        results = search(item, settings["search_results"]) if searching else []
        note = f"Copied: {item}"
        if searching:
            snippets = "\n".join(f"- {r['title']}: {r['body']}" for r in results) or "(no search results)"
            task = f"Search results:\n{snippets}"
        else:
            task = WHOLE_CONVERSATION if mode != "define" else "(offline: answer from your own knowledge)"
        return config.get("model", settings["model"]), note, {"role": "user", "content": f"{note}\n\n{task}"}, results

    def ask(self, item: str | Shot, settings: dict, mode: str | None = None,
            on_token: Callable[[str], None] = lambda _: None, cancel: threading.Event | None = None) -> Answer:
        """Answer with a mode's prompt, streaming chunks to on_token. A set `cancel` stops early (nothing is kept)."""
        if isinstance(item, str):
            inline, item = split_inline_mode(item, settings["modes"])
            mode = inline or mode
        mode = mode or settings["mode"]
        key = cache_key(item, settings, mode)
        with self._lock:
            cached = self._last[1] if self._last and self._last[0] == key else None
            history, generation = list(self.history), self._generation
        if cached:
            on_token(cached.text)
            return cached  # same item again: don't add a duplicate turn
        stopped = Answer("", "", mode, cancelled=True)
        if cancel and cancel.is_set():
            return stopped  # superseded before it even started: skip the search and the model call
        model, note, current, results = self._prompt(item, settings, mode)
        if cancel and cancel.is_set():
            return stopped
        system = {"role": "system", "content": settings["modes"][mode]["prompt"].replace("{language}", settings["language"])}
        if mode == "define" or not history:
            messages = [system, *history, current]
        else:  # one folded message, so earlier reply formats aren't copied and strict chat templates stay happy
            transcript = "\n\n".join(m["content"] for m in history)
            messages = [system, {**current, "content": f"Conversation so far:\n{transcript}\n\n{current['content']}"}]
        chunks = []
        for chunk in stream_chat(settings, model, messages):
            if cancel and cancel.is_set():
                return Answer("".join(chunks).strip(), "", mode, cancelled=True)
            chunks.append(chunk)
            on_token(chunk)
        answer = Answer("".join(chunks).strip(), first_safe_link(results), mode)
        with self._lock:
            if generation == self._generation:  # the context wasn't cleared while we were streaming
                turn = [{"role": "user", "content": note}, {"role": "assistant", "content": answer.text}]
                self.history = trimmed([*self.history, *turn], settings["context_messages"])
                self._last = (key, answer)
        return answer
