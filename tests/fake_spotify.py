"""A local stand-in for the Spotify accounts service and Web API.

It speaks the February 2026 dialect (POST /me/playlists, /playlists/{id}/items,
search limit 10) so the client is tested over real HTTP without an account.
"""

from __future__ import annotations

import itertools
import json
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from music2spotify.normalize import fold


@dataclass
class Recorded:
    method: str
    path: str
    query: dict
    body: object


@dataclass
class FakeSpotify:
    catalogue: dict[str, dict] = field(default_factory=dict)
    playlists: dict[str, dict] = field(default_factory=dict)
    requests: list[Recorded] = field(default_factory=list)
    failures: list[tuple[int, dict]] = field(default_factory=list)
    access_token: str = "access-1"
    refresh_token: str = "refresh-1"
    user_id: str = "tester"
    search_quota: int | None = None  # searches left before QUOTA_EXCEEDED

    def __post_init__(self):
        self._ids = itertools.count(1)
        self._tokens = itertools.count(2)
        self._server = None

    # --- test setup ----------------------------------------------------------

    def add_track(self, name: str, artists: list[str], album: str = "An Album",
                  duration_ms: int = 200_000, album_type: str = "album",
                  playable: bool = True) -> dict:
        track_id = f"{next(self._ids):022d}"
        album_id = "al" + fold(album + artists[0]).replace(" ", "")[:20]
        track = {
            "id": track_id, "uri": f"spotify:track:{track_id}", "name": name,
            "artists": [{"name": a} for a in artists],
            "album": {"id": album_id, "name": album, "album_type": album_type,
                      "release_date": "2010-01-01", "artists": [{"name": artists[0]}]},
            "duration_ms": duration_ms, "external_ids": {"isrc": f"XX{track_id[-10:]}"},
            "is_playable": playable,
        }
        self.catalogue[track_id] = track
        return track

    def fail_next(self, status: int, headers: dict | None = None) -> None:
        self.failures.append((status, headers or {}))

    def calls(self, method: str, prefix: str) -> list[Recorded]:
        return [r for r in self.requests if r.method == method and r.path.startswith(prefix)]

    # --- server --------------------------------------------------------------

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _body(self):
                size = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(size) if size else b""
                if self.headers.get("Content-Type", "").startswith("application/json") and raw:
                    return json.loads(raw)
                return dict(urllib.parse.parse_qsl(raw.decode())) if raw else None

            def _send(self, status: int, payload=None, headers: dict | None = None):
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, str(value))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str):
                url = urllib.parse.urlparse(self.path)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                body = self._body()
                fake.requests.append(Recorded(method, url.path, query, body))
                if url.path == "/api/token":
                    return self._send(200, fake._token(body))
                if fake.failures:
                    status, headers = fake.failures.pop(0)
                    return self._send(status, {"error": {"status": status}}, headers)
                if self.headers.get("Authorization") != f"Bearer {fake.access_token}":
                    return self._send(401, {"error": {"status": 401, "message": "expired"}})
                status, payload = fake._route(method, url.path, query, body)
                headers = {"Retry-After": 86_000} if status == 429 else None
                self._send(status, payload, headers)

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

            def do_PUT(self):  # noqa: N802
                self._handle("PUT")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        return f"http://127.0.0.1:{self._server.server_port}"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    # --- behaviour -----------------------------------------------------------

    def _token(self, form: dict) -> dict:
        if form.get("grant_type") == "refresh_token":
            assert form.get("refresh_token") == self.refresh_token, "wrong refresh token"
        self.access_token = f"access-{next(self._tokens)}"
        return {"access_token": self.access_token, "token_type": "Bearer", "expires_in": 3600,
                "refresh_token": self.refresh_token, "scope": "playlist-modify-private"}

    def _search(self, q: str) -> list[dict]:
        words = [w for w in fold(q.replace("track:", " ").replace("artist:", " ")).split()]
        hits = []
        for track in self.catalogue.values():
            haystack = fold(track["name"] + " " + " ".join(a["name"] for a in track["artists"]))
            if all(w in haystack.split() for w in words):
                hits.append(track)
        return hits

    def _search_albums(self, q: str) -> list[dict]:
        words = fold(q.replace("album:", " ").replace("artist:", " ")).split()
        albums = {}
        for track in self.catalogue.values():
            album = track["album"]
            haystack = fold(album["name"] + " " + album["artists"][0]["name"]).split()
            if all(w in haystack for w in words):
                total = sum(1 for t in self.catalogue.values() if t["album"]["id"] == album["id"])
                albums[album["id"]] = {**album, "total_tracks": total}
        return list(albums.values())

    def _route(self, method: str, path: str, query: dict, body):
        parts = path.strip("/").split("/")[1:]  # drop "v1"
        if parts == ["search"]:
            if query.get("market") == "from_token":
                # What Spotify answers without the user-read-private scope.
                return 403, {"error": {"status": 403, "message": "Insufficient client scope"}}
            limit = int(query.get("limit", 5))
            if limit > 10:
                return 400, {"error": {"status": 400, "message": "Invalid limit"}}
            if self.search_quota is not None:
                if self.search_quota <= 0:
                    self._quota_block = True
                    return 429, {"error": {"status": 429, "message": "Too many requests",
                                           "reason": "QUOTA_EXCEEDED"}}
                self.search_quota -= 1
            if query.get("type") == "album":
                return 200, {"albums": {"items": self._search_albums(query["q"])[:limit]}}
            return 200, {"tracks": {"items": self._search(query["q"])[:limit]}}
        if parts[:1] == ["albums"] and parts[2:] == ["tracks"]:
            listing = [dict((k, v) for k, v in t.items() if k != "album")
                       for t in self.catalogue.values() if t["album"]["id"] == parts[1]]
            return 200, {"items": listing, "next": None}
        if parts[:1] == ["tracks"] and len(parts) == 2:
            track = self.catalogue.get(parts[1])
            return (200, track) if track else (404, {"error": {"status": 404}})
        if parts == ["me"]:
            return 200, {"id": self.user_id, "display_name": "Test User"}
        if parts == ["me", "playlists"] and method == "GET":
            items = [{k: p[k] for k in ("id", "name", "description")} for p in self.playlists.values()]
            offset, limit = int(query.get("offset", 0)), int(query.get("limit", 20))
            page = items[offset:offset + limit]
            more = offset + limit < len(items)
            return 200, {"items": page, "next": "more" if more else None}
        if parts == ["me", "playlists"] and method == "POST":
            playlist_id = f"pl{next(self._ids):020d}"
            self.playlists[playlist_id] = {"id": playlist_id, "name": body["name"],
                                           "description": body.get("description", ""),
                                           "public": body.get("public", True), "uris": []}
            return 201, {k: v for k, v in self.playlists[playlist_id].items() if k != "uris"}
        if parts[:1] == ["playlists"]:
            playlist = self.playlists.get(parts[1])
            if playlist is None:
                return 404, {"error": {"status": 404, "message": "Not found"}}
            if len(parts) == 2 and method == "GET":
                return 200, {k: v for k, v in playlist.items() if k != "uris"}
            if len(parts) == 2 and method == "PUT":
                playlist.update({k: body[k] for k in ("name", "description") if k in body})
                return 200, None
            if parts[2:] == ["items"]:
                if method == "GET":
                    offset, limit = int(query.get("offset", 0)), int(query.get("limit", 20))
                    page = playlist["uris"][offset:offset + limit]
                    more = offset + limit < len(playlist["uris"])
                    return 200, {"items": [{"item": {"uri": u}} for u in page],
                                 "next": "more" if more else None}
                if len(body["uris"]) > 100:
                    return 400, {"error": {"status": 400, "message": "Too many items"}}
                if method == "PUT":
                    playlist["uris"] = list(body["uris"])
                else:
                    playlist["uris"] += body["uris"]
                return 200, {"snapshot_id": f"s{len(playlist['uris'])}"}
        return 404, {"error": {"status": 404, "message": f"no route {method} {path}"}}
