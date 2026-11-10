import tkinter as tk
from types import SimpleNamespace

import pytest
from ddgs.exceptions import DDGSException

import hover

DOCS_URL = "https://docs.python.org/3/library/os.path.html"
RESULTS = [
    {"title": "os.path — pathname functions", "href": DOCS_URL, "body": "os.path.join(path, *paths) joins path segments."},
    {"title": "Tutorial", "href": "https://example.com/join", "body": "How to join paths."},
]


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
        return SimpleNamespace(message=SimpleNamespace(content="  os.path.join: Python function\nJoins paths.  "))

    monkeypatch.setattr(hover.ollama, "chat", fake_chat)
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


def test_explain_grounds_the_prompt_in_search_results(monkeypatch, chat_calls):
    monkeypatch.setattr(hover, "DDGS", fake_search(RESULTS))

    answer, link = hover.explain("os.path.join")

    assert (answer, link) == ("os.path.join: Python function\nJoins paths.", DOCS_URL)
    call = chat_calls[0]
    assert (call["model"], call["think"], call["keep_alive"]) == (hover.MODEL, False, -1)
    assert call["messages"][0]["content"] == hover.SYSTEM_PROMPT
    assert "joins path segments" in call["messages"][1]["content"]


def test_explain_falls_back_to_model_only_when_search_fails(monkeypatch, chat_calls):
    monkeypatch.setattr(hover, "DDGS", fake_search(error=DDGSException("No results found.")))

    _, link = hover.explain("sonder")

    assert link == ""
    assert "(no search results)" in chat_calls[0]["messages"][1]["content"]


def test_explain_only_links_to_web_pages(monkeypatch, chat_calls):
    unsafe = {"title": "x", "href": "javascript:alert(1)", "body": "x"}
    quoted = {"title": "x", "href": 'https://a.example/x"\tfile:///C:/Windows/win.ini', "body": "x"}
    monkeypatch.setattr(hover, "DDGS", fake_search([unsafe, quoted, *RESULTS]))
    assert hover.explain("os.path.join")[1] == DOCS_URL

    monkeypatch.setattr(hover, "DDGS", fake_search([unsafe]))
    assert hover.explain("os.path.join")[1] == ""


def test_lookup_turns_failures_into_a_readable_message(monkeypatch):
    def offline(term):
        raise ConnectionError("Failed to connect to Ollama.")

    monkeypatch.setattr(hover, "explain", offline)
    assert hover.lookup("hubris") == ("Error: Failed to connect to Ollama.", "")


def test_warm_up_pins_the_model_and_reports_failures(monkeypatch):
    calls = []
    monkeypatch.setattr(hover.ollama, "generate", lambda **kwargs: calls.append(kwargs))
    assert hover.warm_up() == ""
    assert (calls[0]["model"], calls[0]["keep_alive"]) == (hover.MODEL, -1)

    def missing(**kwargs):
        raise hover.ollama.ResponseError("model not found", 404)

    monkeypatch.setattr(hover.ollama, "generate", missing)
    assert hover.warm_up().startswith("Model not ready: model not found")


def test_clipboard_term_reads_and_cleans_text_or_returns_empty():
    assert hover.clipboard_term(SimpleNamespace(clipboard_get=lambda: "  std::unique_ptr \n")) == "std::unique_ptr"

    def no_text():
        raise tk.TclError("CLIPBOARD selection doesn't exist")

    assert hover.clipboard_term(SimpleNamespace(clipboard_get=no_text)) == ""


def test_popup_shows_fills_and_hides(root):
    popup = hover.Popup(root)
    assert root.state() == "withdrawn"

    popup.show("ephemeral", hover.LOADING)
    assert root.state() == "normal"
    assert (popup.title["text"], popup.body["text"]) == ("ephemeral", hover.LOADING)

    popup.fill("stale term", "old answer", DOCS_URL)
    assert popup.body["text"] == hover.LOADING

    popup.fill("ephemeral", "adjective: lasting a very short time", "https://www.merriam-webster.com/x")
    assert popup.body["text"] == "adjective: lasting a very short time"
    assert "merriam-webster.com" in popup.link["text"]

    popup.fill("ephemeral", "answer", "")
    assert popup.link["text"] == hover.NO_WEB_NOTE

    popup.hide()
    assert root.state() == "withdrawn"


def test_tray_image_is_a_visible_64px_icon():
    image = hover.tray_image()
    assert (image.size, image.mode) == ((64, 64), "RGBA")
    assert image.getbbox() is not None
