"""What made it to Spotify and what did not.

The CSV files are meant to be edited: `accept` in review.csv and `spotify` in
not-found.csv are read back by `make accept`. They are written with a BOM so
Numbers and Excel both open the Polish diacritics correctly.
"""

from __future__ import annotations

import csv
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from .library import Library, Playlist, Track
from .matching import ALTERNATIVE, MATCHED, NOT_FOUND, SKIPPED, UNCERTAIN
from .pipeline import NONE, MatchResult, Override, spotify_track_id

ENCODING = "utf-8-sig"

NOT_FOUND_FIELDS = ["spotify", "artist", "title", "album", "year", "length", "plays", "stars",
                    "genre", "playlists", "why", "closest_on_spotify", "closest_url",
                    "search_url", "track_id"]
REVIEW_FIELDS = ["accept", "status", "why", "music_artist", "music_title", "music_album",
                 "music_length", "spotify_artist", "spotify_title", "spotify_album",
                 "spotify_length", "spotify_url", "score", "plays", "playlists", "track_id"]
SKIPPED_FIELDS = ["reason", "artist", "title", "genre", "kind", "playlists", "track_id"]

YES = {"y", "yes", "t", "tak", "1", "x", "+"}
NO = {"n", "no", "nie", "0", "-"}


def length(seconds: float | None) -> str:
    if not seconds:
        return ""
    seconds = round(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def path_label(playlist: Playlist) -> str:
    return " / ".join(playlist.path)


def memberships(library: Library) -> dict[str, list[str]]:
    found: dict[str, list[str]] = defaultdict(list)
    for playlist in library.playlists():
        for track_id in dict.fromkeys(playlist.track_ids):
            found[track_id].append(path_label(playlist))
    return found


def search_url(track: Track) -> str:
    return "https://open.spotify.com/search/" + urllib.parse.quote(f"{track.artist} {track.name}")


def _why_not_found(result: MatchResult) -> str:
    if result.notes:
        return "; ".join(result.notes)
    if result.candidate:
        return (f"closest result too different (title {result.title:.0%}, "
                f"artist {result.artist:.0%})")
    return "no search results"


def _write(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=ENCODING, newline="") as out:
        writer = csv.DictWriter(out, fields)
        writer.writeheader()
        writer.writerows(rows)


def _by_plays(library: Library, results: list[MatchResult]) -> list[MatchResult]:
    return sorted(results, key=lambda r: (-library.tracks[r.track_id].plays,
                                          library.tracks[r.track_id].artist.lower(),
                                          library.tracks[r.track_id].name.lower()))


@dataclass
class Coverage:
    playlist: Playlist
    total: int
    non_music: int
    matched: int
    review: int
    missing: list[Track]

    @property
    def music(self) -> int:
        return self.total - self.non_music

    @property
    def percent(self) -> str:
        return f"{self.matched / self.music:.0%}" if self.music else "–"


def coverage(library: Library, results: dict[str, MatchResult]) -> list[Coverage]:
    rows = []
    for playlist in library.playlists():
        tracks = library.playlist_tracks(playlist)
        statuses = [results[t.id].status if t.id in results else NOT_FOUND for t in tracks]
        rows.append(Coverage(
            playlist=playlist,
            total=len(tracks),
            non_music=statuses.count(SKIPPED),
            matched=statuses.count(MATCHED),
            review=statuses.count(UNCERTAIN) + statuses.count(ALTERNATIVE),
            missing=[t for t, s in zip(tracks, statuses) if s not in (MATCHED, SKIPPED)],
        ))
    return rows


def write_reports(library: Library, results: dict[str, MatchResult], reports_dir: Path) -> list[Path]:
    member_of = memberships(library)
    by_status: dict[str, list[MatchResult]] = defaultdict(list)
    for result in results.values():
        by_status[result.status].append(result)

    not_found = []
    for result in _by_plays(library, by_status[NOT_FOUND]):
        track = library.tracks[result.track_id]
        closest = result.candidate
        not_found.append({
            "spotify": "", "artist": track.artist, "title": track.name, "album": track.album,
            "year": track.year or "", "length": length(track.duration), "plays": track.plays,
            "stars": track.stars or "", "genre": track.genre,
            "playlists": "; ".join(member_of.get(track.id, [])),
            "why": _why_not_found(result),
            "closest_on_spotify": closest.label if closest else "",
            "closest_url": closest.url if closest else "",
            "search_url": search_url(track), "track_id": track.id,
        })

    review = []
    for result in _by_plays(library, by_status[UNCERTAIN] + by_status[ALTERNATIVE]):
        track, candidate = library.tracks[result.track_id], result.candidate
        review.append({
            "accept": "", "status": result.status, "why": "; ".join(result.notes),
            "music_artist": track.artist, "music_title": track.name, "music_album": track.album,
            "music_length": length(track.duration),
            "spotify_artist": ", ".join(candidate.artists), "spotify_title": candidate.name,
            "spotify_album": candidate.album, "spotify_length": length(candidate.duration),
            "spotify_url": candidate.url, "score": f"{result.score:.2f}", "plays": track.plays,
            "playlists": "; ".join(member_of.get(track.id, [])), "track_id": track.id,
        })

    skipped = []
    for result in by_status[SKIPPED]:
        track = library.tracks[result.track_id]
        skipped.append({
            "reason": "; ".join(result.notes), "artist": track.artist, "title": track.name,
            "genre": track.genre, "kind": track.kind,
            "playlists": "; ".join(member_of.get(track.id, [])), "track_id": track.id,
        })

    paths = [reports_dir / "not-found.csv", reports_dir / "review.csv",
             reports_dir / "skipped.csv", reports_dir / "report.md"]
    _write(paths[0], NOT_FOUND_FIELDS, not_found)
    _write(paths[1], REVIEW_FIELDS, review)
    _write(paths[2], SKIPPED_FIELDS, skipped)
    paths[3].write_text(render_markdown(library, results), encoding="utf-8")
    return paths


def _md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(library: Library, results: dict[str, MatchResult]) -> str:
    counts = Counter(r.status for r in results.values())
    music = len(results) - counts[SKIPPED]
    overrides = sum(1 for r in results.values() if r.source == "override" and r.status == MATCHED)
    unsearched = sum(1 for r in results.values() if r.unsearched)

    def share(n: int) -> str:
        return f"{n / music:.1%}" if music else "–"

    lines = [
        "# Music → Spotify: migration report",
        "",
        f"Library snapshot: {library.exported_at} (Music {library.source_version}).",
        "",
        "| | Tracks | Share of music |",
        "|---|---:|---:|",
        f"| Library items | {len(results)} | |",
        f"| Not music (voice memos, radio, …) | {counts[SKIPPED]} | |",
        f"| **Music** | **{music}** | 100% |",
        f"| On Spotify, same version | {counts[MATCHED]} | {share(counts[MATCHED])} |",
        f"| Probably on Spotify, needs a look | {counts[UNCERTAIN]} | {share(counts[UNCERTAIN])} |",
        f"| Only another version on Spotify | {counts[ALTERNATIVE]} | {share(counts[ALTERNATIVE])} |",
        f"| Not found | {counts[NOT_FOUND]} | {share(counts[NOT_FOUND])} |",
        "",
    ]
    if overrides:
        lines += [f"{overrides} matches come from `data/overrides.csv`.", ""]
    if unsearched:
        lines += [f"**{unsearched} tracks were matched offline with some searches never run** — "
                  "run `make match` online to finish them.", ""]
    lines += [
        "Files next to this report:",
        "",
        "- `review.csv` — uncertain matches and other versions. Put `y` in `accept` to take the "
        "Spotify track, `n` to mark the song as not on Spotify, then run `make accept`.",
        "- `not-found.csv` — paste a Spotify link into `spotify` when you find a track by hand, "
        "then run `make accept`.",
        "- `skipped.csv` — library items that are not music.",
        "",
        "Only the same version counts as found. A remaster, radio edit, explicit or clean release "
        "or an \"original mix\" is the same version; a remix, bootleg, flip, VIP, live, acoustic, "
        "instrumental, acapella or cover is not, and is only proposed in `review.csv`.",
        "",
        "## Playlists",
        "",
        "| Playlist | Kind | Tracks | Not music | On Spotify | To review | Missing | Coverage |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    rows = coverage(library, results)
    for row in rows:
        lines.append(
            f"| {_md(path_label(row.playlist))} | {row.playlist.kind} | {row.total} | "
            f"{row.non_music} | {row.matched} | {row.review} | {len(row.missing)} | {row.percent} |"
        )
    lines += ["", "## Missing from each playlist", ""]
    for row in rows:
        if not row.missing:
            continue
        lines.append(f"<details><summary>{_md(path_label(row.playlist))} — "
                     f"{len(row.missing)} missing</summary>")
        lines.append("")
        for track in row.missing:
            status = results[track.id].status.replace("_", " ") if track.id in results else "not matched"
            lines.append(f"- {_md(track.artist)} – {_md(track.name)} ({status})")
        lines += ["", "</details>", ""]

    missing_artists = Counter(library.tracks[r.track_id].artist or "(no artist)"
                              for r in results.values() if r.status == NOT_FOUND)
    if missing_artists:
        lines += ["## Artists with the most tracks not found", "",
                  "| Artist | Not found |", "|---|---:|"]
        for artist, n in missing_artists.most_common(40):
            lines.append(f"| {_md(artist)} | {n} |")
        lines.append("")

    reasons = Counter(n for r in results.values() if r.status == SKIPPED for n in r.notes)
    if reasons:
        lines += ["## Not music", ""]
        lines += [f"- {reason}: {n}" for reason, n in reasons.most_common()]
        lines.append("")

    shared: dict[str, list[str]] = defaultdict(list)
    for result in results.values():
        if result.uri:
            shared[result.uri].append(result.track_id)
    duplicates = {uri: ids for uri, ids in shared.items() if len(ids) > 1}
    if duplicates:
        lines += ["## Several Music tracks, one Spotify track", "",
                  "These Music tracks all matched the same Spotify track (duplicate files, or "
                  "versions Spotify does not tell apart). \"All songs\" keeps it once.", ""]
        for uri, ids in duplicates.items():
            names = "; ".join(f"{library.tracks[i].artist} – {library.tracks[i].name}" for i in ids)
            lines.append(f"- [{_md(results[ids[0]].candidate.label)}]"
                         f"(https://open.spotify.com/track/{uri.rsplit(':', 1)[-1]}): {_md(names)}")
        lines.append("")
    return "\n".join(lines)


def read_decisions(reports_dir: Path) -> tuple[list[Override], list[str]]:
    """Decisions typed into review.csv and not-found.csv, plus problems found."""
    decisions, problems = [], []
    review = reports_dir / "review.csv"
    if review.exists():
        with review.open(encoding=ENCODING, newline="") as rows:
            for line, row in enumerate(csv.DictReader(rows), start=2):
                answer = (row.get("accept") or "").strip().lower()
                if not answer:
                    continue
                if answer in YES:
                    track_id = spotify_track_id(row.get("spotify_url") or "")
                    decisions.append(Override(row["track_id"], track_id,
                                              f"accepted from review ({row.get('status')})"))
                elif answer in NO:
                    decisions.append(Override(row["track_id"], NONE, "rejected in review"))
                else:
                    problems.append(f"review.csv:{line}: accept={answer!r} is neither yes nor no")
    not_found = reports_dir / "not-found.csv"
    if not_found.exists():
        with not_found.open(encoding=ENCODING, newline="") as rows:
            for line, row in enumerate(csv.DictReader(rows), start=2):
                value = (row.get("spotify") or "").strip()
                if not value:
                    continue
                track_id = spotify_track_id(value)
                if track_id is None:
                    problems.append(f"not-found.csv:{line}: {value!r} is not a Spotify track link")
                    continue
                decisions.append(Override(row["track_id"], track_id, "found by hand"))
    return decisions, problems
