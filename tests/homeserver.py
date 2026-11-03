"""A scripted Matrix homeserver on a loopback port, for real-HTTP tests.

Each route is ``(method, path regex) -> handler(request) -> (status, json,
headers)``. Every request is recorded, headers included, so a test can check
what the client actually put on the wire.
"""
import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

PREFIX = "/_matrix/client/v3"


@dataclass
class Request:
    method: str
    path: str
    raw_path: str
    query: Dict[str, List[str]]
    headers: Dict[str, str]
    body: Any = None
    match: Optional[re.Match] = field(default=None, repr=False)


Answer = Tuple[int, Any, Dict[str, str]]


class Homeserver:
    """``with Homeserver() as hs: hs.route("GET", "/account/whoami", ...)``."""

    def __init__(self):
        self.routes: List[Tuple[str, re.Pattern, Callable[[Request], Answer]]] = []
        self.requests: List[Request] = []
        self._lock = threading.Lock()
        homeserver = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                split = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                path = split.path
                request = Request(self.command, path[len(PREFIX):] if path.startswith(PREFIX) else path,
                                  split.path, parse_qs(split.query), dict(self.headers), body)
                with homeserver._lock:
                    homeserver.requests.append(request)
                status, payload, headers = 404, {"errcode": "M_UNRECOGNIZED"}, {}
                for method, pattern, handler in list(homeserver.routes):
                    found = pattern.fullmatch(request.path) if method == self.command else None
                    if found:
                        request.match = found
                        status, payload, headers = handler(request)
                        break
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = _serve

            def log_message(self, format, *args):
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def route(self, method: str, path: str, handler) -> None:
        """Answer ``method path`` (a regex, after the API prefix) with ``handler``.

        ``handler`` is a callable, or a fixed ``(status, json)`` / ``(status,
        json, headers)`` answer. Later routes take precedence.
        """
        if not callable(handler):
            fixed = handler if len(handler) == 3 else (*handler, {})
            handler = lambda request, fixed=fixed: fixed  # noqa: E731
        self.routes.insert(0, (method, re.compile(path), handler))

    def requests_to(self, method: str, path: str) -> List[Request]:
        pattern = re.compile(path)
        with self._lock:
            return [r for r in self.requests if r.method == method and pattern.fullmatch(r.path)]

    def __enter__(self) -> "Homeserver":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
