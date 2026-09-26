"""Command line entry point: python -m music2spotify <command>."""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from .config import Settings, load_settings
from .library import Library, LibraryError, load_library


def _print_tree(library: Library) -> None:
    for playlist in library.walk():
        indent = "  " * len(playlist.folder_path)
        if playlist.is_folder:
            print(f"{indent}{playlist.name}/")
        else:
            print(f"{indent}{playlist.name} [{playlist.kind}] {len(playlist.track_ids)}")


def summary(settings: Settings, args: argparse.Namespace) -> int:
    library = load_library(settings.library_path)
    tracks = library.tracks.values()
    non_music = Counter(t.non_music_reason() for t in tracks if not t.is_music)
    kinds = Counter(p.kind for p in library.playlists())
    print(f"Snapshot {library.exported_at} from Music {library.source_version}")
    print(f"{len(library.tracks)} tracks, {sum(non_music.values())} of them not music: "
          + ", ".join(f"{n} {reason}" for reason, n in non_music.most_common()))
    print(f"{sum(kinds.values())} playlists ("
          + ", ".join(f"{n} {kind}" for kind, n in kinds.most_common())
          + f") in {len(library.folders())} folders")
    print()
    _print_tree(library)
    for problem in library.problems:
        print(f"warning: {problem}", file=sys.stderr)
    return 0


def validate(settings: Settings, args: argparse.Namespace) -> int:
    library = load_library(settings.library_path)
    if not library.tracks:
        print("the snapshot has no tracks", file=sys.stderr)
        return 1
    for problem in library.problems:
        print(f"problem: {problem}", file=sys.stderr)
    if library.problems:
        return 1
    print(f"ok: {len(library.tracks)} tracks, {len(library.playlists())} playlists, "
          f"{len(library.folders())} folders")
    return 0


COMMANDS = {
    "summary": (summary, "print the library snapshot as a folder tree with counts"),
    "validate": (validate, "check the snapshot is complete and consistent"),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="music2spotify")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, (_, help_text) in COMMANDS.items():
        sub.add_parser(name, help=help_text)
    args = parser.parse_args(argv)
    handler, _ = COMMANDS[args.command]
    try:
        return handler(load_settings(), args)
    except LibraryError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
