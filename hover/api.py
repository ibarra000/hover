"""Optional local trigger for scripts, AutoHotkey, or a Stream Deck.

POST http://127.0.0.1:<api_port>/ask with headers `Content-Type: application/json` and
`Authorization: Bearer <token from %APPDATA%\\Hover\\api_token>`, body {"text": ..., "mode": ...}.
"""

import hmac
import json
import secrets
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BODY = 64 * 1024
REQUEST_TIMEOUT = 10  # seconds a client may take to send its request


def load_token(path: Path) -> str:
    """The shared secret callers must send; created on first use, readable only by the user's own account."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(32), "utf-8")
    return path.read_text("utf-8").strip()


def make_handler(ask: Callable[[str, str | None], dict], port: int, token: str) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT

        def do_POST(self) -> None:  # noqa: N802 (http.server naming)
            # Browsers always send Origin on cross-site POSTs, and a DNS-rebound page has a foreign Host header,
            # so both are refused; the bearer token stops any other local caller that doesn't know the secret.
            if self.headers.get("Origin") or self.headers.get("Host") not in allowed_hosts:
                return self._reply(403, {"error": "forbidden"})
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                return self._reply(401, {"error": "missing or wrong bearer token"})
            if self.path != "/ask" or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._reply(404, {"error": "POST /ask with Content-Type: application/json"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._reply(400, {"error": "bad Content-Length"})
            if not 0 < length <= MAX_BODY:
                return self._reply(413, {"error": f"body must be 1..{MAX_BODY} bytes"})
            try:
                body = json.loads(self.rfile.read(length))
                text, mode = str(body["text"]), body.get("mode")
            except (ValueError, KeyError, TypeError):
                return self._reply(400, {"error": "body must be JSON like {\"text\": \"...\", \"mode\": \"define\"}"})
            self._reply(200, ask(text, mode if isinstance(mode, str) else None))

        def _reply(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:  # silence per-request logging (no console under pythonw)
            pass

    return Handler


def serve(port: int, token: str, ask: Callable[[str, str | None], dict]) -> ThreadingHTTPServer:
    """Start the API on 127.0.0.1 only, in a background thread."""
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(ask, port, token))
    if port == 0:  # tests ask for any free port; allow the one we actually got
        server.RequestHandlerClass = make_handler(ask, server.server_address[1], token)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
