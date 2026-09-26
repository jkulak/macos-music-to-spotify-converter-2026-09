"""A small Spotify Web API client on urllib, shaped by the February 2026 rules.

Development-mode apps get at most 10 search results per request, create
playlists with POST /me/playlists and edit them through /playlists/{id}/items.
Requests are paced and 429 responses honoured, because Spotify answers bursts
with Retry-After values that can run to hours.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

SEARCH_LIMIT = 10  # the development-mode maximum since February 2026
ITEMS_PER_REQUEST = 100


class SpotifyError(Exception):
    pass


class RateLimited(SpotifyError):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        hours = retry_after / 3600
        super().__init__(
            f"Spotify asks to wait {retry_after}s (~{hours:.1f}h) before the next request; "
            "everything fetched so far is cached, run the same command again later"
        )


class NotAuthorised(SpotifyError):
    pass


def _post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        with error:
            detail = error.read()[:300].decode(errors="replace")
        raise NotAuthorised(f"token request failed ({error.code}): {detail}") from None


class TokenStore:
    """The user's access and refresh tokens, kept in a file only the owner can read."""

    def __init__(self, path: Path, client_id: str, accounts_base: str,
                 clock: Callable[[], float] = time.time):
        self.path = path
        self.client_id = client_id
        self.accounts_base = accounts_base.rstrip("/")
        self.clock = clock

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            raise NotAuthorised("not logged in to Spotify; run `make auth` first") from None

    def save(self, response: dict) -> dict:
        previous = self.load() if self.path.exists() else {}
        token = {
            "access_token": response["access_token"],
            # Spotify may or may not rotate the refresh token; keep the old one if not.
            "refresh_token": response.get("refresh_token") or previous.get("refresh_token"),
            "scope": response.get("scope", previous.get("scope", "")),
            "expires_at": self.clock() + int(response.get("expires_in", 3600)),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(token, indent=2))
        os.chmod(self.path, 0o600)
        return token

    def refresh(self) -> str:
        token = self.load()
        if not token.get("refresh_token"):
            raise NotAuthorised("no refresh token stored; run `make auth` again")
        response = _post_form(f"{self.accounts_base}/api/token", {
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": self.client_id,
        })
        return self.save(response)["access_token"]

    def access_token(self) -> str:
        token = self.load()
        if token.get("expires_at", 0) - 60 <= self.clock():
            return self.refresh()
        return token["access_token"]


class SpotifyClient:
    def __init__(self, tokens: TokenStore, api_base: str = "https://api.spotify.com/v1",
                 min_interval: float = 0.35, max_retry_wait: int = 120, market: str = "PL",
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.tokens = tokens
        self.api_base = api_base.rstrip("/")
        self.min_interval = min_interval
        self.max_retry_wait = max_retry_wait
        self.market = market
        self.sleep = sleep
        self.clock = clock
        self.requests_made = 0
        self._last_request = float("-inf")

    def _pace(self) -> None:
        wait = self._last_request + self.min_interval - self.clock()
        if wait > 0:
            self.sleep(wait)
        self._last_request = self.clock()

    def request(self, method: str, path: str, params: dict | None = None,
                body: dict | None = None) -> dict | None:
        url = self.api_base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        refreshed = False
        last_error: object = "server errors"
        for attempt in range(6):
            self._pace()
            request = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.tokens.access_token()}",
                "Content-Type": "application/json",
            })
            self.requests_made += 1
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = response.read()
                    return json.loads(payload) if payload else None
            except urllib.error.HTTPError as error:
                with error:
                    status = error.code
                    retry_after = int(error.headers.get("Retry-After") or 5)
                    detail = error.read()[:300].decode(errors="replace")
                if status == 401 and not refreshed:
                    self.tokens.refresh()
                    refreshed = True
                    continue
                if status == 429:
                    if retry_after > self.max_retry_wait:
                        raise RateLimited(retry_after) from None
                    self.sleep(retry_after + 1)
                    continue
                if status >= 500:
                    last_error = f"HTTP {status}"
                    self.sleep(2 ** attempt)
                    continue
                raise SpotifyError(f"{method} {path} failed ({status}): {detail}") from None
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = error
                self.sleep(2 ** attempt)
                continue
        raise SpotifyError(f"{method} {path} kept failing; last error: {last_error}")

    # --- catalogue -----------------------------------------------------------

    def search_tracks(self, query: str, limit: int = SEARCH_LIMIT) -> list[dict]:
        result = self.request("GET", "/search", {
            "q": query, "type": "track", "limit": limit, "market": self.market,
        })
        return ((result or {}).get("tracks") or {}).get("items") or []

    def track(self, track_id: str) -> dict:
        return self.request("GET", f"/tracks/{track_id}", {"market": self.market})

    # --- the user's playlists ------------------------------------------------

    def me(self) -> dict:
        return self.request("GET", "/me")

    def my_playlists(self) -> list[dict]:
        playlists, offset = [], 0
        while True:
            page = self.request("GET", "/me/playlists", {"limit": 50, "offset": offset})
            playlists += page.get("items") or []
            if not page.get("next"):
                return playlists
            offset += 50

    def playlist(self, playlist_id: str) -> dict:
        return self.request("GET", f"/playlists/{playlist_id}",
                            {"fields": "id,name,description,owner(id),public"})

    def create_playlist(self, name: str, description: str, public: bool = False) -> dict:
        return self.request("POST", "/me/playlists",
                            body={"name": name, "description": description, "public": public})

    def change_playlist(self, playlist_id: str, name: str, description: str) -> None:
        self.request("PUT", f"/playlists/{playlist_id}",
                     body={"name": name, "description": description})

    def playlist_uris(self, playlist_id: str) -> list[str]:
        uris, offset = [], 0
        while True:
            page = self.request("GET", f"/playlists/{playlist_id}/items",
                                {"limit": 50, "offset": offset, "fields": "items(item(uri)),next"})
            uris += [(i.get("item") or {}).get("uri") for i in page.get("items") or []]
            if not page.get("next"):
                return [u for u in uris if u]
            offset += 50

    def set_playlist_items(self, playlist_id: str, uris: list[str]) -> None:
        """Replace the whole playlist: the first batch replaces, the rest append."""
        batches = [uris[i:i + ITEMS_PER_REQUEST] for i in range(0, len(uris), ITEMS_PER_REQUEST)]
        self.request("PUT", f"/playlists/{playlist_id}/items",
                     body={"uris": batches[0] if batches else []})
        for batch in batches[1:]:
            self.request("POST", f"/playlists/{playlist_id}/items", body={"uris": batch})
