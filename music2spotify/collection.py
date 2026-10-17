"""The whole collection as one text file, one line per track.

The mp3s are gone once the archive is emptied, and the library snapshot is JSON only this repo
reads. This list outlives both: tab-separated, so grep finds a track by any word in it and a
spreadsheet opens it as a table.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from .archive import ON_SPOTIFY, ArchiveEntry
from .library import Library, Track
from .matching import MATCHED
from .pipeline import UNREVIEWED, MatchResult, Override
from .youtube import Found

FIELDS = ["artist", "title", "album", "album artist", "year", "genre", "time", "plays", "stars",
          "added", "last played", "playlists", "fate", "spotify", "youtube", "archived as"]
NOT_ARCHIVED = "not archived"
_BREAKS = re.compile(r"[\t\r\n]+")


def _time(seconds: float | None) -> str:
    if not seconds:
        return ""
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}:{rest:02d}"


def _playlists_of(library: Library) -> dict[str, list[str]]:
    names: dict[str, list[str]] = {}
    for playlist in library.playlists():
        name = " / ".join(playlist.path)
        for track_id in dict.fromkeys(playlist.track_ids):
            names.setdefault(track_id, []).append(name)
    return names


def _fate(track: Track, entry: ArchiveEntry | None, override: Override | None) -> str:
    if entry is not None:
        # Taken without review, Spotify may hold another version than the file was.
        if entry.why == ON_SPOTIFY and override is not None and override.note == UNREVIEWED:
            return f"{ON_SPOTIFY}, taken without review"
        return entry.why
    if not track.is_music:
        return f"not music ({track.non_music_reason()}), stayed in Music"
    return NOT_ARCHIVED


def _archived_as(entry: ArchiveEntry | None, archive_dir: Path) -> str:
    if entry is None:
        return ""
    try:
        return str(entry.target.relative_to(archive_dir))
    except ValueError:
        return str(entry.target)


def collection_rows(library: Library, results: dict[str, MatchResult], overrides: dict[str, Override],
                    found: dict[str, Found], added: Iterable[str], archived: dict[str, ArchiveEntry],
                    archive_dir: Path) -> list[dict[str, str]]:
    """Every track in the snapshot, music or not, ordered as the albums were.

    `archived` holds only the entries whose file really left Music, and `added` the videos that
    went into the YouTube playlist: a pick never added is no copy of anything.
    """
    added = set(added)
    playlists = _playlists_of(library)
    rows = []
    ordered = sorted(library.tracks.values(), key=lambda t: (
        (t.album_artist or t.artist).casefold(), t.album.casefold(), t.disc_number or 0,
        t.track_number or 0, t.name.casefold()))
    for track in ordered:
        result, entry = results.get(track.id), archived.get(track.id)
        spotify = ""
        if result is not None and result.status == MATCHED and result.candidate is not None:
            spotify = result.candidate.url
        elif entry is not None and entry.spotify:
            spotify = f"https://open.spotify.com/track/{entry.spotify.rsplit(':', 1)[-1]}"
        picks = found[track.id].picks if track.id in found else []
        row = {
            "artist": track.artist,
            "title": track.name,
            "album": track.album,
            "album artist": track.album_artist,
            "year": str(track.year or ""),
            "genre": track.genre,
            "time": _time(track.duration),
            "plays": str(track.plays),
            "stars": str(track.stars or ""),
            "added": track.date_added.date().isoformat() if track.date_added else "",
            "last played": track.last_played.date().isoformat() if track.last_played else "",
            "playlists": " | ".join(playlists.get(track.id, [])),
            "fate": _fate(track, entry, overrides.get(track.id)),
            "spotify": spotify,
            "youtube": " ".join(p.url for p in picks if p.video_id in added),
            "archived as": _archived_as(entry, archive_dir),
        }
        rows.append({k: _BREAKS.sub(" ", v or "").strip() for k, v in row.items()})
    return rows


def write_collection(rows: list[dict[str, str]], path: Path) -> None:
    """Plain tab-separated lines: csv quoting would put quotes around every title with a quote
    in it, and grep would show them. Fields never hold a tab or a line break."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        out.write("\t".join(FIELDS) + "\n")
        for row in rows:
            out.write("\t".join(row[k] for k in FIELDS) + "\n")
    tmp.replace(path)
