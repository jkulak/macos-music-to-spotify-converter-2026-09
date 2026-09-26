"""Match the whole library: cached searches, manual overrides, saved results."""

from __future__ import annotations

import csv
import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .albums import AlbumGroup, AlbumHit, album_groups, rank_albums
from .library import Library, Track
from .matching import (
    MATCHED,
    NOT_FOUND,
    SKIPPED,
    Candidate,
    best_assessment,
    search_queries,
)
from .spotify import RateLimited, SpotifyClient


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


class Budget:
    """One quota bucket. Once Spotify refuses with a long Retry-After, nothing more
    is fetched from it in this run; cached answers keep coming."""

    def __init__(self):
        self.blocked: RateLimited | None = None

    def call(self, fetch: Callable, *args):
        if self.blocked is not None:
            return None
        try:
            return fetch(*args)
        except RateLimited as error:
            self.blocked = error
            return None


class Sources:
    """Spotify answers through the cache. Without a client (offline) or once a
    quota bucket is used up, anything not cached comes back as None."""

    def __init__(self, cache: JsonLinesCache, client: SpotifyClient | None = None):
        self.cache = cache
        self.client = client
        self.search_budget = Budget()  # track and album search share Spotify's search quota
        self.catalogue_budget = Budget()  # album track lists and single tracks

    @property
    def blocked(self) -> RateLimited | None:
        return self.search_budget.blocked or self.catalogue_budget.blocked

    def _cached(self, key: str, budget: Budget, fetch: Callable | None, convert: Callable):
        if key not in self.cache:
            if self.client is None or fetch is None:
                return None
            fetched = budget.call(fetch)
            if fetched is None:
                return None
            self.cache.put(key, convert(fetched))
        return self.cache.get(key)

    def search(self, query: str) -> list[Candidate] | None:
        found = self._cached(
            f"search:{query}", self.search_budget,
            self.client and (lambda: self.client.search_tracks(query)),
            lambda items: [Candidate.from_api(i).to_dict() for i in items if i])
        return None if found is None else [Candidate.from_dict(c) for c in found]

    def lookup(self, track_id: str) -> Candidate | None:
        found = self._cached(
            f"track:{track_id}", self.catalogue_budget,
            self.client and (lambda: self.client.track(track_id)),
            lambda item: Candidate.from_api(item).to_dict())
        return None if found is None else Candidate.from_dict(found)

    def search_albums(self, query: str) -> list[AlbumHit] | None:
        found = self._cached(
            f"album-search:{query}", self.search_budget,
            self.client and (lambda: self.client.search_albums(query)),
            lambda items: [AlbumHit.from_api(i).to_dict() for i in items if i])
        return None if found is None else [AlbumHit.from_dict(a) for a in found]

    def album_tracks(self, album: AlbumHit) -> list[Candidate] | None:
        found = self._cached(
            f"album-tracks:{album.id}", self.catalogue_budget,
            self.client and (lambda: self.client.album_tracks(album.id)),
            lambda items: [Candidate.from_album_track(i, album).to_dict() for i in items if i])
        return None if found is None else [Candidate.from_dict(c) for c in found]


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


def match_track(track: Track, sources: Sources, override: Override | None = None,
                extra: Iterable[Candidate] = (), max_queries: int | None = None) -> MatchResult:
    """Match one track from its album's candidates (extra) and up to max_queries searches."""
    if override is not None:
        if override.spotify == SKIP:
            return MatchResult(track.id, SKIPPED, source="override",
                               notes=[override.note or "skipped in overrides"])
        if override.spotify == NONE:
            return MatchResult(track.id, NOT_FOUND, source="override",
                               notes=[override.note or "confirmed not on Spotify"])
        candidate = sources.lookup(override.spotify)
        if candidate is not None:
            return MatchResult(track.id, MATCHED, candidate, score=1.0, source="override",
                               notes=[override.note or "set in overrides"])
    reason = track.non_music_reason()
    if reason:
        return MatchResult(track.id, SKIPPED, source="library", notes=[reason])

    candidates: dict[str, Candidate] = {c.uri: c for c in extra}
    best = best_assessment(track, list(candidates.values())) if candidates else None
    searched = unsearched = 0
    if best is None or best.status != MATCHED:
        for query in search_queries(track)[:max_queries]:
            found = sources.search(query)
            if found is None:
                unsearched += 1
                continue
            searched += 1
            for candidate in found:
                candidates.setdefault(candidate.uri, candidate)
            best = best_assessment(track, list(candidates.values()))
            if best is not None and best.status == MATCHED:
                break
    if best is None:
        notes = ["no search results"] if searched else []
        return MatchResult(track.id, NOT_FOUND, notes=notes, unsearched=unsearched)
    return MatchResult(
        track.id, best.status, best.candidate, score=best.score, title=best.title,
        artist=best.artist, duration_delta=best.duration_delta, notes=best.notes,
        unsearched=unsearched,
    )


