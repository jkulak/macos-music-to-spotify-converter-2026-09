"""The Music library snapshot written by exporter/export_library.js."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SUPPORTED_FORMAT = 1

# Library items nobody would look for on Spotify. Keyed by lower-cased genre,
# because that is where Music files voice memos, audiobooks and the like.
NON_MUSIC_GENRES = {
    "voice memo": "voice memo",
    "audiobook": "audiobook",
    "learning material": "learning material",
    "ringtone": "ringtone",
}


class LibraryError(Exception):
    """The snapshot cannot be used at all (wrong format, not JSON, ...)."""


@dataclass(frozen=True)
class Track:
    id: str
    name: str
    artist: str
    album_artist: str
    album: str
    genre: str
    year: int | None
    duration: float | None  # seconds
    track_number: int | None
    disc_number: int | None
    plays: int
    last_played: datetime | None
    skips: int
    rating: int  # 0-100 as Music stores it: 20 per star
    rating_kind: str  # "user", or "computed" when derived from the album rating
    favorited: bool
    date_added: datetime | None
    bpm: int | None
    kind: str
    track_class: str
    compilation: bool

    @property
    def stars(self) -> int:
        return self.rating // 20

    def non_music_reason(self) -> str | None:
        if self.track_class == "urlTrack" or self.kind == "Internet audio stream":
            return "radio stream"
        if "pdf" in self.kind.lower():
            return "document"
        return NON_MUSIC_GENRES.get(self.genre.strip().lower())

    @property
    def is_music(self) -> bool:
        return self.non_music_reason() is None


@dataclass(eq=False)
class Playlist:
    id: str
    name: str
    kind: str  # folder | smart | regular | other
    parent_id: str | None
    position: int
    description: str
    track_ids: list[str]
    parent: Playlist | None = field(default=None, repr=False)
    children: list[Playlist] = field(default_factory=list, repr=False)

    @property
    def is_folder(self) -> bool:
        return self.kind == "folder"

    @property
    def folder_path(self) -> list[str]:
        """Names of the folders this playlist sits in, outermost first."""
        names = []
        node = self.parent
        while node is not None:
            names.append(node.name)
            node = node.parent
        return names[::-1]

    @property
    def path(self) -> list[str]:
        return [*self.folder_path, self.name]

    def walk(self) -> Iterator[Playlist]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class Library:
    tracks: dict[str, Track]
    roots: list[Playlist]
    exported_at: str
    source_version: str | None
    problems: list[str]

    def walk(self) -> Iterator[Playlist]:
        """Every folder and playlist, depth first, in Music's sidebar order."""
        for root in self.roots:
            yield from root.walk()

    def playlists(self) -> list[Playlist]:
        return [p for p in self.walk() if not p.is_folder]

    def folders(self) -> list[Playlist]:
        return [p for p in self.walk() if p.is_folder]

    def playlist_tracks(self, playlist: Playlist) -> list[Track]:
        return [self.tracks[i] for i in playlist.track_ids if i in self.tracks]


def _date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _positive(value) -> int | None:
    return int(value) if value else None


def _track(raw: dict) -> Track:
    return Track(
        id=raw["id"],
        name=raw.get("name") or "",
        artist=raw.get("artist") or "",
        album_artist=raw.get("albumArtist") or "",
        album=raw.get("album") or "",
        genre=raw.get("genre") or "",
        year=_positive(raw.get("year")),
        duration=raw.get("duration") or None,
        track_number=_positive(raw.get("trackNumber")),
        disc_number=_positive(raw.get("discNumber")),
        plays=raw.get("plays") or 0,
        last_played=_date(raw.get("lastPlayed")),
        skips=raw.get("skips") or 0,
        rating=raw.get("rating") or 0,
        rating_kind=raw.get("ratingKind") or "computed",
        favorited=bool(raw.get("favorited")),
        date_added=_date(raw.get("dateAdded")),
        bpm=_positive(raw.get("bpm")),
        kind=raw.get("kind") or "",
        track_class=raw.get("class") or "",
        compilation=bool(raw.get("compilation")),
    )


def parse_library(data: dict) -> Library:
    if data.get("format") != SUPPORTED_FORMAT:
        raise LibraryError(
            f"snapshot format {data.get('format')!r} is not supported "
            f"(expected {SUPPORTED_FORMAT}); re-run `make export`"
        )
    problems = [f"exporter: {w}" for w in data.get("warnings", [])]

    tracks: dict[str, Track] = {}
    for raw in data["tracks"]:
        if raw["id"] in tracks:
            problems.append(f"track {raw['id']} appears twice in the library")
            continue
        tracks[raw["id"]] = _track(raw)

    by_id: dict[str, Playlist] = {}
    for raw in sorted(data["playlists"], key=lambda p: p["position"]):
        playlist = Playlist(
            id=raw["id"],
            name=raw["name"],
            kind=raw["kind"],
            parent_id=raw.get("parentId"),
            position=raw["position"],
            description=raw.get("description") or "",
            track_ids=list(raw.get("trackIds") or []),
        )
        by_id[playlist.id] = playlist

    roots = []
    for playlist in by_id.values():
        parent = by_id.get(playlist.parent_id) if playlist.parent_id else None
        if playlist.parent_id and parent is None:
            problems.append(
                f"playlist {playlist.name!r} points at a parent {playlist.parent_id} "
                "that is not in the snapshot; placed at the top level"
            )
        if parent is not None:
            playlist.parent = parent
            parent.children.append(playlist)
        else:
            roots.append(playlist)

    for playlist in by_id.values():
        missing = [i for i in playlist.track_ids if i not in tracks]
        if missing:
            problems.append(
                f"playlist {' / '.join(playlist.path)!r} has {len(missing)} item(s) "
                "that are not in the library"
            )

    return Library(
        tracks=tracks,
        roots=roots,
        exported_at=data.get("exportedAt", ""),
        source_version=(data.get("source") or {}).get("version"),
        problems=problems,
    )


def load_library(path: Path) -> Library:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LibraryError(f"{path} does not exist; run `make export` first") from None
    except json.JSONDecodeError as error:
        raise LibraryError(f"{path} is not valid JSON: {error}") from None
    return parse_library(data)
