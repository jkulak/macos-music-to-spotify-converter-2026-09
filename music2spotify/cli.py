"""Command line entry point: python -m music2spotify <command>."""

from __future__ import annotations

import argparse
import secrets
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import auth
from .config import Settings, load_settings
from .insights import build_insights
from .library import Library, LibraryError, load_library
from .pipeline import (
    JsonLinesCache,
    Sources,
    load_matches,
    load_overrides,
    match_library,
    save_matches,
    save_overrides,
)
from .plan import NAME_STYLES, build_plan, folder_count, render_plan
from .reports import read_decisions, write_reports
from .spotify import RateLimited, SpotifyClient, SpotifyError, TokenStore
from .sync import SyncState, sync


class UsageError(Exception):
    pass


def _tokens(settings: Settings) -> TokenStore:
    if not settings.client_id:
        raise UsageError("SPOTIFY_CLIENT_ID is not set; copy .env.example to .env and fill it in")
    return TokenStore(settings.token_path, settings.client_id, settings.accounts_base)


def _client(settings: Settings) -> SpotifyClient:
    return SpotifyClient(_tokens(settings), settings.api_base, settings.min_interval,
                         settings.max_retry_wait, settings.market)


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


def _progress(stage: str, done: int, total: int) -> None:
    if done % 100 == 0 or done == total:
        print(f"{stage}: {done}/{total}", flush=True)


def match(settings: Settings, args: argparse.Namespace) -> int:
    library = load_library(settings.library_path)
    overrides = load_overrides(settings.overrides_path)
    client = None if args.offline else _client(settings)
    sources = Sources(JsonLinesCache(settings.cache_path), client)

    only = None
    if args.track:
        only = args.track
    elif args.limit:
        only = list(library.tracks)[: args.limit]
    previous = load_matches(settings.matches_path) if settings.matches_path.exists() else {}

    results = match_library(library, sources, overrides, _progress, only)
    if only is not None:
        results = {**{k: v for k, v in previous.items() if k in library.tracks}, **results}
    save_matches(settings.matches_path, results)
    paths = write_reports(library, results, settings.reports_dir)
    counts = Counter(r.status for r in results.values())
    unsearched = sum(1 for r in results.values() if r.unsearched)
    print(f"\n{len(results)} tracks: " + ", ".join(f"{n} {s}" for s, n in counts.most_common()))
    if client:
        print(f"{client.requests_made} requests to Spotify")
    print("Reports: " + ", ".join(str(p) for p in paths))

    blocked_file = settings.data_dir / "retry-after-seconds"
    if sources.blocked:
        blocked = sources.blocked
        until = datetime.now(timezone.utc) + timedelta(seconds=blocked.retry_after)
        blocked_file.write_text(str(blocked.retry_after))
        print(f"\nSpotify quota used up ({blocked.reason or 'rate limit'}): {unsearched} tracks "
              f"still have searches to run. More allowed after {until:%Y-%m-%d %H:%M} UTC; "
              "run the same command then (make match-until-done waits by itself).",
              file=sys.stderr)
        return 3
    blocked_file.unlink(missing_ok=True)
    if unsearched:
        print(f"{unsearched} tracks have searches that were not run (offline)", file=sys.stderr)
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


def _plan(settings: Settings):
    if settings.name_style not in NAME_STYLES:
        raise UsageError(f"PLAYLIST_NAMES must be one of {', '.join(NAME_STYLES)}")
    library = load_library(settings.library_path)
    results = load_matches(settings.matches_path)
    unmatched = [t for t in library.tracks if t not in results]
    if unmatched:
        raise UsageError(f"{len(unmatched)} library tracks have not been matched; run `make match`")
    return build_plan(library, results, library.exported_at[:10],
                      build_insights(library, results))


def plan(settings: Settings, args: argparse.Namespace) -> int:
    planned = _plan(settings)
    state = SyncState(settings.state_path)
    actions = {k: "synced before" for k in state.playlists}
    path = settings.reports_dir / "plan.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_plan(planned, settings.name_style, settings.root_folder, actions),
                    encoding="utf-8")
    new = sum(1 for p in planned if p.key not in state.playlists)
    print(f"{len(planned)} playlists in {folder_count(planned)} folders, "
          f"{sum(len(p.uris) for p in planned)} tracks in total; "
          f"{new} would be created, {len(planned) - new} updated if changed")
    print(f"Layout and checklist: {path}")
    return 0


def push(settings: Settings, args: argparse.Namespace) -> int:
    planned = _plan(settings)
    state = SyncState(settings.state_path)

    def show(name: str, action: str) -> None:
        print(f"{action:>14}  {name}", flush=True)

    try:
        actions = sync(planned, _client(settings), state, settings.name_style, show)
    except RateLimited as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 3
    path = settings.reports_dir / "plan.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_plan(planned, settings.name_style, settings.root_folder, actions),
                    encoding="utf-8")
    print(f"\n{len(actions)} playlists synced: "
          + ", ".join(f"{n} {a}" for a, n in Counter(actions.values()).most_common()))
    print(f"Layout and checklist: {path}")
    return 0


def review(settings: Settings, args: argparse.Namespace) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise UsageError("review needs a terminal; run `make review` on the Mac, not in Docker")
    from .review import ReviewSession, run

    library = load_library(settings.library_path)
    session = ReviewSession(library, load_matches(settings.matches_path), settings.overrides_path)
    before = dict(session.overrides)
    run(session)
    changed = sum(1 for k in set(before) | set(session.overrides)
                  if before.get(k) != session.overrides.get(k))
    print(f"{changed} decisions changed, saved in {settings.overrides_path}.")
    if changed:
        print("Push them to Spotify with: make apply")
    return 0


COMMANDS = {
    "summary": (summary, "print the library snapshot as a folder tree with counts"),
    "validate": (validate, "check the snapshot is complete and consistent"),
    "auth": (login, "log in to Spotify and store a refreshable token"),
    "match": (match, "find every library track on Spotify and write the reports"),
    "report": (report, "rewrite the reports from the saved matches"),
    "accept": (accept, "move decisions from review.csv and not-found.csv into overrides"),
    "plan": (plan, "write reports/plan.md: the playlists and folders sync would produce"),
    "sync": (push, "create or update the playlists in Spotify"),
    "review": (review, "browse, play and decide the open tracks in a terminal UI (host only)"),
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
