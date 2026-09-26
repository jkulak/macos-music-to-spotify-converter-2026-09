"""Log in to Spotify with Authorization Code + PKCE.

PKCE needs no client secret, so the only thing configured is the public client
ID. Spotify accepts plain-HTTP redirects only to a loopback IP literal (never
"localhost"), hence http://127.0.0.1:8888/callback.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from .spotify import NotAuthorised, _post_form

SCOPES = ("playlist-read-private", "playlist-modify-private", "playlist-modify-public")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def new_pkce() -> tuple[str, str]:
    """A code verifier and its S256 challenge."""
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def authorize_url(accounts_base: str, client_id: str, redirect_uri: str,
                  challenge: str, state: str) -> str:
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
        "scope": " ".join(SCOPES),
    })
    return f"{accounts_base.rstrip('/')}/authorize?{query}"


def wait_for_code(listen_host: str, redirect_uri: str, state: str, timeout: float = 600) -> str:
    """Serve the redirect URI once and return the authorisation code it carries."""
    target = urllib.parse.urlparse(redirect_uri)
    received: dict[str, str] = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server naming)
            url = urllib.parse.urlparse(self.path)
            if url.path != target.path:
                self.send_error(404)
                return
            received.update({k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()})
            ok = "code" in received and received.get("state") == state
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Logged in, you can close this tab." if ok
                             else b"Login failed, see the terminal.")

        def log_message(self, *args):
            pass

    server = HTTPServer((listen_host, target.port or 80), Callback)
    deadline = time.monotonic() + timeout
    try:
        # Browsers also ask for /favicon.ico; keep serving until the redirect comes.
        while not received:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NotAuthorised("no redirect from Spotify arrived in time")
            server.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()
    if received.get("state") != state:
        raise NotAuthorised("the redirect carried a different state; start `make auth` again")
    if "code" not in received:
        raise NotAuthorised(f"Spotify refused the login: {received.get('error', 'unknown error')}")
    return received["code"]


def exchange_code(accounts_base: str, client_id: str, code: str, redirect_uri: str,
                  verifier: str) -> dict:
    return _post_form(f"{accounts_base.rstrip('/')}/api/token", {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    })
