"""Make Spotify match the plan, without ever creating a playlist twice.

Which Spotify playlist stands for which planned one is kept in
data/spotify-state.json, keyed by the Music persistent ID (or the extra
playlist's key), so renaming a playlist in Music or switching the name style
edits the same Spotify playlist. Only playlists recorded there are touched.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from .plan import PlannedPlaylist
from .spotify import SpotifyClient

CREATED = "created"
RECREATED = "recreated (was removed from Your Library)"
UPDATED = "tracks updated"
RENAMED = "renamed"
DESCRIBED = "description updated"
UNCHANGED = "unchanged"


def _fingerprint(uris: list[str]) -> str:
    return hashlib.sha256("\n".join(uris).encode()).hexdigest()


class SyncState:
    def __init__(self, path: Path):
        self.path = path
        self.playlists: dict[str, dict] = {}
        if path.exists():
            self.playlists = json.loads(path.read_text(encoding="utf-8")).get("playlists", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"playlists": self.playlists}, ensure_ascii=False, indent=1),
                             encoding="utf-8")


def sync(plan: list[PlannedPlaylist], client: SpotifyClient, state: SyncState, style: str,
         progress: Callable[[str, str], None] | None = None) -> dict[str, str]:
    followed = {p["id"] for p in client.my_playlists() if p}
    actions: dict[str, str] = {}
    for planned in plan:
        name, description = planned.display_name(style), planned.description
        fingerprint = _fingerprint(planned.uris)
        entry = state.playlists.get(planned.key)
        done = []
        if entry is None or entry["id"] not in followed:
            created = client.create_playlist(name, description, public=False)
            done.append(CREATED if entry is None else RECREATED)
            entry = {"id": created["id"]}
            # Saved before the tracks go in: a crash now must not create it again.
            state.playlists[planned.key] = entry
            state.save()
            if planned.uris:
                client.set_playlist_items(entry["id"], planned.uris)
        else:
            if (entry.get("name"), entry.get("description")) != (name, description):
                client.change_playlist(entry["id"], name, description)
                done.append(RENAMED if entry.get("name") != name else DESCRIBED)
            if entry.get("fingerprint") != fingerprint:
                client.set_playlist_items(entry["id"], planned.uris)
                done.append(UPDATED)
        entry.update(name=name, description=description, fingerprint=fingerprint,
                     tracks=len(planned.uris))
        state.save()
        actions[planned.key] = ", ".join(done) or UNCHANGED
        if progress:
            progress(name, actions[planned.key])
    return actions
