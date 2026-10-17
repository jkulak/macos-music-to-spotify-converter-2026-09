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
- Matching fits the development-mode quotas: Spotify allows roughly 700 searches a day per
  developer account (`429 QUOTA_EXCEEDED`, Retry-After ~24 h), while album track lists are not
  counted as searches. Tracks sharing an album are matched from one album search, or with no search
  at all when another track of the album is already matched; the rest get one search each across
  the library before any track gets a second. When the quota runs out the run still saves matches
  and reports, says how many tracks remain and when Spotify allows more, and exits with code 3;
  `make match-until-done` waits and resumes by itself; `make migrate-until-done` also syncs the
  playlists after every run, so they fill up day by day.
- Titles or artists that only contain each other ("Threshold" / "Kitchen Threshold", "Beck" /
  "Jakob Beck") no longer count as equal; they match only when everything else agrees and the
  lengths are within 3 seconds, otherwise they go to review. "Re-Work" and a remix named at the end
  of a title without brackets are recognised as distinct versions.
- `make ui` (or `make tui`): a terminal UI to go through uncertain matches, other versions and
  missing tracks, most played first, with both versions playable for comparison (Spotify app and
  Music, via AppleScript on the Mac) and every decision saved to `data/overrides.csv` as it is made.
  `make apply` pushes the decisions to Spotify. A pasted Spotify link shows the title, artist and
  album it points at, and `p` then plays that track, so a hand-picked version can be checked.
- `make how` prints the whole flow on half a page: five steps, the make target behind each, and the
  few other targets used day to day.
- `make archive-matched` moves the files of tracks already on Spotify out of Music's media folder
  into `ARCHIVE_DIR` (default `~/Music/mp3-on-spotify`, keeping `Artist/Album/file`) and removes
  those tracks from the Music library. It is a dry run unless `CONFIRM=yes`; the Music library is
  backed up first, every move is recorded in a manifest, and `make restore-archived` moves the files
  back. Afterwards `make export` refuses to replace the snapshot, since one without the archived
  tracks would make the next sync drop them from the Spotify playlists.
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
- An `Insights` folder built from the listening history: Top 100 overall, rock and not rock;
  Top 25 for every genre with 50+ tracks; the 10 most played artists; 5 stars, 4 stars and up,
  forgotten gems and undiscovered songs; songs by the year they were last played and by release
  decade; and four BPM ranges for DJ sets. "Rock" and ratings follow the rules of the smart
  playlists in Music (genre contains "rock"; album-derived ratings count).
