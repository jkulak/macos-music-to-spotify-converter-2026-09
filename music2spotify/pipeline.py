"""Match the whole library: cached searches, manual overrides, saved results."""

from __future__ import annotations

import csv
import json
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .library import Library, Track
from .matching import (
    MATCHED,
    NOT_FOUND,
    SKIPPED,
    Candidate,
    best_assessment,
    search_queries,
)

Search = Callable[[str], list[Candidate] | None]  # None: not cached and offline
Lookup = Callable[[str], Candidate | None]


class JsonLinesCache:
    """Responses by key, appended to a JSON-lines file as they arrive, so an
    interrupted run keeps everything it fetched."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, object] = {}
        if path.exists():
            with path.open(encoding="utf-8") as lines:
                for line in lines:
                    if line.strip():
                        entry = json.loads(line)
                        self.entries[entry["key"]] = entry["value"]

    def get(self, key: str):
        return self.entries.get(key)

    def __contains__(self, key: str) -> bool:
        return key in self.entries

    def put(self, key: str, value) -> None:
        self.entries[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"key": key, "value": value}, ensure_ascii=False) + "\n")


def cached_search(cache: JsonLinesCache, fetch: Callable[[str], list[dict]] | None) -> Search:
    """Search through the cache; with no fetch function, only cached queries answer."""

    def search(query: str) -> list[Candidate] | None:
        key = f"search:{query}"
        if key not in cache:
            if fetch is None:
                return None
            cache.put(key, [Candidate.from_api(item).to_dict() for item in fetch(query) if item])
        return [Candidate.from_dict(c) for c in cache.get(key)]

    return search


def cached_lookup(cache: JsonLinesCache, fetch: Callable[[str], dict] | None) -> Lookup:
    def lookup(track_id: str) -> Candidate | None:
        key = f"track:{track_id}"
        if key not in cache:
            if fetch is None:
                return None
            cache.put(key, Candidate.from_api(fetch(track_id)).to_dict())
        return Candidate.from_dict(cache.get(key))

    return lookup


# --- overrides ----------------------------------------------------------------

SKIP = "skip"  # leave the track out of Spotify altogether
NONE = "none"  # not on Spotify: stop proposing candidates
_TRACK_REF = re.compile(r"(?:spotify:track:|open\.spotify\.com/(?:intl-\w+/)?track/)([A-Za-z0-9]{22})")
_BARE_ID = re.compile(r"^[A-Za-z0-9]{22}$")


def spotify_track_id(text: str) -> str | None:
    """The track ID in a spotify:track: URI, an open.spotify.com link or a bare ID."""
    text = text.strip()
    match = _TRACK_REF.search(text)
    if match:
        return match.group(1)
    return text if _BARE_ID.match(text) else None


@dataclass(frozen=True)
class Override:
    track_id: str
    spotify: str  # a track ID, SKIP or NONE
    note: str = ""


OVERRIDE_FIELDS = ["track_id", "spotify", "note"]


def load_overrides(path: Path) -> dict[str, Override]:
    if not path.exists():
        return {}
    overrides = {}
    with path.open(encoding="utf-8-sig", newline="") as rows:
        for line, row in enumerate(csv.DictReader(rows), start=2):
            value = (row.get("spotify") or "").strip()
            track_id = (row.get("track_id") or "").strip()
            if not track_id or not value:
                continue
            if value.lower() in (SKIP, NONE):
                spotify = value.lower()
            else:
                spotify = spotify_track_id(value)
                if spotify is None:
                    raise ValueError(f"{path}:{line}: {value!r} is not a Spotify track, "
                                     f"'{SKIP}' or '{NONE}'")
            overrides[track_id] = Override(track_id, spotify, (row.get("note") or "").strip())
    return overrides


def save_overrides(path: Path, overrides: dict[str, Override]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, OVERRIDE_FIELDS)
        writer.writeheader()
        for override in overrides.values():
            writer.writerow({"track_id": override.track_id, "spotify": override.spotify,
                             "note": override.note})


# --- results ------------------------------------------------------------------

@dataclass
class MatchResult:
    track_id: str
    status: str
    candidate: Candidate | None = None
    score: float = 0.0
    title: float = 0.0
    artist: float = 0.0
    duration_delta: float | None = None
    source: str = "search"  # search | override | library
    notes: list[str] = field(default_factory=list)
    unsearched: int = 0  # queries skipped because they were not cached (offline run)

    @property
    def uri(self) -> str | None:
        return self.candidate.uri if self.candidate and self.status == MATCHED else None

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data["candidate"] = self.candidate.to_dict() if self.candidate else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> MatchResult:
        candidate = Candidate.from_dict(data["candidate"]) if data.get("candidate") else None
        return cls(**{**data, "candidate": candidate})


def match_track(track: Track, search: Search, lookup: Lookup,
                override: Override | None = None) -> MatchResult:
    if override is not None:
        if override.spotify == SKIP:
            return MatchResult(track.id, SKIPPED, source="override",
                               notes=[override.note or "skipped in overrides"])
        if override.spotify == NONE:
            return MatchResult(track.id, NOT_FOUND, source="override",
                               notes=[override.note or "confirmed not on Spotify"])
        candidate = lookup(override.spotify)
        if candidate is not None:
            return MatchResult(track.id, MATCHED, candidate, score=1.0, source="override",
                               notes=[override.note or "set in overrides"])
    reason = track.non_music_reason()
    if reason:
        return MatchResult(track.id, SKIPPED, source="library", notes=[reason])

    candidates: dict[str, Candidate] = {}
    best = None
    unsearched = 0
    for query in search_queries(track):
        found = search(query)
        if found is None:
            unsearched += 1
            continue
        for candidate in found:
            candidates.setdefault(candidate.uri, candidate)
        best = best_assessment(track, list(candidates.values()))
        if best is not None and best.status == MATCHED:
            break
    if best is None:
        notes = ["no search results"] if not unsearched else []
        return MatchResult(track.id, NOT_FOUND, notes=notes, unsearched=unsearched)
    return MatchResult(
        track.id, best.status, best.candidate, score=best.score, title=best.title,
        artist=best.artist, duration_delta=best.duration_delta, notes=best.notes,
        unsearched=unsearched,
    )


def match_library(library: Library, search: Search, lookup: Lookup,
                  overrides: dict[str, Override],
                  progress: Callable[[int, int, Counter], None] | None = None,
                  only: Iterable[str] | None = None) -> dict[str, MatchResult]:
    tracks = [library.tracks[i] for i in only] if only is not None else list(library.tracks.values())
    results: dict[str, MatchResult] = {}
    counts: Counter = Counter()
    for done, track in enumerate(tracks, start=1):
        result = match_track(track, search, lookup, overrides.get(track.id))
        results[track.id] = result
        counts[result.status] += 1
        if progress:
            progress(done, len(tracks), counts)
    return results


def save_matches(path: Path, results: dict[str, MatchResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"savedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "results": [r.to_dict() for r in results.values()]}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_matches(path: Path) -> dict[str, MatchResult]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"{path} does not exist; run `make match` first") from None
    return {r["track_id"]: MatchResult.from_dict(r) for r in data["results"]}
