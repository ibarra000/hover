import json

from hover import settings


def test_first_load_writes_the_defaults(settings_file):
    assert settings.load() == settings.DEFAULTS
    assert json.loads(settings_file.read_text("utf-8")) == settings.DEFAULTS


def test_user_edits_override_defaults_and_missing_keys_fall_back(settings_file):
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(json.dumps({"temperature": 0.9, "hotkeys": {"snip": "<ctrl>+<alt>+x"}}), "utf-8")

    loaded = settings.load()

    assert loaded["temperature"] == 0.9
    assert loaded["model"] == settings.DEFAULTS["model"]
    assert loaded["hotkeys"] == {**settings.DEFAULTS["hotkeys"], "snip": "<ctrl>+<alt>+x"}


def test_v03_settings_are_migrated_and_saved(settings_file):
    settings_file.parent.mkdir(parents=True)
    old = {"hotkey": "<ctrl>+<shift>+h", "mode": "compress", "prompts": {"define": "D", "compress": "C"}}
    settings_file.write_text(json.dumps(old), "utf-8")

    loaded = settings.load()

    assert loaded["hotkeys"]["lookup"] == "<ctrl>+<shift>+h"
    assert loaded["modes"]["define"] == {"prompt": "D", "search": True}
    assert loaded["modes"]["compress"] == {"prompt": "C", "search": False}
    assert "translate" in loaded["modes"]  # new built-in modes are added
    saved = json.loads(settings_file.read_text("utf-8"))
    assert "prompts" not in saved and "hotkey" not in saved


def test_update_changes_one_key_and_keeps_the_rest(settings_file):
    settings.load()
    settings.update(mode="translate", offline=True)
    loaded = settings.load()
    assert (loaded["mode"], loaded["offline"], loaded["model"]) == ("translate", True, settings.DEFAULTS["model"])


def test_safe_load_reports_a_broken_file_and_falls_back(settings_file):
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text("{ not json", "utf-8")

    loaded, error = settings.safe_load()

    assert loaded == {**settings.DEFAULTS, "offline": True}  # fail closed: no web search on an unreadable file
    assert error.startswith("settings.json error:")

    settings_file.write_text('{"modes": null}', "utf-8")
    assert settings.safe_load()[1].startswith("settings.json error:")
