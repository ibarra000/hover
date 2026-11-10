import io
import tkinter as tk
from types import SimpleNamespace

import pytest
from ddgs.exceptions import DDGSException
from PIL import Image

import hover

DOCS_URL = "https://docs.python.org/3/library/os.path.html"
RESULTS = [
    {"title": "os.path — pathname functions", "href": DOCS_URL, "body": "os.path.join(path, *paths) joins path segments."},
    {"title": "Tutorial", "href": "https://example.com/join", "body": "How to join paths."},
]
ANSWER = "os.path.join: Python function\nJoins paths."
PNG = b"\x89PNG fake screenshot"


def fake_search(results=(), error=None):
    class FakeDDGS:
        def text(self, query, max_results):
            if error:
                raise error
            return list(results)

    return FakeDDGS


@pytest.fixture
def chat_calls(monkeypatch):
    calls = []

    def fake_chat(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(message=SimpleNamespace(content=f"  {ANSWER}  "))

    monkeypatch.setattr(hover.ollama, "chat", fake_chat)
    monkeypatch.setattr(hover, "DDGS", fake_search(RESULTS))
    return calls


@pytest.fixture
def root():
    window = tk.Tk()
    yield window
    window.destroy()


def test_clean_collapses_whitespace_and_caps_length():
    assert hover.clean("  os.path\n\t.join  ") == "os.path .join"
    assert hover.clean(" \n ") == ""
    assert len(hover.clean("x" * 1000)) == hover.MAX_TERM_CHARS


def test_text_is_grounded_in_search_and_remembered_without_snippets(chat_calls):
    session = hover.Session()

    assert session.ask("os.path.join") == (ANSWER, DOCS_URL)

    call = chat_calls[0]
    assert (call["model"], call["think"], call["keep_alive"]) == (hover.MODEL, False, -1)
    assert call["messages"][0]["content"] == hover.SYSTEM_PROMPT
    assert "joins path segments" in call["messages"][-1]["content"]
    assert session.history == [
        {"role": "user", "content": "Copied: os.path.join"},
        {"role": "assistant", "content": ANSWER},
    ]
    assert session.turns == 1


def test_follow_ups_carry_the_conversation(chat_calls):
    session = hover.Session()
    session.ask("os.path.join")
    session.ask("what does it return?")

    assert chat_calls[1]["messages"][1:3] == session.history[:2]
    assert session.turns == 2


def test_screenshots_go_to_the_vision_model_without_search(monkeypatch, chat_calls):
    monkeypatch.setattr(hover, "DDGS", fake_search(error=AssertionError("searched")))
    session = hover.Session()

    assert session.ask(PNG) == (ANSWER, "")

    call = chat_calls[0]
    assert call["model"] == hover.VISION_MODEL
    assert call["messages"][-1]["images"] == [PNG]
    assert session.history[0] == {"role": "user", "content": "[screenshot]"}


def test_repeating_an_item_reuses_the_answer(chat_calls):
    session = hover.Session()
    session.ask("os.path.join")

    assert session.ask("os.path.join") == (ANSWER, DOCS_URL)
    assert len(chat_calls) == 1


def test_clear_and_trim_bound_the_history(chat_calls):
    session = hover.Session()
    for n in range(hover.MAX_MESSAGES):
        session.ask(f"term {n}")
    assert len(session.history) == hover.MAX_MESSAGES

    session.clear()
    assert (session.history, session.turns) == ([], 0)


def test_search_failure_falls_back_to_model_only(monkeypatch, chat_calls):
    monkeypatch.setattr(hover, "DDGS", fake_search(error=DDGSException("No results found.")))

    assert hover.Session().ask("sonder")[1] == ""
    assert "(no search results)" in chat_calls[0]["messages"][-1]["content"]


def test_only_links_to_web_pages(monkeypatch, chat_calls):
    unsafe = {"title": "x", "href": "javascript:alert(1)", "body": "x"}
    quoted = {"title": "x", "href": 'https://a.example/x"\tfile:///C:/Windows/win.ini', "body": "x"}
    monkeypatch.setattr(hover, "DDGS", fake_search([unsafe, quoted, *RESULTS]))
    assert hover.Session().ask("os.path.join")[1] == DOCS_URL

    monkeypatch.setattr(hover, "DDGS", fake_search([unsafe]))
    assert hover.Session().ask("os.path.join")[1] == ""


def test_lookup_turns_failures_into_a_readable_message():
    class Offline:
        def ask(self, item):
            raise ConnectionError("Failed to connect to Ollama.")

    assert hover.lookup(Offline(), "hubris") == ("Error: Failed to connect to Ollama.", "")


def test_warm_up_pins_both_models_and_reports_failures(monkeypatch):
    calls = []
    monkeypatch.setattr(hover.ollama, "generate", lambda **kwargs: calls.append(kwargs))
    assert hover.warm_up() == ""
    assert [(c["model"], c["keep_alive"]) for c in calls] == [(hover.MODEL, -1), (hover.VISION_MODEL, -1)]

    def missing(**kwargs):
        raise hover.ollama.ResponseError("model not found", 404)

    monkeypatch.setattr(hover.ollama, "generate", missing)
    assert hover.warm_up().startswith("Model not ready: model not found")


def test_clipboard_item_prefers_screenshots_then_text(monkeypatch):
    text_root = SimpleNamespace(clipboard_get=lambda: "  std::unique_ptr \n")
    monkeypatch.setattr(hover.ImageGrab, "grabclipboard", lambda: Image.new("RGB", (4000, 100)))
    png = hover.clipboard_item(text_root)
    assert Image.open(io.BytesIO(png)).width == hover.MAX_IMAGE_PX

    monkeypatch.setattr(hover.ImageGrab, "grabclipboard", lambda: None)
    assert hover.clipboard_item(text_root) == "std::unique_ptr"

    def no_text():
        raise tk.TclError("CLIPBOARD selection doesn't exist")

    assert hover.clipboard_item(SimpleNamespace(clipboard_get=no_text)) == ""


def test_popup_shows_fills_and_hides(root):
    popup = hover.Popup(root)
    assert root.state() == "withdrawn"

    popup.show("ephemeral", hover.LOADING)
    assert root.state() == "normal"
    assert (popup.title["text"], popup.body["text"]) == ("ephemeral", hover.LOADING)

    popup.fill("stale term", "old answer", DOCS_URL, 1)
    assert popup.body["text"] == hover.LOADING

    popup.fill("ephemeral", "adjective: lasting a very short time", "https://www.merriam-webster.com/x", 1)
    assert popup.body["text"] == "adjective: lasting a very short time"
    assert popup.link["text"] == "www.merriam-webster.com ↗"

    popup.fill("ephemeral", "answer", "", 3)
    assert popup.link["text"] == f"{hover.NO_WEB_NOTE} · 3 in context"

    popup.hide()
    assert root.state() == "withdrawn"


def test_tray_image_is_a_visible_64px_icon():
    image = hover.tray_image()
    assert (image.size, image.mode) == ((64, 64), "RGBA")
    assert image.getbbox() is not None
