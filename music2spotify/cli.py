"""Command line entry point: python -m music2spotify <command>."""

from __future__ import annotations

import argparse
import secrets
import sys
import time
from collections import Counter

from . import auth
from .config import Settings, load_settings
from .library import Library, LibraryError, load_library
from .matching import ALTERNATIVE, MATCHED, NOT_FOUND, SKIPPED, UNCERTAIN
from .pipeline import (
    JsonLinesCache,
    cached_lookup,
    cached_search,
    load_matches,
    load_overrides,
    match_library,
    save_matches,
    save_overrides,
)
from .reports import read_decisions, write_reports
from .spotify import RateLimited, SpotifyClient, SpotifyError, TokenStore


class UsageError(Exception):
    pass


def _tokens(settings: Settings) -> TokenStore:
    if not settings.client_id:
        raise UsageError("SPOTIFY_CLIENT_ID is not set; copy .env.example to .env and fill it in")
    return TokenStore(settings.token_path, settings.client_id, settings.accounts_base)


def _client(settings: Settings) -> SpotifyClient:
    return SpotifyClient(_tokens(settings), settings.api_base, settings.min_interval,
                         settings.max_retry_wait)


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


def login(settings: Settings, args: argparse.Namespace) -> int:
    tokens = _tokens(settings)
    verifier, challenge = auth.new_pkce()
    state = secrets.token_urlsafe(16)
    url = auth.authorize_url(settings.accounts_base, settings.client_id, settings.redirect_uri,
                             challenge, state)
    print("Open this link in your browser and allow access:\n")
    print(f"  {url}\n")
    print(f"Waiting for the redirect to {settings.redirect_uri} ...", flush=True)
    code = auth.wait_for_code(settings.listen_host, settings.redirect_uri, state)
    tokens.save(auth.exchange_code(settings.accounts_base, settings.client_id, code,
                                   settings.redirect_uri, verifier))
    me = _client(settings).me()
    print(f"Logged in as {me.get('display_name') or me.get('id')} ({me.get('id')}); "
          f"token saved to {settings.token_path}")
    return 0


def _progress(started: float):
    def show(done: int, total: int, counts: Counter) -> None:
        if done % 100 and done != total:
            return
        elapsed = time.monotonic() - started
        eta = elapsed / done * (total - done)
        print(f"{done}/{total}  matched {counts[MATCHED]}  uncertain {counts[UNCERTAIN]}  "
              f"other version {counts[ALTERNATIVE]}  not found {counts[NOT_FOUND]}  "
              f"skipped {counts[SKIPPED]}  ~{eta / 60:.0f} min left", flush=True)
    return show


def match(settings: Settings, args: argparse.Namespace) -> int:
    library = load_library(settings.library_path)
    overrides = load_overrides(settings.overrides_path)
    cache = JsonLinesCache(settings.cache_path)
    client = None if args.offline else _client(settings)
    search = cached_search(cache, client.search_tracks if client else None)
    lookup = cached_lookup(cache, client.track if client else None)

    only = None
    if args.track:
        only = args.track
    elif args.limit:
        only = list(library.tracks)[: args.limit]
    previous = load_matches(settings.matches_path) if settings.matches_path.exists() else {}

    started = time.monotonic()
    try:
        results = match_library(library, search, lookup, overrides, _progress(started), only)
    except RateLimited as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 3
    if only is not None:
        results = {**{k: v for k, v in previous.items() if k in library.tracks}, **results}
    save_matches(settings.matches_path, results)
    paths = write_reports(library, results, settings.reports_dir)
    counts = Counter(r.status for r in results.values())
    print(f"\n{len(results)} tracks: " + ", ".join(f"{n} {s}" for s, n in counts.most_common()))
    if client:
        print(f"{client.requests_made} requests to Spotify")
    print("Reports: " + ", ".join(str(p) for p in paths))
    return 0


def report(settings: Settings, args: argparse.Namespace) -> int:
    library = load_library(settings.library_path)
    paths = write_reports(library, load_matches(settings.matches_path), settings.reports_dir)
    print("Reports: " + ", ".join(str(p) for p in paths))
    return 0


def accept(settings: Settings, args: argparse.Namespace) -> int:
    decisions, problems = read_decisions(settings.reports_dir)
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    if problems:
        return 1
    overrides = load_overrides(settings.overrides_path)
    for decision in decisions:
        overrides[decision.track_id] = decision
    save_overrides(settings.overrides_path, overrides)
    kinds = Counter("rejected" if d.spotify == "none" else "accepted" for d in decisions)
    print(f"{len(decisions)} decisions saved to {settings.overrides_path} "
          f"({kinds['accepted']} accepted, {kinds['rejected']} rejected); "
          f"{len(overrides)} overrides in total")
    return 0


COMMANDS = {
    "summary": (summary, "print the library snapshot as a folder tree with counts"),
    "validate": (validate, "check the snapshot is complete and consistent"),
    "auth": (login, "log in to Spotify and store a refreshable token"),
    "match": (match, "find every library track on Spotify and write the reports"),
    "report": (report, "rewrite the reports from the saved matches"),
    "accept": (accept, "move decisions from review.csv and not-found.csv into overrides"),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="music2spotify")
    sub = parser.add_subparsers(dest="command", required=True)
    parsers = {name: sub.add_parser(name, help=help_text)
               for name, (_, help_text) in COMMANDS.items()}
    parsers["match"].add_argument("--offline", action="store_true",
                                  help="use cached searches only (re-score without requests)")
    parsers["match"].add_argument("--limit", type=int,
                                  help="match only the first N library tracks")
    parsers["match"].add_argument("--track", action="append",
                                  help="match only this Music persistent ID (repeatable)")
    args = parser.parse_args(argv)
    handler, _ = COMMANDS[args.command]
    try:
        return handler(load_settings(), args)
    except (LibraryError, UsageError, SpotifyError, FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
