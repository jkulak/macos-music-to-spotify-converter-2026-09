# Changelog

Versions use CalVer, `YYYY.MM.PATCH` (tags `vYYYY.MM.PATCH`), chosen because there is no earlier
scheme and releases are occasional snapshots rather than a product line.

## [Unreleased]

### Added

- `make export` snapshots the Music.app library (tracks with play counts, ratings, BPM, dates;
  folders, smart and regular playlists in sidebar order with their current contents) to
  `data/library.json` through JXA, without the manual File > Library > Export step.
- `make summary` and `make smoke-export` print the snapshot as a folder tree and check it is
  consistent; voice memos, radio streams, documents, audiobooks, learning material and ringtones
  are classified as non-music.
- `make auth` logs in to Spotify with Authorization Code + PKCE (no client secret) through a
  loopback redirect, and keeps a refreshable token under `data/`.
- `make match` finds every track on Spotify, only accepting the same version: remasters, radio
  edits, explicit/clean releases and "original mix" count as the same; remixes, bootlegs, live,
  acoustic, instrumental and acapella takes do not and are only proposed. Titles with
  "Artist - Title", featured artists, Camelot keys or track numbers typed in are read several ways.
  Searches are cached and paced, 429 responses honoured, and a long ban stops the run cleanly.
  Search runs in an explicit market (`SPOTIFY_MARKET`, default `PL`): `market=from_token` needs a
  scope the app does not ask for, and a named market also tells which tracks cannot be played there.
- Reports in `reports/`: `report.md` with coverage and missing tracks per playlist,
  `not-found.csv`, `review.csv` and `skipped.csv`. `make accept` turns `y`/`n` answers in
  `review.csv` and links pasted into `not-found.csv` into `data/overrides.csv`.
- `make plan` and `make sync` create one private Spotify playlist per Music playlist (smart ones
  as a snapshot, in Music's order) plus "All songs", most played first. Re-running updates the
  same playlists; the mapping lives in `data/spotify-state.json`, and playlists the tool did not
  create are never touched.
- The Web API cannot create folders, so playlist names carry their folder path
  (`Added / 2013 / 2013-04 Trap Dubstep`) and `reports/plan.md` is the checklist for arranging
  them; `make sync PLAYLIST_NAMES=plain` then drops the prefixes.
