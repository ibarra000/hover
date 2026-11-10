import threading

import pytest

from hover import app, llm, settings
from hover.llm import Shot
from tests.conftest import fake_search, pump


@pytest.fixture
def hover_app(monkeypatch, tmp_path, root):
    monkeypatch.setattr(app, "GLOSSARY_FILE", tmp_path / "glossary.db")

    def fake_stream(current, model, messages):
        yield from ["adjective: ", "short-lived"]

    monkeypatch.setattr(llm, "stream_chat", fake_stream)
    monkeypatch.setattr(llm, "DDGS", fake_search([]))
    monkeypatch.setattr(app.ocr, "read_text", lambda png: "OCR text from the screenshot")
    instance = app.App(root)
    yield instance
    instance.cancel.set()
    instance.glossary.close()


def test_tray_image_and_titles():
    assert app.tray_image().size == (64, 64)
    assert app.title_for("mutex") == "mutex"
    assert app.title_for(Shot(b"png")) == "Screenshot"


def test_launch_command_runs_the_launcher_with_pythonw():
    target, arguments = app.launch_command()
    assert target.endswith("pythonw.exe") and arguments.endswith('launch.pyw"')


def test_autostart_off_removes_the_startup_link(monkeypatch, tmp_path):
    link = tmp_path / "Hover.lnk"
    link.write_text("x")
    monkeypatch.setattr(app, "STARTUP_LINK", link)
    app.set_autostart(False)
    assert not link.exists()


def test_a_lookup_streams_into_the_card_and_lands_in_the_glossary(hover_app):
    current = settings.load()
    hover_app.ask("ephemeral", None, current)

    assert pump(hover_app.root, lambda: hover_app.popup.status["text"].startswith("local"))
    assert hover_app.popup.answer == "adjective: short-lived"
    hover_app.ask("ephemeral", "translate", current)
    assert pump(hover_app.root, lambda: "translate" in hover_app.popup.status["text"])
    assert hover_app.glossary.rows()[0][:2] == ("ephemeral", "translate")


def test_screenshots_are_ocrd_and_inline_modes_apply(hover_app, monkeypatch):
    seen = []
    original = hover_app.session.ask

    def spy(item, current, mode=None, **kwargs):
        seen.append((item, mode))
        return original(item, current, mode, **kwargs)

    monkeypatch.setattr(hover_app.session, "ask", spy)
    current = settings.load()
    hover_app.ask(b"\x89PNG", None, current)
    assert pump(hover_app.root, lambda: bool(seen) and hover_app.popup.status["text"])
    hover_app.ask(">>compress: a long article", None, current)
    assert pump(hover_app.root, lambda: len(seen) == 2 and "compress" in hover_app.popup.status["text"])

    assert seen[0] == (Shot(b"\x89PNG", "OCR text from the screenshot"), None)
    assert seen[1] == ("a long article", "compress")


def test_empty_input_clears_the_context(hover_app):
    hover_app.session.history = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
    hover_app.ask("", None, settings.load())
    assert hover_app.session.history == [] and "Context cleared" in hover_app.popup.answer


def test_errors_are_shown_in_the_card(hover_app, monkeypatch):
    def broken(*args, **kwargs):
        raise ConnectionError("Failed to connect to Ollama.")

    monkeypatch.setattr(hover_app.session, "ask", broken)
    hover_app.ask("mutex", None, settings.load())
    assert pump(hover_app.root, lambda: hover_app.popup.answer.startswith("Error: Failed to connect"))


def test_api_requests_run_through_the_same_flow(hover_app):
    result = {}
    worker = threading.Thread(target=lambda: result.update(hover_app.api_ask("mutex", None)))
    worker.start()
    assert pump(hover_app.root, lambda: not worker.is_alive())
    assert result == {"answer": "adjective: short-lived", "source": "", "mode": "define"}


def test_hotkey_map_includes_per_mode_hotkeys(hover_app):
    current = settings.load()
    current["modes"]["translate"]["hotkey"] = "<ctrl>+<alt>+t"
    assert set(hover_app.hotkey_map(current)) == {"<ctrl>+<alt>+h", "<ctrl>+<alt>+j", "<ctrl>+<alt>+s", "<ctrl>+<alt>+t"}


def test_tray_menu_lists_modes_and_actions(hover_app):
    labels = [item.text for item in hover_app.tray_menu().items if item.text]
    assert labels[:4] == ["Define", "Explain", "Compress", "Translate"]
    assert {"Strict offline", "Start with Windows", "Clear context", "Export glossary to Anki", "Settings", "Quit"} <= set(labels)
