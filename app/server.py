"""stdlib-only HTTP server hosting the review page and JSON API."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import api
from .webui import PAGE

MAX_BODY = 128 * 1024  # 64 KiB class -> <= ~88 KiB base64 plus JSON frame


class Handler(BaseHTTPRequestHandler):
    server_version = "DiagVerifier/1.0"

    def log_message(self, fmt, *args):  # quiet, structured enough for compose
        pass

    def _send(self, status, ctype, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE)
        elif path == "/healthz":
            self._send(200, "application/json",
                       json.dumps({"status": "ok"}).encode("utf-8"))
        else:
            self._send(404, "application/json",
                       json.dumps({"error": "not found"}).encode("utf-8"))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/verify":
            self._send(404, "application/json",
                       json.dumps({"error": "not found"}).encode("utf-8"))
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            self._send(400, "application/json",
                       json.dumps(api._reject(0, "empty request body"))
                       .encode("utf-8"))
            return
        if length > MAX_BODY:
            self._send(413, "application/json",
                       json.dumps(api._reject(
                           length, "request body too large "
                                   "(class limit is 64 KiB)"))
                       .encode("utf-8"))
            return
        body = self.rfile.read(length)
        status, result = api.handle_api(body)
        self._send(status, "application/json",
                   json.dumps(result).encode("utf-8"))


def make_server(host, port):
    return ThreadingHTTPServer((host, port), Handler)
