"""Match tracks album by album.

Search is the scarce resource in development mode (a per-account daily quota),
while fetching an album's tracks is not counted against it. So tracks that share
an album are matched from one album search plus the album's track list, and
only what that leaves open is searched track by track.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass

from .library import Track
from .matching import NO_ARTIST
from .normalize import fold, parse_title, similarity, split_artists, strip_the

MIN_TRACKS = 2  # one album search already saves a request at two tracks
_PACKAGING = re.compile(r"\b(ep|lp|single|deluxe|expanded|edition|remastered|version)\b")


def album_core(title: str) -> str:
    """The album title without "(Deluxe Version)", " - EP" and the like."""
    core = _PACKAGING.sub(" ", parse_title(title).core)
    return " ".join(core.split())


@dataclass(frozen=True)
class AlbumGroup:
    artist: str  # as tagged; empty for a compilation
    album: str  # as tagged
    track_ids: tuple[str, ...]

    @property
    def main_artist(self) -> str:
        parts = split_artists(self.artist) if self.artist else []
        return parts[0] if parts else ""

    def queries(self) -> list[str]:
        core = album_core(self.album)
        if not core:
            return []
        if self.main_artist:
            return [f"album:{core} artist:{self.main_artist}", f"{self.main_artist} {core}"]
        return [f"album:{core}"]


@dataclass(frozen=True)
class AlbumHit:
    id: str
    name: str
    artists: tuple[str, ...]
    album_type: str
    release_date: str
    total_tracks: int

    @classmethod
    def from_api(cls, item: dict) -> AlbumHit:
        return cls(
            id=item["id"],
            name=item.get("name") or "",
            artists=tuple(a.get("name") or "" for a in item.get("artists") or []),
            album_type=item.get("album_type") or "",
            release_date=item.get("release_date") or "",
            total_tracks=item.get("total_tracks") or 0,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> AlbumHit:
        return cls(**{**data, "artists": tuple(data["artists"])})


def album_groups(tracks: list[Track], min_tracks: int = MIN_TRACKS) -> list[AlbumGroup]:
    """Tracks that share an album, keyed by album artist (or artist) and title."""
    grouped: dict[tuple[str, str], list[Track]] = defaultdict(list)
    for track in tracks:
        if not track.album.strip():
            continue
        # The album artist when there is one ("Various Artists" included, which keeps
        # a compilation together); otherwise the main artist, so "Kilo" and
        # "Kilo feat. Moth" land on the same album.
        owner = fold(track.album_artist) or (split_artists(track.artist) or [""])[0]
        grouped[(owner, fold(track.album))].append(track)
    groups = []
    for members in grouped.values():
        if len(members) < min_tracks:
            continue
        first = members[0]
        owner = first.album_artist if fold(first.album_artist) not in NO_ARTIST else ""
        if not owner:
            # A compilation, unless every track is by the same artist.
            mains = {(split_artists(t.artist) or [""])[0] for t in members}
            owner = first.artist if len(mains) == 1 and fold(first.artist) not in NO_ARTIST else ""
        groups.append(AlbumGroup(owner, first.album, tuple(t.id for t in members)))
    return groups


def rank_albums(group: AlbumGroup, hits: list[AlbumHit]) -> list[AlbumHit]:
    """Albums that can be this one, best first."""
    ours = album_core(group.album)
    artist = strip_the(group.main_artist)
    scored = []
    for hit in hits:
        title = similarity(ours, album_core(hit.name))
        if title < 0.75:
            continue
        if artist:
            names = [strip_the(fold(a)) for a in hit.artists]
            credit = max((similarity(artist, n) for n in names), default=0.0)
            if credit < 0.6 and "various artists" not in names:
                continue
        else:
            credit = 1.0
        size = -abs(hit.total_tracks - len(group.track_ids)) if hit.total_tracks >= len(group.track_ids) else -99
        scored.append(((round(title, 2), round(credit, 2), size), hit))
    return [hit for _, hit in sorted(scored, key=lambda pair: pair[0], reverse=True)]
