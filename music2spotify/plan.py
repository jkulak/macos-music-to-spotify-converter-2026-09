"""What should exist in Spotify: every Music playlist, the whole collection, extras.

Folders cannot be created through the Web API, so each playlist is named with
its folder path ("Added / 2013 / 2013-04 Trap Dubstep") until it has been moved
into the matching Spotify folder; then the names are switched to plain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .library import Library, Track
from .matching import SKIPPED
from .pipeline import MatchResult

ALL_SONGS = "All songs"
NAME_STYLES = ("path", "plain")
DESCRIPTION_LIMIT = 300  # Spotify cuts longer descriptions


@dataclass
class PlannedPlaylist:
    key: str  # stable identity across runs, e.g. "music:9C64AF57EEC6CB3A"
    folder_path: list[str]
    name: str
    uris: list[str]
    music_tracks: int  # tracks in the source that are music
    origin: str  # where the playlist comes from, for the description
    notes: list[str] = field(default_factory=list)

    @property
    def missing(self) -> int:
        return self.music_tracks - len(self.uris)

    def display_name(self, style: str) -> str:
        if style == "plain" or not self.folder_path:
            return self.name
        return " / ".join([*self.folder_path, self.name])

    @property
    def description(self) -> str:
        text = f"{self.origin} · {len(self.uris)} of {self.music_tracks} tracks on Spotify"
        if self.notes:
            text += " · " + " · ".join(self.notes)
        return text[:DESCRIPTION_LIMIT]


def _unique(uris: list[str | None]) -> tuple[list[str], int]:
    seen = dict.fromkeys(u for u in uris if u)
    return list(seen), sum(1 for u in uris if u) - len(seen)


def _planned(key: str, folder_path: list[str], name: str, tracks: list[Track],
             results: dict[str, MatchResult], origin: str, notes: list[str]) -> PlannedPlaylist:
    music = [t for t in tracks if t.id in results and results[t.id].status != SKIPPED]
    uris, doubles = _unique([results[t.id].uri for t in music])
    if doubles:
        notes = [*notes, f"{doubles} duplicate{'s' if doubles > 1 else ''} merged"]
    return PlannedPlaylist(key, folder_path, name, uris, len(music), origin, notes)


def all_songs_order(tracks: list[Track]) -> list[Track]:
    """Most played first, like the Songs view sorted by Plays."""
    return sorted(tracks, key=lambda t: (-t.plays, t.artist.casefold(), t.name.casefold()))


def build_plan(library: Library, results: dict[str, MatchResult], snapshot_date: str,
               extras: list[PlannedPlaylist] = ()) -> list[PlannedPlaylist]:
    plan = [_planned("all-songs", [], ALL_SONGS, all_songs_order(list(library.tracks.values())),
                     results, "Every song from the Music library, most played first", [])]
    for playlist in library.playlists():
        tracks = library.playlist_tracks(playlist)
        origin = "Music: " + " / ".join(playlist.path)
        notes = [f"smart playlist as of {snapshot_date}"] if playlist.kind == "smart" else []
        plan.append(_planned(f"music:{playlist.id}", playlist.folder_path, playlist.name,
                             tracks, results, origin, notes))
    return [*plan, *extras]


def folder_count(plan: list[PlannedPlaylist]) -> int:
    folders = set()
    for playlist in plan:
        for depth in range(1, len(playlist.folder_path) + 1):
            folders.add(tuple(playlist.folder_path[:depth]))
    return len(folders)


def render_plan(plan: list[PlannedPlaylist], style: str, root_folder: str,
                actions: dict[str, str] | None = None) -> str:
    """The layout as a checklist: which folders to create, what goes where."""
    actions = actions or {}
    lines = [
        "# Spotify layout",
        "",
        f"{len(plan)} playlists in {folder_count(plan)} folders under `{root_folder}`.",
        "",
    ]
    if style == "path":
        lines += [
            "Playlist names carry their folder path, so sorting Your Library alphabetically puts "
            "each folder's playlists together. Create the folders below inside "
            f"`{root_folder}` (right-click → Create folder), move each playlist into its folder "
            "(right-click → Move to folder), then run `make sync PLAYLIST_NAMES=plain` to drop "
            "the prefixes. Renaming does not take a playlist out of its folder.",
            "",
        ]
    lines += ["## Folders and playlists", "", f"- **{root_folder}/**"]
    placed: set[tuple[str, ...]] = set()
    for playlist in plan:
        for depth in range(1, len(playlist.folder_path) + 1):
            folder = tuple(playlist.folder_path[:depth])
            if folder not in placed:
                placed.add(folder)
                lines.append(f"{'  ' * depth}- [ ] **{folder[-1]}/**")
        indent = "  " * (len(playlist.folder_path) + 1)
        lines.append(f"{indent}- [ ] `{playlist.display_name(style)}` — "
                     f"{len(playlist.uris)} of {playlist.music_tracks}")
    lines += ["", "## Playlists", "",
              "| Spotify name | On Spotify | Music tracks | Missing | Last sync |",
              "|---|---:|---:|---:|---|"]
    for playlist in plan:
        name = playlist.display_name(style).replace("|", "\\|")
        lines.append(f"| {name} | {len(playlist.uris)} | {playlist.music_tracks} | "
                     f"{playlist.missing} | {actions.get(playlist.key, 'not synced yet')} |")
    return "\n".join(lines) + "\n"
