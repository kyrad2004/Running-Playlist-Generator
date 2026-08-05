"""Shared OAuth plumbing: PKCE helpers and a one-shot loopback callback server.

Both Spotify and Strava use the Authorization Code flow with a browser redirect
back to a local URL. Rather than making you paste a code out of the address bar,
we run a tiny HTTP server that catches the redirect and reads the code directly.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


class OAuthError(RuntimeError):
    """Raised when the authorization step fails or is denied."""


def generate_state() -> str:
    """Opaque value echoed back by the provider, used to detect a mismatched
    or forged callback."""
    return secrets.token_urlsafe(24)


def generate_code_verifier() -> str:
    """PKCE verifier: 43-128 chars from the unreserved character set."""
    return secrets.token_urlsafe(64)[:128]


def code_challenge_s256(verifier: str) -> str:
    """PKCE S256 challenge: base64url(sha256(verifier)), no padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


_SUCCESS_PAGE = b"""<!doctype html><meta charset="utf-8">
<title>Authorized</title>
<style>
 body{font-family:system-ui,-apple-system,sans-serif;background:#0f1115;color:#e7e9ee;
      display:grid;place-items:center;height:100vh;margin:0}
 .card{text-align:center;padding:2.5rem 3rem;border:1px solid #262b36;border-radius:14px;background:#161a22}
 h1{font-size:1.25rem;margin:0 0 .5rem}
 p{margin:0;color:#98a2b3;font-size:.9rem}
</style>
<div class="card"><h1>&#10003; Authorized</h1><p>You can close this tab and return to your terminal.</p></div>
"""

_FAILURE_PAGE = b"""<!doctype html><meta charset="utf-8">
<title>Authorization failed</title>
<style>
 body{font-family:system-ui,-apple-system,sans-serif;background:#0f1115;color:#e7e9ee;
      display:grid;place-items:center;height:100vh;margin:0}
 .card{text-align:center;padding:2.5rem 3rem;border:1px solid #4a2530;border-radius:14px;background:#1f151a}
</style>
<div class="card"><h1>Authorization failed</h1><p>Check your terminal for details.</p></div>
"""


@dataclass
class CallbackResult:
    params: dict[str, str]

    @property
    def code(self) -> str | None:
        return self.params.get("code")

    @property
    def state(self) -> str | None:
        return self.params.get("state")

    @property
    def error(self) -> str | None:
        return self.params.get("error")


class _CallbackHandler(BaseHTTPRequestHandler):
    server: "_CallbackServer"  # type: ignore[assignment]

    def do_GET(self) -> None:  # noqa: N802 (name fixed by BaseHTTPRequestHandler)
        parsed = urlparse(self.path)
        if parsed.path != self.server.expected_path:
            self.send_response(404)
            self.end_headers()
            return

        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.server.result = CallbackResult(params)

        body = _FAILURE_PAGE if params.get("error") else _SUCCESS_PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """Silence the default stderr access log."""


class _CallbackServer(HTTPServer):
    def __init__(self, address, handler, expected_path: str) -> None:
        super().__init__(address, handler)
        self.expected_path = expected_path
        self.result: CallbackResult | None = None


def wait_for_callback(
    redirect_uri: str,
    authorize_url: str,
    timeout_seconds: int = 300,
    open_browser: bool = True,
) -> CallbackResult:
    """Open the consent page and block until the provider redirects back.

    Binds to the host and port in `redirect_uri`, so that URI must point at a
    loopback address you can actually listen on.
    """
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"

    try:
        server = _CallbackServer((host, port), _CallbackHandler, path)
    except OSError as exc:
        raise OAuthError(
            f"Could not listen on {host}:{port} for the OAuth callback ({exc}). "
            "Another process may be using that port — change the redirect URI in .env "
            "and update it on the provider's app settings to match."
        ) from exc

    server.timeout = timeout_seconds
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("\nOpening your browser to authorize. If it doesn't open, paste this URL:\n")
    print(f"  {authorize_url}\n")
    if open_browser:
        try:
            webbrowser.open(authorize_url)
        except Exception:
            pass  # headless box; the printed URL is the fallback

    thread.join(timeout=timeout_seconds + 5)
    result = server.result
    server.server_close()

    if result is None:
        raise OAuthError(
            f"Timed out after {timeout_seconds}s waiting for the OAuth redirect. "
            "Make sure the redirect URI registered with the provider matches "
            f"{redirect_uri} exactly."
        )
    if result.error:
        raise OAuthError(f"Authorization denied by provider: {result.error}")
    if not result.code:
        raise OAuthError(f"Callback arrived without an authorization code: {result.params}")
    return result
