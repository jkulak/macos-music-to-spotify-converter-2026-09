"""Move the files of tracks that are on Spotify out of the Music library.

Nothing is deleted. Files are moved into an archive folder (keeping
Artist/Album/file), every move is written to a manifest the moment it happens,
and `restore` puts them back. Tracks are removed from the Music library only
after their file has moved: deleting a track whose file is still in the Media
folder can make Music send the file to the Trash.

Runs on the host, like the exporter: it needs the files and Music's scripting.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .library import Library
from .matching import MATCHED
from .pipeline import MatchResult

MANIFEST_FIELDS = ["track_id", "source", "target", "size", "spotify", "label", "state"]
MOVED, REMOVED = "moved", "removed from Music"

Progress = Callable[[str, int, int], None]


@dataclass
class ArchiveEntry:
    track_id: str
    source: Path
    target: Path
    size: int
    spotify: str
    label: str
    state: str = ""


def plan_archive(library: Library, results: dict[str, MatchResult], locations: dict[str, str | None],
                 media_root: Path, archive_root: Path,
                 archived: Iterable[str] = ()) -> tuple[list[ArchiveEntry], list[str]]:
    """Which files would move where, and the problems that keep a track out.
    Tracks already archived by an earlier run are skipped, not reported."""
    archived = set(archived)
    entries, problems = [], []
    for track_id, result in results.items():
        if result.status != MATCHED or track_id not in library.tracks or track_id in archived:
            continue
        track = library.tracks[track_id]
        label = f"{track.artist} — {track.name}"
        location = locations.get(track_id)
        if not location:
            problems.append(f"{label}: Music has no file for it")
            continue
        source = Path(location)
        try:
            relative = source.relative_to(media_root)
        except ValueError:
            relative = Path("_outside-media") / source.name
        target = archive_root / relative
        if not source.exists():
            problems.append(f"{label}: file not found at {source}")
            continue
        entries.append(ArchiveEntry(track_id, source, target, source.stat().st_size,
                                    result.uri or "", label))
    entries.sort(key=lambda e: str(e.target).casefold())
    return entries, problems


def write_plan(entries: list[ArchiveEntry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.DictWriter(out, MANIFEST_FIELDS)
        writer.writeheader()
        for e in entries:
            writer.writerow(_row(e))


def _row(entry: ArchiveEntry) -> dict:
    return {"track_id": entry.track_id, "source": str(entry.source), "target": str(entry.target),
            "size": entry.size, "spotify": entry.spotify, "label": entry.label, "state": entry.state}


class Manifest:
    """Every move as it happens, appended and flushed, so an interrupted run
    still knows exactly which files are where."""

    def __init__(self, paths: Iterable[Path]):
        self.paths = list(paths)
        self.entries: dict[str, ArchiveEntry] = {}
        if self.paths[0].exists():
            with self.paths[0].open(encoding="utf-8-sig", newline="") as rows:
                for row in csv.DictReader(rows):
                    self.entries[row["track_id"]] = ArchiveEntry(
                        row["track_id"], Path(row["source"]), Path(row["target"]), int(row["size"] or 0),
                        row["spotify"], row["label"], row["state"])

    def archived(self) -> set[str]:
        return {k for k, e in self.entries.items() if e.state in (MOVED, REMOVED)}

    def record(self, entry: ArchiveEntry) -> None:
        self.entries[entry.track_id] = entry
        for path in self.paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8-sig", newline="") as out:
                writer = csv.DictWriter(out, MANIFEST_FIELDS)
                writer.writeheader()
                writer.writerows(_row(e) for e in self.entries.values())
            os.replace(tmp, path)


def move_files(entries: list[ArchiveEntry], manifest: Manifest,
               progress: Progress | None = None) -> list[str]:
    problems = []
    for done, entry in enumerate(entries, start=1):
        known = manifest.entries.get(entry.track_id)
        if known and known.state in (MOVED, REMOVED) and known.target.exists():
            continue  # moved by an earlier, interrupted run
        if entry.target.exists():
            problems.append(f"{entry.label}: {entry.target} already exists, left where it is")
            continue
        entry.target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry.source), str(entry.target))
        entry.state = MOVED
        manifest.record(entry)
        if progress:
            progress("moving files", done, len(entries))
    return problems


def restore(manifest: Manifest, progress: Progress | None = None) -> list[str]:
    problems = []
    entries = list(manifest.entries.values())
    for done, entry in enumerate(entries, start=1):
        if not entry.target.exists():
            if not entry.source.exists():
                problems.append(f"{entry.label}: not in the archive and not back in place")
            continue
        if entry.source.exists():
            problems.append(f"{entry.label}: {entry.source} exists again, archive copy kept")
            continue
        entry.source.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry.target), str(entry.source))
        entry.state = "restored"
        manifest.record(entry)
        if progress:
            progress("restoring files", done, len(entries))
    return problems


# --- Music.app ------------------------------------------------------------------

_LOCATIONS_JS = """
const tracks = Application("Music").libraryPlaylists[0].fileTracks;
const ids = tracks.persistentID(), locations = tracks.location();
JSON.stringify(ids.map((id, i) => [id, locations[i] ? locations[i].toString() : null]));
"""

_REMOVE_JS = """
function run(ids) {
  const library = Application("Music").libraryPlaylists[0];
  const missing = [];
  for (const id of ids) {
    const found = library.tracks.whose({persistentID: id});
    if (found.length === 0) { missing.push(id); continue; }
    found[0].delete();
  }
  return JSON.stringify(missing);
}
"""

Runner = Callable[[list[str]], str]


def _osascript(argv: list[str]) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout


def music_locations(run: Runner = _osascript) -> dict[str, str | None]:
    return dict(json.loads(run(["osascript", "-l", "JavaScript", "-e", _LOCATIONS_JS])))


def remove_from_music(manifest: Manifest, run: Runner = _osascript, batch: int = 100,
                      progress: Progress | None = None) -> list[str]:
    """Remove every moved track from the library, in batches so progress can be
    shown; tracks of an interrupted run are picked up again from the manifest."""
    todo = [e for e in manifest.entries.values() if e.state == MOVED]
    problems = []
    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        missing = set(json.loads(run(["osascript", "-l", "JavaScript", "-e", _REMOVE_JS,
                                      *[e.track_id for e in chunk]])))
        for entry in chunk:
            if entry.track_id in missing:
                problems.append(f"{entry.label}: no longer in the Music library")
            entry.state = REMOVED
            manifest.record(entry)
        if progress:
            progress("removing from Music", min(start + batch, len(todo)), len(todo))
    return problems


def backup_library(library_bundle: Path, archive_root: Path, today: date | None = None) -> Path:
    """A copy of Music's library database next to the archive: restoring it (with
    Music quit) brings back the removed tracks with their play counts and ratings."""
    target = archive_root / f"_music-library-backup-{(today or date.today()).isoformat()}.musiclibrary"
    if not target.exists():
        shutil.copytree(library_bundle, target)
    return target
