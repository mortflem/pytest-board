"""The HTTP server: authentication, the API routes, and the page (static/)."""
import base64
import hmac
import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from . import STATIC
from .model import ApiError


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pytest-board/1.0"

        def log_message(self, fmt, *args):
            if not self.path.startswith("/api/board"):
                sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def _authed(self):
            if not app.token:
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                    if hmac.compare_digest(pw, app.token):
                        return True
                except Exception:
                    pass
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="pytest-board"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _send(self, status, payload, ctype="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _cross_site(self, method):
            """
            Refuse requests another website makes the browser send (CSRF), and requests whose Host header
            doesn't fit a loopback-only bind (DNS rebinding). Returns an error message, or None when fine.
            Clients like curl send no Origin / Sec-Fetch-Site headers and are not affected.
            """
            host = (self.headers.get("Host") or "").strip().lower()
            if app.loopback_only:
                hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
                if hostname not in ("localhost", "127.0.0.1", "[::1]", "::1", ""):
                    return f"unexpected Host header {host!r} (server is bound to localhost)"
            if method == "GET":
                return None
            site = (self.headers.get("Sec-Fetch-Site") or "").lower()
            if site in ("cross-site", "same-site"):
                return "cross-site request refused"
            origin = self.headers.get("Origin")
            if origin is not None:
                o = urlparse(origin)
                if origin == "null" or not o.netloc or o.netloc.lower() != host:
                    return "cross-origin request refused"
            return None

        def _dispatch(self, method):
            bad = self._cross_site(method)
            if bad:
                return self._send(403, {"error": bad})
            if not self._authed():
                return
            url = urlparse(self.path)
            if not url.path.startswith("/api/"):
                return self._static(url.path)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            body = {}
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "invalid JSON"})
            for m, rx, fn in app.routes:
                mt = rx.match(url.path)
                if mt and m == method:
                    try:
                        res = fn(q, body, *(int(g) if g.isdigit() else g for g in mt.groups()))
                        if isinstance(res, tuple) and res[0] == "raw":
                            return self._send(200, res[1], res[2])
                        return self._send(200, res)
                    except ApiError as e:
                        return self._send(e.status, {"error": str(e)})
                    except (ValueError, TypeError, KeyError) as e:
                        return self._send(400, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def _static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            f = (STATIC / path.lstrip("/")).resolve()
            if STATIC not in f.parents or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
            self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler
