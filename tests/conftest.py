import tkinter as tk

import pytest

from hover import settings

DOCS_URL = "https://docs.python.org/3/library/os.path.html"
RESULTS = [
    {"title": "os.path — pathname functions", "href": DOCS_URL, "body": "os.path.join(path, *paths) joins path segments."},
    {"title": "Tutorial", "href": "https://example.com/join", "body": "How to join paths."},
]


@pytest.fixture(autouse=True)
def settings_file(monkeypatch, tmp_path):
    """Tests never read or write the real %APPDATA%\\Hover\\settings.json."""
    path = tmp_path / "Hover" / "settings.json"
    monkeypatch.setattr(settings, "SETTINGS_FILE", path)
    return path


@pytest.fixture(scope="session")
def tk_interpreter():
    """One Tk for the whole run, like the app. Creating many interpreters is flaky on Windows (init.tcl races), and
    a dead one collected on a worker thread makes Tcl abort the process."""
    interpreter = tk.Tk()
    interpreter.withdraw()
    yield interpreter
    interpreter.destroy()


@pytest.fixture
def root(tk_interpreter):
    """A fresh top-level window per test (Popup and the snip overlay work on any toplevel)."""
    window = tk.Toplevel(tk_interpreter)
    yield window
    window.destroy()


def fake_search(results=(), error=None, queries=None):
    class FakeDDGS:
        def text(self, query, max_results):
            if queries is not None:
                queries.append(query)
            if error:
                raise error
            return list(results)

    return FakeDDGS


def pump(root, until, timeout=5.0):
    """Run the real Tk mainloop until `until()` is true. Worker threads can only hand work to Tk (via after())
    while the main thread is inside mainloop, exactly as in the running app."""
    import time

    deadline = time.monotonic() + timeout

    def check():
        if until() or time.monotonic() > deadline:
            root.quit()
        else:
            root.after(20, check)

    root.after(0, check)
    root.mainloop()
    return until()
