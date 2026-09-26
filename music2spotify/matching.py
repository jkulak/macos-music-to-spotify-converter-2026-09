"""Decide which Spotify track, if any, is the same recording as a Music track.

Only the same version counts. A different version (the original instead of a
remix, a live take instead of the studio one) is kept as an *alternative* for
Kuba to accept or reject; it never goes into a playlist on its own.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .library import Track
from .normalize import (
    ParsedTitle,
    fold,
    is_version_segment,
    parse_title,
    similarity,
    split_artists,
    strip_the,
    versions_compatible,
)

MATCHED = "matched"
UNCERTAIN = "uncertain"
ALTERNATIVE = "alternative"
NOT_FOUND = "not_found"
SKIPPED = "skipped"

RANK = {MATCHED: 3, UNCERTAIN: 2, ALTERNATIVE: 1, NOT_FOUND: 0}

# Artist fields that say nothing about who made the track.
_NO_ARTIST = {"", "various artists", "various", "unknown artist", "unknown", "va"}
_LEADING_NUMBER = re.compile(r"^\d{1,2}(?:[ ._]+|\s*-\s*)(?=\D)")


@dataclass(frozen=True)
class Candidate:
    """The parts of a Spotify track object that matching and reporting use."""

    uri: str
    name: str
    artists: tuple[str, ...]
    album: str
    album_type: str
    release_date: str
    duration_ms: int
    isrc: str = ""
    playable: bool = True  # in the configured market

    @property
    def id(self) -> str:
        return self.uri.rsplit(":", 1)[-1]

    @property
    def url(self) -> str:
        return f"https://open.spotify.com/track/{self.id}"

    @property
    def duration(self) -> float:
        return self.duration_ms / 1000

    @property
    def label(self) -> str:
        return f"{', '.join(self.artists)} - {self.name}"

    @classmethod
    def from_api(cls, item: dict) -> Candidate:
        album = item.get("album") or {}
        return cls(
            uri=item["uri"],
            name=item.get("name") or "",
            artists=tuple(a.get("name") or "" for a in item.get("artists") or []),
            album=album.get("name") or "",
            album_type=album.get("album_type") or "",
            release_date=album.get("release_date") or "",
            duration_ms=item.get("duration_ms") or 0,
            isrc=(item.get("external_ids") or {}).get("isrc") or "",
            playable=item.get("is_playable", True) is not False,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Candidate:
        return cls(**{**data, "artists": tuple(data["artists"])})


@dataclass(frozen=True)
class Reading:
    """One way of reading a Music track's tags."""

    label: str
    artists: tuple[str, ...]  # folded; the first is the main artist
    artist_text: str  # folded, as one string
    title: ParsedTitle

    @property
    def main_artist(self) -> str:
        return self.artists[0] if self.artists else ""


def _artists_of(text: str) -> tuple[tuple[str, ...], str]:
    folded = fold(text)
    if folded in _NO_ARTIST:
        return (), ""
    return tuple(split_artists(text)), folded


def readings(track: Track) -> list[Reading]:
    """The track as tagged, plus the other readings its title allows.

    DJ libraries often have "Artist - Title" or "Title - Artist" typed into the
    title field, or a leading track number; each of those is tried as well.
    """
    artists, artist_text = _artists_of(track.artist)
    if not artists:
        artists, artist_text = _artists_of(track.album_artist)
    result = [Reading("as tagged", artists, artist_text, parse_title(track.name))]

    if " - " in track.name:
        left, right = track.name.split(" - ", 1)
        left_artists, left_text = _artists_of(left)
        right_bare = re.sub(r"\s*[\(\[].*?[\)\]]", "", right).strip()
        # Judge the dash part without its brackets: "I Need A Dollar (X Remix)" is a
        # title carrying a remix, "X Remix" alone is a version suffix.
        right_is_version = not right_bare or is_version_segment(right_bare)
        # Tried even when the left part is not the tagged artist: some files carry
        # the uploader or a compilation in the artist field instead.
        if left_artists and not right_is_version:
            result.append(Reading("artist - title in the title field",
                                  left_artists, left_text, parse_title(right)))
        if not right_is_version:
            right_artists, right_text = _artists_of(right_bare)
            if right_artists:
                result.append(Reading("title - artist in the title field",
                                      right_artists, right_text, parse_title(left)))

    stripped = _LEADING_NUMBER.sub("", track.name)
    if stripped != track.name and stripped.strip():
        result.append(Reading("leading number dropped", artists, artist_text,
                              parse_title(stripped)))
    return result