Progress = Callable[[str, int, int], None]


def _album_of(candidate: Candidate, sources: Sources) -> AlbumHit | None:
    """The Spotify album a matched track came from. Older cache entries lack the
    album ID; looking the track up costs catalogue quota, not search quota."""
    if not candidate.album_id:
        candidate = sources.lookup(candidate.id)
        if candidate is None or not candidate.album_id:
            return None
    return AlbumHit(candidate.album_id, candidate.album, candidate.artists,
                    candidate.album_type, candidate.release_date, 0)


def _album_candidates(group: AlbumGroup, sources: Sources, tracks: dict[str, Track],
                      results: dict[str, MatchResult]) -> list[Candidate]:
    hits: list[AlbumHit] = []
    # A track of the album that is already matched names the album for free.
    for track_id in group.track_ids:
        result = results.get(track_id)
        if result and result.status == MATCHED and result.candidate:
            known = _album_of(result.candidate, sources)
            if known and rank_albums(group, [known]):
                hits = [known]
                break
    if not hits:
        for query in group.queries():
            found = sources.search_albums(query)
            if found is None:
                return []
            hits = rank_albums(group, found)
            if hits:
                break
    candidates: list[Candidate] = []
    members = [tracks[i] for i in group.track_ids]
    for hit in hits[:2]:
        listing = sources.album_tracks(hit)
        if listing is None:
            break
        candidates += listing
        settled = sum(1 for t in members
                      if (a := best_assessment(t, candidates)) and a.status == MATCHED)
        # Deluxe and standard editions both exist often; look at the second
        # candidate only when the first leaves most of the tracks open.
        if settled * 2 >= len(members):
            break
    return candidates


def match_library(library: Library, sources: Sources, overrides: dict[str, Override],
                  progress: Progress | None = None,
                  only: Iterable[str] | None = None) -> dict[str, MatchResult]:
    """Match in the order that gets the most out of the search quota: what the
    cache already settles, then album by album, then one search per track
    across the library, then the next search for whatever is still open."""
    tracks = [library.tracks[i] for i in only] if only is not None else list(library.tracks.values())

    def report(stage: str, done: int, total: int) -> None:
        if progress:
            progress(stage, done, total)

    offline = Sources(sources.cache)
    results = {t.id: match_track(t, offline, overrides.get(t.id)) for t in tracks}

    def is_open(track: Track) -> bool:
        result = results[track.id]
        return result.status not in (MATCHED, SKIPPED) and result.source != "override"

    extra: dict[str, list[Candidate]] = {}
    groups = [g for g in album_groups([t for t in tracks if t.is_music and t.id not in overrides])
              if any(is_open(library.tracks[i]) for i in g.track_ids)]
    for done, group in enumerate(groups, start=1):
        candidates = _album_candidates(group, sources, library.tracks, results)
        for track_id in group.track_ids:
            extra[track_id] = candidates
        report("albums", done, len(groups))

    query_count = {t.id: len(search_queries(t)) for t in tracks}
    for max_queries in range(1, max(query_count.values(), default=0) + 1):
        # The first round takes every open track, so album candidates are weighed
        # even for tracks with nothing to search for.
        pending = [t for t in tracks if is_open(t)
                   and (max_queries == 1 or query_count[t.id] >= max_queries)]
        for done, track in enumerate(pending, start=1):
            results[track.id] = match_track(track, sources, overrides.get(track.id),
                                            extra.get(track.id, ()), max_queries)
            report(f"search {max_queries}", done, len(pending))
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
