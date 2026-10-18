"""Read-only loopback dashboard. No database mutation or update registration."""

from __future__ import annotations

import json
import secrets
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer

from bubo.ui_export import build_data


def make_server(index: bytes, port: int = 0) -> HTTPServer:
    """Bind localhost; a per-launch secret URL prevents cross-site data reads."""
    token = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def do_GET(self) -> None:
            expected = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != expected:
                self.send_error(403)
                return
            if self.path == f"/{token}/":
                body, content_type = index, "text/html; charset=utf-8"
            elif self.path == f"/{token}/data.js":
                body, content_type = b"", "application/javascript"
            elif self.path == f"/{token}/data.json":
                try:
                    data = build_data()
                    data["meta"]["live"] = True
                    body = json.dumps(data, allow_nan=False).encode()
                    content_type = "application/json"
                except OSError, ValueError:
                    self.send_error(503, "Snapshot temporarily unavailable")
                    return
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass  # Do not persist the secret URL or local review data.

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.dashboard_url = f"http://127.0.0.1:{server.server_port}/{token}/"
    return server


def serve(index: bytes, port: int = 0) -> int:
    with make_server(index, port) as server:
        print(f"Bubo dashboard: {server.dashboard_url}\nPress Ctrl-C to stop.", flush=True)
        with suppress(KeyboardInterrupt):
            server.serve_forever()
    return 0
