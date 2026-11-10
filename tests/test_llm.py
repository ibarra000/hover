import threading
from types import SimpleNamespace

import pytest
from ddgs.exceptions import DDGSException

from hover import llm
from hover.llm import Session, Shot
from hover.settings import DEFAULTS
from tests.conftest import DOCS_URL, RESULTS, fake_search

ANSWER_CHUNKS = ["os.path.join: ", "Python function"]
ANSWER = "".join(ANSWER_CHUNKS)
COMPRESS = {**DEFAULTS, "mode": "compress"}


@pytest.fixture
def chats(monkeypatch):
    calls = []

    def fake_stream(settings, model, messages):
        calls.append({"model": model, "messages": messages})
        yield from ANSWER_CHUNKS

    monkeypatch.setattr(llm, "stream_chat", fake_stream)
    monkeypatch.setattr(llm, "DDGS", fake_search(RESULTS))
    return calls


def test_define_is_grounded_streamed_and_remembered_without_snippets(chats):
    session, tokens = Session(), []

    answer = session.ask("os.path.join", DEFAULTS, on_token=tokens.append)

    assert (answer.text, answer.link, answer.mode) == (ANSWER, DOCS_URL, "define")
    assert tokens == ANSWER_CHUNKS
    messages = chats[0]["messages"]
    assert chats[0]["model"] == DEFAULTS["model"]
    assert messages[0]["content"] == DEFAULTS["modes"]["define"]["prompt"]
    assert "joins path segments" in messages[-1]["content"]
    assert session.history == [{"role": "user", "content": "Copied: os.path.join"},
                               {"role": "assistant", "content": ANSWER}]


def test_offline_mode_never_searches(monkeypatch, chats):
    monkeypatch.setattr(llm, "DDGS", fake_search(error=AssertionError("searched")))
    answer = Session().ask("sonder", {**DEFAULTS, "offline": True})
    assert answer.link == ""
    assert "offline" in chats[0]["messages"][-1]["content"]


def test_search_failure_and_unsafe_links_leave_no_link(monkeypatch, chats):
    monkeypatch.setattr(llm, "DDGS", fake_search(error=DDGSException("No results found.")))
    assert Session().ask("sonder", DEFAULTS).link == ""

    unsafe = [{"title": "x", "href": 'https://a.example/x"\tfile:///C:/win.ini', "body": "x"},
              {"title": "x", "href": "javascript:alert(1)", "body": "x"}]
    monkeypatch.setattr(llm, "DDGS", fake_search(unsafe))
    assert Session().ask("os.path.join", DEFAULTS).link == ""


def test_first_safe_link_skips_urls_that_dont_parse():
    assert llm.first_safe_link([{"href": "http://["}, {"href": DOCS_URL}]) == DOCS_URL
    assert llm.first_safe_link([{"href": "http://["}]) == ""


def test_search_query_is_capped(monkeypatch, chats):
    queries = []
    monkeypatch.setattr(llm, "DDGS", fake_search(RESULTS, queries=queries))
    Session().ask("y" * 1000, DEFAULTS)
    assert queries == ["y" * llm.MAX_QUERY_CHARS]


def test_compress_sees_the_conversation_as_one_transcript(chats):
    session = Session()
    session.ask("mutex", DEFAULTS)
    answer = session.ask("deadlock", COMPRESS)

    messages = chats[1]["messages"]
    assert answer.mode == "compress"
    assert [m["role"] for m in messages] == ["system", "user"]  # folded: no copied formats, strict templates OK
    assert messages[1]["content"].startswith(f"Conversation so far:\nCopied: mutex\n\n{ANSWER}")
    assert messages[1]["content"].endswith(llm.WHOLE_CONVERSATION)


def test_settings_edits_invalidate_the_cache(chats):
    session = Session()
    session.ask("bonjour", DEFAULTS, mode="translate")
    session.ask("bonjour", {**DEFAULTS, "language": "German"}, mode="translate")
    assert len(chats) == 2


def test_clearing_during_a_stream_discards_that_answer(monkeypatch):
    session = Session()

    def stream_then_clear(settings, model, messages):
        session.clear()  # e.g. the user cleared the context while this answer was streaming
        yield "late answer"

    monkeypatch.setattr(llm, "stream_chat", stream_then_clear)
    assert Session.ask(session, "x", {**DEFAULTS, "offline": True}).text == "late answer"
    assert session.history == []


def test_a_lookup_cancelled_before_it_starts_never_searches(monkeypatch, chats):
    monkeypatch.setattr(llm, "DDGS", fake_search(error=AssertionError("searched")))
    cancel = threading.Event()
    cancel.set()
    assert Session().ask("mutex", DEFAULTS, cancel=cancel).cancelled
    assert chats == []


def test_trimmed_keeps_whole_pairs():
    history = [{"n": n} for n in range(6)]
    assert llm.trimmed(history, 3) == history[-2:]
    assert llm.trimmed(history, 12) == history  # shorter than the limit: keep everything
    assert llm.trimmed(history, 0) == []
    assert llm.trimmed(history, -4) == []