def search_queries(track: Track) -> list[str]:
    """Search queries, most precise first. Search returns at most 10 results,
    so each query has to be narrow enough to put the right track in them."""
    queries: list[str] = []
    for reading in readings(track):
        title = reading.title
        artist = reading.main_artist
        if not title.core:
            continue
        if artist:
            queries.append(f"track:{title.core} artist:{artist}")
            if title.versions:
                queries.append(f"{title.search_words} {artist}")
            queries.append(f"{artist} {title.core}")
        else:
            queries.append(f"track:{title.search_words}")
    return list(dict.fromkeys(q for q in queries if q.strip()))


@dataclass
class Assessment:
    candidate: Candidate
    reading: str
    title: float
    artist: float
    album: float
    duration_delta: float | None
    same_version: bool
    score: float
    status: str
    notes: list[str] = field(default_factory=list)

    @property
    def sort_key(self) -> tuple:
        return (
            RANK[self.status],
            round(self.score, 3),
            round(self.album, 2),
            self.candidate.album_type != "compilation",
            -(self.duration_delta or 0),
        )


def _part_score(part: str, names: list[str], joined: str) -> float:
    part = strip_the(part)
    if f" {part} " in f" {joined} ":
        return 1.0
    return max((similarity(part, n) for n in names), default=0.0)


def artist_score(reading: Reading, candidate: Candidate) -> float:
    if not reading.artists:
        return 0.5  # unknown artist: neither evidence for nor against
    names = [strip_the(fold(a)) for a in candidate.artists if a]
    if not names:
        return 0.0
    joined = " ".join(names)
    whole = max(similarity(strip_the(reading.artist_text), n) for n in [*names, joined])
    parts = [_part_score(p, names, joined) for p in reading.artists]
    # The main artist has to be there; the others (featured, "& friends") may be
    # missing on Spotify or credited differently, so they weigh less.
    by_parts = 0.7 * parts[0] + 0.3 * (sum(parts) / len(parts))
    return max(whole, by_parts)


def duration_score(delta: float | None) -> float:
    if delta is None:
        return 0.6
    for limit, score in ((3, 1.0), (7, 0.9), (15, 0.7), (30, 0.4), (60, 0.2)):
        if delta <= limit:
            return score
    return 0.0


def assess(track: Track, reading: Reading, candidate: Candidate) -> Assessment:
    theirs = parse_title(candidate.name)
    title = similarity(reading.title.core, theirs.core)
    artist = artist_score(reading, candidate)
    album = similarity(fold(track.album), fold(candidate.album)) if track.album else 0.0
    delta = abs(track.duration - candidate.duration) if track.duration else None
    same_version = versions_compatible(reading.title.versions, theirs.versions)
    score = 0.45 * title + 0.35 * artist + 0.15 * duration_score(delta) + 0.05 * album
    notes = []
    if reading.label != "as tagged":
        notes.append(f"read as {reading.label}")

    tolerance = max(10.0, 0.05 * (track.duration or 0))
    if same_version and title >= 0.9 and artist >= 0.8 and reading.artists:
        if delta is None or delta <= tolerance:
            status = MATCHED
        elif reading.title.cut or theirs.cut:
            status = MATCHED
            notes.append(f"another cut of the same version, {delta:.0f}s difference")
        else:
            status = UNCERTAIN
            notes.append(f"length differs by {delta:.0f}s")
    elif same_version and title >= 0.75 and score >= 0.65 and (
            artist >= 0.6
            # Without an artist only an exact title of the same length is worth a look.
            or (not reading.artists and title >= 0.9 and delta is not None and delta <= tolerance)):
        status = UNCERTAIN
        if title < 0.9:
            notes.append(f"title only {title:.0%} similar")
        if artist < 0.8:
            notes.append(f"artist only {artist:.0%} similar")
        if not reading.artists:
            notes.append("Music has no artist")
        if delta is not None and delta > tolerance:
            notes.append(f"length differs by {delta:.0f}s")
    elif not same_version and title >= 0.85 and artist >= 0.7:
        status = ALTERNATIVE
        ours = ", ".join(_describe(v) for v in reading.title.versions) or "the original"
        spotify = ", ".join(_describe(v) for v in theirs.versions) or "the original"
        notes.append(f"Music has {ours}, Spotify has {spotify}")
    else:
        status = NOT_FOUND
    if not candidate.playable and status != NOT_FOUND:
        # A greyed-out entry in a playlist is no better than a missing one.
        notes.append(f"on Spotify but not playable in this market (would be {status})")
        status = NOT_FOUND
    return Assessment(candidate, reading.label, title, artist, album, delta,
                      same_version, score, status, notes)


def _describe(version) -> str:
    return f"{version.by} {version.marker}".strip()


def best_assessment(track: Track, candidates: list[Candidate]) -> Assessment | None:
    best = None
    for reading in readings(track):
        for candidate in candidates:
            current = assess(track, reading, candidate)
            if best is None or current.sort_key > best.sort_key:
                best = current
    return best
