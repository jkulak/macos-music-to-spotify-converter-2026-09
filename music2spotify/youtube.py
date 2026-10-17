"""A private YouTube playlist for the favourites Spotify does not have in the exact version.

Searching is anonymous: YouTube Music through ytmusicapi, songs (Art Tracks) first, then
videos, as djdl does. Every result is scored with the same matching as Spotify, so "the same
recording" means the same thing on both. When the exact recording is there it is the only pick;
otherwise the two closest plausible results are kept, so picking by ear later replaces picking
by hand now.

Writing the playlist needs the browser headers saved by `make youtube-auth`. That file is a
login: nothing here prints it, and data/ is never committed.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from .library import Library, Track
from .matching import (
    ALTERNATIVE,
    MATCHED,
    NOT_FOUND,
    RANK,
    UNCERTAIN,
    Assessment,
    Candidate,
    assess,
    readings,
)
from .normalize import is_version_segment
from .pipeline import NONE, SKIP, UNREVIEWED, JsonLinesCache, MatchResult, Override
from .reports import _md, _write, length

SONG = "song"
VIDEO = "video"
FILTERS = (("songs", SONG), ("videos", VIDEO))
MAX_QUERIES = 3  # readings of one track; more only repeats the same search
PICKS_WHEN_UNSURE = 2
BATCH = 50  # videos per playlist edit
MIN_INTERVAL = 0.5  # seconds between searches: anonymous, so be gentle

# What each status means in the reports, from the point of view of the playlist.
EXACT = "exact"
SIMILAR = "similar"
NOTHING = "nothing"

DESCRIPTION = ("Favourites from my mp3 collection that Spotify does not have in the exact "
               "version. Where the recording was not certain, the two closest results are both here.")


class YouTubeError(Exception):
    pass


class MusicClient(Protocol):
    def search(self, query: str, filter: str | None = None, limit: int = 20) -> list[dict]: ...


class PlaylistClient(Protocol):
    def create_playlist(self, title: str, description: str, privacy_status: str = "PRIVATE") -> str | dict: ...
    def get_playlist(self, playlistId: str, limit: int | None = 100) -> dict: ...  # noqa: N803
    def add_playlist_items(self, playlistId: str, videoIds: list[str],  # noqa: N803
                           duplicates: bool = False) -> str | dict: ...


def anonymous_client() -> MusicClient:
    from ytmusicapi import YTMusic

    # English is not cosmetic: ytmusicapi drops filtered shelves whose localised title lacks
    # the English word, which empties searches made in another language.
    return YTMusic(language="en")


def signed_in_client(auth_file: Path) -> PlaylistClient:
    if not auth_file.exists():
        raise YouTubeError(f"{auth_file} does not exist; run `make youtube-auth` first")
    from ytmusicapi import YTMusic

    return YTMusic(str(auth_file), language="en")


# --- which tracks ----------------------------------------------------------------------------

NOT_ON_SPOTIFY = "not found on Spotify"
DECIDED_NOT_ON_SPOTIFY = "not on Spotify (your decision)"
UNSURE_ON_SPOTIFY = "on Spotify as a version accepted without review"


@dataclass
class Wanted:
    track: Track
    why: str
    spotify: str = ""  # what Spotify has instead, when it has something


@dataclass
class Selection:
    wanted: list[Wanted]
    let_go: list[Track]  # favourites left out in review: not searched, not kept


def _most_played(track: Track) -> tuple:
    return (-track.plays, track.artist.lower(), track.name.lower(), track.id)


def select_favourites(library: Library, results: dict[str, MatchResult],
                      overrides: dict[str, Override], favourite_plays: int) -> Selection:
    """Music played favourite_plays times or more that Spotify lacks in the exact version.

    A track Kuba linked or accepted himself is on Spotify as he wants it, and one he left out
    is let go; only what nobody looked at, or what he marked as missing, goes to YouTube.
    """
    wanted, let_go = [], []
    for track_id, track in library.tracks.items():
        if not track.is_music or track.plays < favourite_plays:
            continue
        override = overrides.get(track_id)
        result = results.get(track_id)
        if override is not None:
            if override.spotify == SKIP:
                let_go.append(track)
            elif override.spotify == NONE:
                wanted.append(Wanted(track, DECIDED_NOT_ON_SPOTIFY))
            elif override.note == UNREVIEWED:
                label = result.candidate.label if result and result.candidate else ""
                wanted.append(Wanted(track, UNSURE_ON_SPOTIFY, label))
        elif result is not None and result.status in (NOT_FOUND, UNCERTAIN, ALTERNATIVE):
            wanted.append(Wanted(track, NOT_ON_SPOTIFY))
    wanted.sort(key=lambda w: _most_played(w.track))
    let_go.sort(key=_most_played)
    return Selection(wanted, let_go)


# --- reading YouTube results -----------------------------------------------------------------

_NOISE = re.compile(r"\b(official|lyrics?|visuali[sz]er|audio|video|videoclip|clip|hd|hq|4k|m/?v)\b", re.I)
_BRACKET = re.compile(r"\s*[\(\[]([^\)\]]*)[\)\]]")
_PIPE = re.compile(r"\s+[|｜]\s+")
_TRAILING_QUALITY = re.compile(r"\s+(?:hd|hq|4k)$", re.I)
_DASH = re.compile(r"\s+[-–—]\s+")


def clean_title(title: str) -> str:
    """The title without what only a video upload adds: "(Official Video)", "[Lyrics]",
    "| Official Audio", a trailing "HD". A bracket naming a version stays."""
    def drop(match: re.Match) -> str:
        inner = match.group(1)
        return "" if _NOISE.search(inner) and not is_version_segment(inner) else match.group(0)

    title = _BRACKET.sub(drop, title)
    title = _PIPE.split(title, maxsplit=1)[0]
    return _TRAILING_QUALITY.sub("", title).strip()


def _artists(result: dict) -> tuple[str, ...]:
    names = []
    for artist in result.get("artists") or []:
        name = artist.get("name") if isinstance(artist, dict) else artist
        if name:
            names.append(name)
    return tuple(names)


def _seconds(result: dict) -> float:
    if result.get("duration_seconds"):
        return float(result["duration_seconds"])
    text = result.get("duration") or ""
    if re.fullmatch(r"\d+(?::\d{1,2}){1,2}", text):
        seconds = 0
        for part in text.split(":"):
            seconds = seconds * 60 + int(part)
        return float(seconds)
    return 0.0


def slim(result: dict) -> dict | None:
    """The part of a search result worth caching; None for anything that is not playable."""
    video_id = result.get("videoId")
    if not video_id:
        return None
    album = result.get("album")
    return {
        "videoId": video_id,
        "title": result.get("title") or "",
        "artists": list(_artists(result)),
        "album": (album.get("name") if isinstance(album, dict) else album) or "",
        "seconds": _seconds(result),
        "videoType": result.get("videoType") or "",
    }


def interpretations(result: dict, kind: str) -> list[Candidate]:
    """A cached result as Candidates for matching. A video uploaded by someone else usually
    carries "Artist - Title" in its title and the uploader as the artist, so it is also
    read that way."""
    title = clean_title(result["title"])
    common = dict(album=result["album"], album_type=kind, release_date="",
                  duration_ms=int(result["seconds"] * 1000))
    uri = f"youtube:{kind}:{result['videoId']}"
    found = [Candidate(uri=uri, name=title, artists=tuple(result["artists"]), **common)]
    if kind == VIDEO and _DASH.search(title):
        left, right = _DASH.split(title, maxsplit=1)
        if left.strip() and right.strip():
            found.append(Candidate(uri=uri, name=right.strip(), artists=(left.strip(),), **common))
    return found


# --- picks -----------------------------------------------------------------------------------

@dataclass
class Pick:
    video_id: str
    title: str
    artists: list[str]
    album: str
    seconds: float
    kind: str  # song (an Art Track, the released audio) or video
    status: str  # matched, uncertain or alternative, as for Spotify
    score: float
    notes: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://music.youtube.com/watch?v={self.video_id}"

    @property
    def label(self) -> str:
        return f"{', '.join(self.artists)} - {self.title}"


@dataclass
class Found:
    track_id: str
    picks: list[Pick] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.picks:
            return NOTHING
        return EXACT if self.picks[0].status == MATCHED else SIMILAR


def _key(assessment: Assessment) -> tuple:
    return (*assessment.sort_key, assessment.candidate.album_type == SONG)


def _best_per_video(track: Track, results: Iterable[tuple[dict, str]]) -> dict[str, Assessment]:
    best: dict[str, Assessment] = {}
    track_readings = readings(track)
    for result, kind in results:
        for candidate in interpretations(result, kind):
            for reading in track_readings:
                current = assess(track, reading, candidate)
                video_id = result["videoId"]
                if video_id not in best or _key(current) > _key(best[video_id]):
                    best[video_id] = current
    return best


def choose(track: Track, results: Iterable[tuple[dict, str]]) -> list[Pick]:
    """One pick when the exact recording is among the results, else up to two plausible ones.

    "Plausible" is what Spotify matching would call uncertain or another version; anything it
    would call not found (another title, another artist) is never picked.
    """
    best = _best_per_video(track, results)
    exact = [a for a in best.values() if a.status == MATCHED]
    if exact:
        # Two exact results are the same recording; the Art Track is the released audio.
        chosen = [max(exact, key=lambda a: (a.candidate.album_type == SONG, *a.sort_key))]
    else:
        plausible = [a for a in best.values() if a.status in (UNCERTAIN, ALTERNATIVE)]
        chosen = sorted(plausible, key=_key, reverse=True)[:PICKS_WHEN_UNSURE]
    return [_pick(a) for a in chosen]


def _pick(assessment: Assessment) -> Pick:
    candidate = assessment.candidate
    kind, video_id = candidate.uri.split(":")[1:]
    return Pick(video_id, candidate.name, list(candidate.artists), candidate.album,
                candidate.duration, kind, assessment.status, round(assessment.score, 3),
                list(assessment.notes))


def youtube_queries(track: Track) -> list[str]:
    queries = []
    for reading in readings(track):
        if reading.title.core:
            queries.append(f"{reading.main_artist} {reading.title.search_words}".strip())
    return list(dict.fromkeys(queries))[:MAX_QUERIES]


class Searcher:
    """YouTube Music searches through a cache, so a run that stops halfway keeps what it
    fetched and the next one carries on from there."""

    def __init__(self, cache: JsonLinesCache, client_factory: Callable[[], MusicClient] | None = None,
                 min_interval: float | None = None):
        self.cache = cache
        self.client_factory = client_factory
        self.min_interval = MIN_INTERVAL if min_interval is None else min_interval
        self.requests_made = 0
        self._client: MusicClient | None = None
        self._last = 0.0

    def search(self, query: str, filter: str) -> list[dict]:
        key = f"{filter}:{query}"
        if key not in self.cache:
            if self._client is None:
                self._client = (self.client_factory or anonymous_client)()
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                answer = self._client.search(query, filter=filter, limit=20)
            except Exception as error:  # ytmusicapi raises requests' and its own errors alike
                raise YouTubeError(f"YouTube Music search failed ({error}); run the same command "
                                   "again to carry on, searches made so far are kept") from error
            finally:
                self._last = time.monotonic()
            self.requests_made += 1
            self.cache.put(key, [s for s in (slim(r) for r in answer or []) if s])
        return self.cache.get(key)

    def find(self, track: Track) -> Found:
        found = Found(track.id)
        seen: list[tuple[dict, str]] = []
        for query in youtube_queries(track):
            for filter, kind in FILTERS:
                seen += [(r, kind) for r in self.search(query, filter)]
                found.queries.append(f"{filter}: {query}")
                found.picks = choose(track, seen)
                if found.status == EXACT:
                    return found
        return found


def find_all(selection: Selection, searcher: Searcher, progress: Callable[[str, int, int], None]
             ) -> dict[str, Found]:
    found: dict[str, Found] = {}
    total = len(selection.wanted)
    for done, wanted in enumerate(selection.wanted, 1):
        found[wanted.track.id] = searcher.find(wanted.track)
        progress("YouTube Music", done, total)
    return found


def save_found(path: Path, found: dict[str, Found]) -> None:
    data = {"tracks": {k: {"queries": f.queries, "picks": [asdict(p) for p in f.picks]}
                       for k, f in found.items()}}
    _write_json(path, data)


def load_found(path: Path) -> dict[str, Found]:
    if not path.exists():
        raise YouTubeError(f"{path} does not exist; run `make youtube-match` first")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: Found(k, [Pick(**p) for p in v["picks"]], v["queries"])
            for k, v in data["tracks"].items()}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# --- reports ---------------------------------------------------------------------------------

CSV_FIELDS = ["plays", "artist", "title", "album", "length", "why", "spotify", "youtube", "pick",
              "kind", "youtube_artist", "youtube_title", "youtube_length", "match", "notes", "url",
              "track_id"]
_KIND = {SONG: "song", VIDEO: "video"}
_MATCH = {MATCHED: "same recording", UNCERTAIN: "probably the same", ALTERNATIVE: "another version"}


def write_youtube_reports(selection: Selection, found: dict[str, Found], reports_dir: Path,
                          favourite_plays: int) -> list[Path]:
    rows = []
    for wanted in selection.wanted:
        track = wanted.track
        result = found.get(track.id, Found(track.id))
        base = {"plays": track.plays, "artist": track.artist, "title": track.name,
                "album": track.album, "length": length(track.duration), "why": wanted.why,
                "spotify": wanted.spotify, "youtube": result.status, "track_id": track.id}
        for rank, pick in enumerate(result.picks, 1):
            rows.append({**base, "pick": rank, "kind": _KIND[pick.kind],
                         "youtube_artist": ", ".join(pick.artists), "youtube_title": pick.title,
                         "youtube_length": length(pick.seconds), "match": _MATCH[pick.status],
                         "notes": "; ".join(pick.notes), "url": pick.url})
        if not result.picks:
            rows.append(base)
    paths = [reports_dir / "youtube.csv", reports_dir / "youtube.md"]
    _write(paths[0], CSV_FIELDS, rows)
    paths[1].write_text(render_youtube(selection, found, favourite_plays), encoding="utf-8")
    return paths


def _track_cell(track: Track) -> str:
    return _md(f"{track.artist} - {track.name}") + (f" ({length(track.duration)})" if track.duration else "")


def render_youtube(selection: Selection, found: dict[str, Found], favourite_plays: int) -> str:
    by_status = {EXACT: [], SIMILAR: [], NOTHING: []}
    for wanted in selection.wanted:
        by_status[found.get(wanted.track.id, Found(wanted.track.id)).status].append(wanted)
    picks = len(playlist_order(selection, found))
    lines = [
        "# YouTube Music: favourites Spotify lacks in the exact version",
        "",
        f"{len(selection.wanted)} music tracks played {favourite_plays} times or more: "
        f"{len(by_status[EXACT])} found as the same recording, {len(by_status[SIMILAR])} with up to "
        f"{PICKS_WHEN_UNSURE} close results, {len(by_status[NOTHING])} with nothing close enough. "
        f"{picks} videos for the playlist, most played first.",
        "",
    ]
    for status, heading in ((EXACT, "Same recording"), (SIMILAR, "Close results, to pick by ear")):
        if not by_status[status]:
            continue
        lines += [f"## {heading}", "", "| Plays | Track | Why | YouTube Music |", "|---:|---|---|---|"]
        for wanted in by_status[status]:
            cells = [f"[{_md(p.label)}]({p.url}) {length(p.seconds)}, {_KIND[p.kind]}"
                     + ("" if p.status == MATCHED else f", {_MATCH[p.status]}")
                     for p in found[wanted.track.id].picks]
            lines.append(f"| {wanted.track.plays} | {_track_cell(wanted.track)} | {_md(wanted.why)} | "
                         + "<br>".join(cells) + " |")
        lines.append("")
    if by_status[NOTHING]:
        lines += ["## Nothing found on YouTube Music", "",
                  "Not in the playlist. The mp3 is the only copy; keep it or let it go.", "",
                  "| Plays | Track | Why |", "|---:|---|---|"]
        lines += [f"| {w.track.plays} | {_track_cell(w.track)} | {_md(w.why)} |" for w in by_status[NOTHING]]
        lines.append("")
    if selection.let_go:
        lines += ["## Left out in review", "",
                  f"Played {favourite_plays} times or more but left out on purpose: not searched.", "",
                  "| Plays | Track |", "|---:|---|"]
        lines += [f"| {t.plays} | {_track_cell(t)} |" for t in selection.let_go]
        lines.append("")
    return "\n".join(lines)


# --- the playlist ----------------------------------------------------------------------------

def playlist_order(selection: Selection, found: dict[str, Found]) -> list[str]:
    """Video IDs in the order they go in: most played tracks first, each track's best pick
    before its second; a video two tracks share goes in once."""
    order = []
    for wanted in selection.wanted:
        if wanted.track.id in found:
            order += [p.video_id for p in found[wanted.track.id].picks]
    return list(dict.fromkeys(order))


class PlaylistState:
    """The playlist this repo made, and every video it ever added. A video removed from the
    playlist by hand stays out: running the sync again only adds what was never there."""

    def __init__(self, path: Path):
        self.path = path
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.playlist_id: str = data.get("playlist_id", "")
        self.name: str = data.get("name", "")
        self.added: list[str] = data.get("added", [])

    @property
    def url(self) -> str:
        return f"https://music.youtube.com/playlist?list={self.playlist_id}"

    def save(self) -> None:
        _write_json(self.path, {"playlist_id": self.playlist_id, "name": self.name, "added": self.added})


@dataclass
class SyncOutcome:
    created: bool
    added: list[str]
    already: int  # in the playlist, or added by an earlier run
    failed: list[str]


def _succeeded(response) -> bool:
    return isinstance(response, dict) and "SUCCEEDED" in str(response.get("status", ""))


def sync_playlist(video_ids: list[str], client: PlaylistClient, state: PlaylistState, name: str,
                  progress: Callable[[str, int, int], None]) -> SyncOutcome:
    created = False
    if not state.playlist_id:
        playlist_id = client.create_playlist(name, DESCRIPTION, privacy_status="PRIVATE")
        if not isinstance(playlist_id, str) or not playlist_id:
            raise YouTubeError("YouTube did not create the playlist; check the account in a browser "
                               "and run `make youtube-auth` again if the login has expired")
        # Saved before anything is added, so a run that stops halfway never makes a second one.
        state.playlist_id, state.name, created = playlist_id, name, True
        state.save()
    present = {t.get("videoId") for t in client.get_playlist(state.playlist_id, limit=None).get("tracks") or []}
    done = present | set(state.added)
    todo = [v for v in video_ids if v not in done]
    added, failed = [], []
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        if _succeeded(client.add_playlist_items(state.playlist_id, batch, duplicates=False)):
            ok = batch
        else:
            # One unavailable video fails the whole edit; one by one, the rest still go in.
            ok = [v for v in batch if _succeeded(client.add_playlist_items(state.playlist_id, [v],
                                                                           duplicates=False))]
            failed += [v for v in batch if v not in ok]
        added += ok
        state.added += ok
        state.save()
        progress("added to the playlist", start + len(batch), len(todo))
    return SyncOutcome(created, added, len(video_ids) - len(todo), failed)
