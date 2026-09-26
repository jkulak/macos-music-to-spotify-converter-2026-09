# macOS Music to Spotify converter

Moves a Music.app library of local files to Spotify: one playlist with the whole collection, the
Music folder and playlist structure (smart playlists as a snapshot of their current contents), and
extra playlists built from the Music listening history. Whatever cannot be matched ends up in a
report.

## How it fits together

1. `make export` reads the library straight from Music.app with JXA (`osascript`) and writes
   `data/library.json`. This is the only step that runs on the host, because Music.app is only
   reachable through Apple events.
2. Everything else runs in Docker on the Python standard library — no third-party packages.

`data/` and `reports/` hold personal listening history and tokens; both are gitignored.

## Setup

```bash
cp /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09/.env.example /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09/.env
```

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 export
```

Create an app at https://developer.spotify.com/dashboard (Web API, redirect URI
`http://127.0.0.1:8888/callback`), put its Client ID into `.env` as `SPOTIFY_CLIENT_ID`, then log in.
Development-mode apps need the owner to have Premium.

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 auth
```

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 match
```

`match` writes `reports/report.md`, `reports/not-found.csv`, `reports/review.csv` and
`reports/skipped.csv`. Interrupting it loses nothing: answers are cached in
`data/spotify-cache.jsonl`.

Development-mode apps get roughly 700 searches a day. Matching therefore goes album by album
(album track lists do not count as searches) and spreads single-track searches so every track gets
one before any gets two. When the quota runs out, `match` saves what it has and exits with code 3;
this keeps going by itself until everything has been searched (the Mac has to stay awake):

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 match-until-done
```

To correct the result, mark rows in `reports/review.csv` with `y` or `n` in `accept`, or paste a
Spotify link into the `spotify` column of `reports/not-found.csv`, then:

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 accept
```

Decisions end up in `data/overrides.csv` (`track_id,spotify,note`; `spotify` is a track link,
`skip` or `none`), which can also be edited by hand.

Besides the Music playlists and "All songs", the plan adds an `Insights` folder (rankings, genres,
ratings, a timeline of last-played years and release decades, BPM ranges); the rules are described
in `music2spotify/insights.py`.

Then create the playlists:

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 plan
```

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 sync
```

The Web API cannot create folders, so playlists are named with their folder path
(`Added / 2013 / 2013-04 Trap Dubstep`). `reports/plan.md` lists the folders to create in Spotify and
what goes into each. Once everything sits in its folder, drop the prefixes:

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 sync PLAYLIST_NAMES=plain
```

`make help` lists every target.

## Tests

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 test
```

`make test` is the gate: the repository is local and single-user, and the exporter needs Music.app
on the Mac, so there is no CI workflow. `make smoke-export` runs the exporter against the real
library and validates the result.
