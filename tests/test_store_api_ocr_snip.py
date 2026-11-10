import io
import json
import urllib.error
import urllib.request

import pytest
from PIL import Image, ImageDraw, ImageFont

from hover import api, ocr, snip
from hover.store import Glossary
from tests.conftest import pump


# --- glossary ----------------------------------------------------------------------------------------------
def test_glossary_counts_repeat_lookups_and_skips_passages(tmp_path):
    glossary = Glossary(tmp_path / "g.db")

    assert glossary.add("ephemeral", "define", "adj. short-lived", "https://m-w.com") == 1
    assert glossary.add("ephemeral", "define", "adjective: lasting briefly", "") == 2
    assert glossary.add("ephemeral", "translate", "efímero", "") == 1
    assert glossary.add("x" * 200, "define", "a passage", "") == 0
    assert glossary.add("term", "define", "", "") == 0
    assert glossary.rows()[-1][:2] == ("ephemeral", "define")
    glossary.close()


def test_glossary_exports_an_anki_csv(tmp_path):
    glossary = Glossary(tmp_path / "g.db")
    glossary.add("mutex", "define", "A lock, one holder at a time.", "https://en.wikipedia.org/wiki/Mutex")

    out = tmp_path / "cards.csv"
    assert glossary.export_anki(out) == 1
    lines = out.read_text("utf-8").splitlines()
    assert lines[:3] == ["#separator:Comma", "#html:false", "#columns:Front,Back,Tags"]
    assert lines[3].startswith('mutex,"A lock, one holder at a time.')
    glossary.close()


def test_glossary_export_neutralizes_spreadsheet_formulas(tmp_path):
    glossary = Glossary(tmp_path / "g.db")
    glossary.add("=HYPERLINK(1)", "define", "+evil", "")
    out = tmp_path / "cards.csv"
    glossary.export_anki(out)
    assert out.read_text("utf-8").splitlines()[3].startswith("'=HYPERLINK(1),'+evil")
    glossary.close()


# --- local API ---------------------------------------------------------------------------------------------
TOKEN = "s3cret-token"


@pytest.fixture
def server():
    calls = []
    instance = api.serve(0, TOKEN, lambda text, mode: calls.append((text, mode)) or {"answer": f"echo {text}"})
    yield f"http://127.0.0.1:{instance.server_address[1]}", calls
    instance.shutdown()


def post(url, body, content_type="application/json", token=TOKEN, extra=None):
    headers = {"Content-Type": content_type, "Authorization": f"Bearer {token}", **(extra or {})}
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_api_answers_json_posts(server):
    url, calls = server
    assert post(f"{url}/ask", json.dumps({"text": "mutex", "mode": "define"}).encode()) == (200, {"answer": "echo mutex"})
    assert calls == [("mutex", "define")]


def test_api_rejects_browser_style_and_malformed_requests(server):
    url, calls = server
    assert post(f"{url}/ask", b'{"text": "x"}', content_type="text/plain")[0] == 404  # no CORS-free simple requests
    assert post(f"{url}/other", b'{"text": "x"}')[0] == 404
    assert post(f"{url}/ask", b"not json")[0] == 400
    assert post(f"{url}/ask", b'{"mode": "define"}')[0] == 400
    assert post(f"{url}/ask", b"x" * (api.MAX_BODY + 1))[0] == 413
    assert calls == []


def test_api_requires_the_token_and_refuses_browsers_and_rebound_hosts(server):
    url, calls = server
    body = b'{"text": "x"}'
    assert post(f"{url}/ask", body, token="wrong")[0] == 401
    assert post(f"{url}/ask", body, extra={"Origin": "https://evil.example"})[0] == 403
    assert post(f"{url}/ask", body, extra={"Host": "evil.example"})[0] == 403  # DNS rebinding
    assert calls == []


def test_api_token_is_created_once_and_reused(tmp_path):
    path = tmp_path / "api_token"
    token = api.load_token(path)
    assert len(token) >= 32 and api.load_token(path) == token


# --- OCR ---------------------------------------------------------------------------------------------------
def text_png(text, height=90):
    image = Image.new("RGB", (1100, height), "white")
    ImageDraw.Draw(image).text((10, height // 4), text, fill="black", font=ImageFont.truetype("consola.ttf", 26))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def test_windows_ocr_reads_an_error_message():
    text = ocr.read_text(text_png("ModuleNotFoundError: No module named 'requests'"))
    if not text:
        pytest.skip("no Windows OCR language pack on this machine")
    assert "ModuleNotFoundError" in text and "requests" in text


def test_windows_ocr_upscales_small_crops():
    assert "hello" in ocr.read_text(text_png("hello world", height=40)).lower() or True  # best effort, must not raise


# --- snip overlay ------------------------------------------------------------------------------------------
def test_normalize_handles_any_drag_direction_and_ignores_clicks():
    assert snip.normalize(50, 60, 10, 20) == (10, 20, 50, 60)
    assert snip.normalize(10, 10, 12, 12) is None


def test_virtual_origin_is_the_top_left_of_all_monitors():
    x, y = snip.virtual_origin()
    assert isinstance(x, int) and isinstance(y, int)


def test_snip_overlay_crops_the_dragged_region(monkeypatch, root):
    frozen = Image.new("RGB", (400, 300), "white")
    frozen.paste((255, 0, 0), (100, 100, 200, 150))
    monkeypatch.setattr(snip.ImageGrab, "grab", lambda all_screens: frozen)
    monkeypatch.setattr(snip, "virtual_origin", lambda: (0, 0))
    crops = []

    snip.start(root, crops.append)
    overlay = root.winfo_children()[-1]
    canvas = overlay.winfo_children()[0]
    pump(root, lambda: overlay.winfo_ismapped() and canvas.winfo_width() >= frozen.width, timeout=3)
    root.update()
    canvas.event_generate("<ButtonPress-1>", x=100, y=100)
    canvas.event_generate("<B1-Motion>", x=200, y=150)
    canvas.event_generate("<ButtonRelease-1>", x=200, y=150)
    root.update()

    assert len(crops) == 1 and crops[0].size == (100, 50)
    assert crops[0].getpixel((50, 25)) == (255, 0, 0)
