from hover import ui
from hover.ui import Popup


def test_segments_parse_light_markdown():
    assert ui.segments("Use `os.path.join` for **portable** paths") == [
        ("Use ", ""), ("os.path.join", "code"), (" for ", ""), ("portable", "bold"), (" paths", "")]
    assert ui.segments("```python\nprint(1)\n```") == [("print(1)\n", "code")]
    assert ui.segments("- one\n- two") == [("• one\n• two", "")]


def test_fit_flips_at_monitor_edges_and_stays_inside():
    area = (0, 0, 1920, 1040)
    assert ui.fit(100, 100, 400, 200, area) == (100, 100)
    assert ui.fit(1800, 100, 400, 200, area) == (1800 - 400 - 2 * ui.CURSOR_OFFSET, 100)
    assert ui.fit(100, 1000, 400, 200, area) == (100, 1000 - 200 - 2 * ui.CURSOR_OFFSET)
    assert ui.fit(-3000, 100, 400, 200, (-1920, 0, 0, 1080)) == (-1920, 100)  # monitor left of the main one


def test_footer_shows_privacy_badge_and_counts():
    assert ui.footer_text("https://docs.python.org/x", "define", 1, 1) == "web · docs.python.org ↗ · define"
    assert ui.footer_text("", "compress", 3, 2) == "local · compress · 3 in context · looked up 2×"


def test_work_area_is_a_rectangle():
    left, top, right, bottom = ui.work_area(0, 0)
    assert right > left and bottom > top


def make_popup(root, events):
    return Popup(root, on_follow_up=lambda q: events.append(("follow", q)),
                 on_replace=lambda t: events.append(("replace", t)), on_hide=lambda: events.append(("hide",)),
                 on_cycle=lambda: events.append(("cycle",)))


def text_of(popup):
    return popup.body.get("1.0", "end-1c")


def test_popup_streams_then_renders_and_ignores_stale_requests(root):
    events = []
    popup = make_popup(root, events)
    assert root.state() == "withdrawn"

    popup.show("os.path.join", request=1)
    assert root.state() == "normal" and text_of(popup) == ui.LOADING
    popup.append(1, "Use `join`")
    popup.append(2, "stale chunk")
    assert text_of(popup) == "Use `join`"

    popup.finish(1, "Use `join`", "web · docs.python.org ↗ · define", "https://docs.python.org")
    assert text_of(popup) == "Use join"
    assert popup.body.tag_names("1.5") == ("code",)
    assert popup.status["text"].startswith("web") and popup.url == "https://docs.python.org"
    popup.finish(0, "older answer", "", "")
    assert text_of(popup) == "Use join"


def test_popup_long_titles_are_truncated(root):
    popup = make_popup(root, [])
    popup.show("x" * 200, request=1)
    assert popup.title["text"] == "x" * ui.MAX_TITLE_CHARS + "…"


def test_popup_buttons_copy_replace_pin_and_follow_up(root):
    events = []
    popup = make_popup(root, events)
    popup.show("term", request=1)
    popup.finish(1, "the answer", "local · define", "")

    popup.copy()
    assert root.clipboard_get() == "the answer" and popup.status["text"] == "copied"

    popup.toggle_pin()
    assert popup.pinned and popup.pin_button["text"] == "Unpin" and popup._timer is None
    assert not root.overrideredirect() and not root.attributes("-topmost")  # a normal window: Snap works
    popup.toggle_pin()
    assert root.overrideredirect() and root.attributes("-topmost")

    popup.entry.insert(0, "  what about threads? ")
    popup._follow_up(None)
    assert ("follow", "what about threads?") in events and popup.entry.get() == ""

    popup.replace()
    assert events[-2:] == [("hide",), ("replace", "the answer")]
    assert root.state() == "withdrawn"


def test_popup_never_replaces_with_errors_or_half_streamed_answers(root):
    events = []
    popup = make_popup(root, events)
    popup.show("term", request=1)
    popup.append(1, "partial")
    popup.replace()
    popup.finish(1, "Error: Failed to connect to Ollama.", "error", "")
    popup.replace()
    assert events == []


def test_popup_notice_and_empty_answer(root):
    popup = make_popup(root, [])
    popup.notice("Hover", "Context cleared.")
    assert text_of(popup) == "Context cleared."
    popup.show("t", request=9)
    popup.finish(9, "", "local", "")
    assert text_of(popup) == "(empty answer)"


def test_popup_shift_tab_cycles_and_the_close_button_hides(root):
    events = []
    popup = make_popup(root, events)
    popup.show("term", request=1, status="define · Shift+Tab: next mode")
    assert popup.status["text"] == "define · Shift+Tab: next mode"
    root.event_generate("<Shift-Tab>")
    root.update()
    assert ("cycle",) in events
    assert root.protocol("WM_DELETE_WINDOW")  # the X button is bound (to hide), not left to destroy the root


def test_a_moved_card_stays_put_while_streaming(root):
    popup = make_popup(root, [])
    popup.show("term", request=1)
    popup.moved = True
    root.geometry("+40+40")
    root.update()
    popup.append(1, "more text " * 30)
    root.update()
    assert (root.winfo_x(), root.winfo_y()) == (40, 40)
    popup.show("next", request=2)
    assert not popup.moved  # a new lookup appears at the cursor again


def test_mic_starts_windows_voice_typing_in_the_box(root, monkeypatch):
    started = []
    monkeypatch.setattr(ui, "start_dictation", lambda: started.append(True))
    popup = make_popup(root, [])
    popup.show("term", request=1)
    popup.dictate()
    assert started and popup.holding_open
    popup._hide_if_unfocused()
    assert root.state() == "normal"  # focus going to the voice typing panel doesn't hide the card
