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

`make help` lists every target.

## Tests

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 test
```

`make test` is the gate: the repository is local and single-user, and the exporter needs Music.app
on the Mac, so there is no CI workflow. `make smoke-export` runs the exporter against the real
library and validates the result.