def test_screenshots_go_to_the_vision_model_with_their_ocr_text(chats):
    session = Session()
    session.ask(Shot(b"png", "TypeError: 'NoneType' object is not subscriptable"), DEFAULTS)
    session.ask(Shot(b"png2", "ok"), DEFAULTS)

    first, second = chats[0]["messages"][-1], chats[1]["messages"][-1]
    assert chats[0]["model"] == DEFAULTS["vision_model"]
    assert first["images"] == [b"png"] and "OCR transcript" in first["content"]
    assert "OCR transcript" not in second["content"]  # too little text to be worth including
    assert session.history[0]["content"].startswith("[screenshot]")


def test_per_mode_model_and_language_placeholder(chats):
    modes = {**DEFAULTS["modes"], "translate": {**DEFAULTS["modes"]["translate"], "model": "custom:1b"}}
    Session().ask("bonjour", {**DEFAULTS, "modes": modes, "language": "German"}, mode="translate")
    assert chats[0]["model"] == "custom:1b"
    assert "German" in chats[0]["messages"][0]["content"] and "{language}" not in chats[0]["messages"][0]["content"]


def test_repeating_an_item_replays_the_answer_unless_the_mode_changed(chats):
    session, tokens = Session(), []
    session.ask("os.path.join", DEFAULTS)

    assert session.ask("os.path.join", DEFAULTS, on_token=tokens.append).text == ANSWER
    assert (len(chats), tokens) == (1, [ANSWER])
    session.ask("os.path.join", COMPRESS)
    assert len(chats) == 2


def test_cancel_stops_streaming_and_keeps_nothing(chats):
    cancel, session = threading.Event(), Session()

    def stop_after_first(chunk):
        cancel.set()

    answer = session.ask("os.path.join", DEFAULTS, on_token=stop_after_first, cancel=cancel)

    assert answer.cancelled and answer.text == ANSWER_CHUNKS[0].strip()
    assert session.history == []


def test_clear_and_trim(chats):
    session = Session()
    for n in range(DEFAULTS["context_messages"]):
        session.ask(f"term {n}", DEFAULTS)
    assert len(session.history) == DEFAULTS["context_messages"]
    session.clear()
    assert (session.history, session.turns) == ([], 0)


def test_inline_mode_prefix():
    assert llm.split_inline_mode(">>translate: bonjour", DEFAULTS["modes"]) == ("translate", "bonjour")
    assert llm.split_inline_mode(">>nope: bonjour", DEFAULTS["modes"]) == (None, ">>nope: bonjour")
    assert llm.split_inline_mode("plain", DEFAULTS["modes"]) == (None, "plain")


def test_openai_messages_turn_images_into_data_urls():
    converted = llm.openai_messages([{"role": "system", "content": "s"},
                                     {"role": "user", "content": "Screenshot", "images": [b"\x89PNG"]}])
    assert converted[0] == {"role": "system", "content": "s"}
    assert converted[1]["content"][0] == {"type": "text", "text": "Screenshot"}
    assert converted[1]["content"][1]["image_url"]["url"] == "data:image/png;base64,iVBORw=="


def test_stream_chat_uses_ollama_with_thinking_off_and_models_pinned(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, host, timeout=None):
            calls.append(host)

        def chat(self, **kwargs):
            calls.append(kwargs)
            return iter([SimpleNamespace(message=SimpleNamespace(content="a")),
                         SimpleNamespace(message=SimpleNamespace(content=None))])

    monkeypatch.setattr(llm.ollama, "Client", FakeClient)
    assert list(llm.stream_chat(DEFAULTS, "m", [])) == ["a", ""]
    assert calls[0] == DEFAULTS["ollama_host"]
    assert (calls[1]["think"], calls[1]["keep_alive"], calls[1]["stream"]) == (False, -1, True)
    assert calls[1]["options"]["num_ctx"] == llm.NUM_CTX


def test_stream_chat_supports_openai_compatible_servers(monkeypatch):
    seen = {}

    class FakeOpenAI:
        def __init__(self, base_url, api_key, timeout=None):
            seen.update(base_url=base_url, api_key=api_key)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            seen.update(kwargs)
            delta = SimpleNamespace(content="hi")
            return iter([SimpleNamespace(choices=[SimpleNamespace(delta=delta)]), SimpleNamespace(choices=[])])

    monkeypatch.setattr(llm, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("MY_KEY", "secret")
    config = {**DEFAULTS, "provider": "openai", "base_url": "http://localhost:1234/v1", "api_key_env": "MY_KEY"}

    assert list(llm.stream_chat(config, "gpt", [{"role": "user", "content": "x"}])) == ["hi"]
    assert (seen["base_url"], seen["api_key"], seen["model"], seen["stream"]) == (
        "http://localhost:1234/v1", "secret", "gpt", True)


def test_warm_up_pins_both_models_and_reports_failures(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, host):
            pass

        def generate(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(llm.ollama, "Client", FakeClient)
    assert llm.warm_up(DEFAULTS) == ""
    assert [(c["model"], c["keep_alive"]) for c in calls] == [(DEFAULTS["model"], -1), (DEFAULTS["vision_model"], -1)]
    assert calls[1]["images"]  # a real image warms the vision encoder too

    class Broken(FakeClient):
        def generate(self, **kwargs):
            raise ConnectionError("Failed to connect to Ollama.")

    monkeypatch.setattr(llm.ollama, "Client", Broken)
    assert llm.warm_up(DEFAULTS) == "Model not ready: Failed to connect to Ollama."
    assert llm.warm_up({**DEFAULTS, "provider": "openai"}) == ""
