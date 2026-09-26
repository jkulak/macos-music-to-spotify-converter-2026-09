"""Synthetic library snapshots shaped like the exporter's output.

Names are invented on purpose: no real listening history belongs in the repo.
"""

from __future__ import annotations

import itertools

_ids = itertools.count(1)


def track_id() -> str:
    return f"{next(_ids):016X}"


def track(id: str | None = None, **fields) -> dict:
    raw = {
        "id": id or track_id(),
        "class": "fileTrack",
        "kind": "MPEG audio file",
        "mediaKind": "song",
        "name": "Paper Lanterns",
        "artist": "The Quiet Engines",
        "albumArtist": "The Quiet Engines",
        "album": "Harbour Lights",
        "composer": "",
        "genre": "Rock",
        "year": 2009,
        "duration": 201.5,
        "trackNumber": 3,
        "trackCount": 11,
        "discNumber": 1,
        "discCount": 1,
        "compilation": False,
        "plays": 12,
        "lastPlayed": "2012-05-04T20:11:09.000Z",
        "skips": 0,
        "rating": 0,
        "ratingKind": "computed",
        "albumRating": 0,
        "favorited": False,
        "disliked": False,
        "dateAdded": "2022-02-06T14:21:55.000Z",
        "bpm": 120,
        "cloudStatus": "unknown",
    }
    raw.update(fields)
    return raw


def playlist(name: str, kind: str = "regular", parent: str | None = None,
             tracks: list[dict] | None = None, id: str | None = None,
             position: int = 0) -> dict:
    return {
        "id": id or track_id(),
        "name": name,
        "kind": kind,
        "class": "folderPlaylist" if kind == "folder" else "userPlaylist",
        "parentId": parent,
        "position": position,
        "description": "",
        "trackIds": [t["id"] for t in tracks or []],
    }


def snapshot(tracks: list[dict], playlists: list[dict] = (), warnings: list[str] = ()) -> dict:
    ordered = [dict(p, position=i + 1) for i, p in enumerate(playlists)]
    return {
        "format": 1,
        "exportedAt": "2026-09-26T18:00:00.000Z",
        "source": {"app": "Music", "version": "1.7.1"},
        "tracks": list(tracks),
        "playlists": ordered,
        "warnings": list(warnings),
    }
