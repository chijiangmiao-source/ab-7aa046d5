"""HTTP API + static page for the flight-control lock arbitration service.

Endpoints
---------
GET  /health                       liveness probe
GET  /                             web console
GET  /static/*                     static assets
POST /api/verdicts                 submit (freeze) / replay / conflict
GET  /api/verdicts                 list frozen verdicts
GET  /api/verdicts/<auditId>       re-read a frozen verdict
"""

from __future__ import annotations

import json
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import ValidationError, VerdictStore, normalize  # noqa: E402

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY_BYTES = 2_000_000

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

store = VerdictStore()


class Handler(BaseHTTPRequestHandler):
    server_version = "LockArbiter/1.0"

    def log_message(self, fmt: str, *args) -> None:  # keep container logs tidy
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ------------------------------------------------------------------ #
    def _send_json(self, obj: dict | list, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel: str) -> None:
        # Serve index.html for "/", otherwise constrain to the static dir.
        if rel in ("", "/"):
            rel = "index.html"
        rel = rel.removeprefix("/static/").lstrip("/")
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR + os.sep) or not os.path.isfile(path):
            self._send_json({"error": "not_found", "message": "页面资源不存在"}, 404)
            return
        ext = os.path.splitext(path)[1]
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send_json({"error": "not_found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/health":
            self._send_json({"status": "ok", "service": "flight-control-lock-arbiter"})
        elif path == "/api/verdicts":
            self._send_json({"verdicts": store.list()})
        elif path.startswith("/api/verdicts/"):
            audit_id = unquote(path[len("/api/verdicts/") :])
            record = store.get(audit_id)
            if record is None:
                self._send_json(
                    {"error": "not_found", "message": f"审计标识 {audit_id} 无冻结裁决"}, 404
                )
            else:
                self._send_json(record)
        elif path.startswith("/api/"):
            self._send_json({"error": "not_found", "message": "未知 API"}, 404)
        else:
            self._send_static(path if path != "/" else "")

    def do_POST(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path != "/api/verdicts":
            self._send_json({"error": "not_found", "message": "未知 API"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_json({"error": "bad_request", "message": "缺少请求体"}, 400)
            return
        if length > MAX_BODY_BYTES:
            self._send_json({"error": "bad_request", "message": "请求体过大"}, 413)
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "bad_request", "message": "请求体不是合法 JSON"}, 400)
            return
        try:
            sub = normalize(payload)
        except ValidationError as exc:
            self._send_json({"error": "bad_request", "message": str(exc)}, 400)
            return
        record, status = store.submit(sub)
        self._send_json(record, status)


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"flight-control lock arbiter listening on {host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
