"""Serve built frontend assets and proxy /api to the local FastAPI server."""
from __future__ import annotations

import argparse
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class FrontendProxy(SimpleHTTPRequestHandler):
    api_target = "http://127.0.0.1:8000"

    def _proxy(self) -> None:
        target = self.api_target + self.path
        body = None
        if self.command in {"POST", "PUT", "PATCH"}:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else None

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "content-length", "connection"}
        }
        req = Request(target, data=body, headers=headers, method=self.command)
        try:
            with urlopen(req, timeout=30) as resp:
                payload = resp.read()
                self.send_response(resp.status)
                for key, value in resp.headers.items():
                    if key.lower() not in {"transfer-encoding", "connection"}:
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)
        except HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            for key, value in exc.headers.items():
                if key.lower() not in {"transfer-encoding", "connection"}:
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path.startswith("/api/"):
            self._proxy()
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path.startswith("/api/"):
            self._proxy()
            return
        self.send_error(404)

    def do_PUT(self) -> None:
        self.do_POST()

    def do_PATCH(self) -> None:
        self.do_POST()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--dist", default=os.path.join("frontend", "dist"))
    args = parser.parse_args()

    os.chdir(args.dist)
    httpd = ThreadingHTTPServer((args.host, args.port), FrontendProxy)
    print(f"Frontend proxy listening on http://{args.host}:{args.port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
