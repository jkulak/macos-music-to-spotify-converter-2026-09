# macOS Music to Spotify converter

Moves a Music.app library of local files to Spotify: one playlist with the whole collection, the
Music folder and playlist structure (smart playlists as a snapshot of their current contents), and
extra playlists built from the Music listening history. Whatever cannot be matched ends up in a
report.

## How it fits together

1. `make export` reads the library straight from Music.app with JXA (`osascript`) and writes
   `data/library.json`. This is the only step that runs on the host, because Music.app is only
   reachable through Apple events.
2. Everything else runs in Docker on the Python standard library, plus one pinned package in the
   image: [ytmusicapi](https://github.com/sigma67/ytmusicapi), for the YouTube playlist only.

`data/` and `reports/` hold personal listening history and tokens; both are gitignored.

The short version of everything below, as the five steps and the target behind each:

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 how
```

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

`migrate-until-done` does the same and syncs the playlists after every run, so they fill up while
the quota allows more searches each day.

To go through what matching could not decide, open the review UI. It runs on the Mac rather than
in Docker, because it plays both versions for comparison: the Spotify candidate in the Spotify app
and your file in Music.

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 ui
```

`make tui` is the same target.

Three lists (to decide, not found, decided), most played first. `y` takes the Spotify track, `n`
marks the song as not on Spotify, `x` leaves it out, `l` takes a pasted Spotify link, `u` undoes;
`p` / `m` play the Spotify and the Music version, space pauses, `o` opens the track or a search in
Spotify, `t` searches YouTube Music in the browser, `/` filters, `?` lists the keys. Every decision
is saved to `data/overrides.csv` at once.

Or skip the review and take every yellow (probably the same) and violet (another version) proposal
as it is. Decisions already made stay; the overrides it writes carry the note
`accepted without review`, and `UNDO=yes` removes exactly those:

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 accept-all
```

Then push the decisions to Spotify (track lookups and sync, no search quota):

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 apply
```

Alternatively, mark rows in `reports/review.csv` with `y` or `n` in `accept`, or paste a
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

Favourites Spotify does not have in the exact version can go to a private YouTube Music playlist
instead: every music track played `FAVOURITE_PLAYS` times or more (default 10) that was not found,
was marked "not on Spotify" in review, or was taken without review (`accept-all`). Tracks linked or
taken by hand are on Spotify as they should be, and tracks left out stay out. The search is
anonymous, songs (the released audio) first, then videos, and every result is scored like a Spotify
candidate. The exact recording is the only pick for its track; otherwise the two closest plausible
results are both kept, to pick by ear later. About 3 seconds per track, cached and resumable;
`reports/youtube.md` and `reports/youtube.csv` list the picks and the tracks with nothing found.

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 youtube-match
```

Writing the playlist needs your YouTube Music login, once. The command explains the steps: open
music.youtube.com logged in, copy the request headers of a "browse" request from the browser's
developer tools, paste them. They are saved to `data/youtube-auth.json` (`YOUTUBE_AUTH_FILE`),
readable by you only; logging out in the browser revokes them.

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 youtube-auth
```

Then fill the playlist (`YOUTUBE_PLAYLIST`, private), most played first. It is created once and
remembered in `data/youtube-state.json`; running it again adds only what is new, and a video removed
from the playlist by hand is not added back.

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 youtube-sync
```

Once tracks are on Spotify, their mp3s can leave the Music library. This is a dry run that lists
every file in `reports/archive-plan.csv`; add `CONFIRM=yes` to back up the Music library, move the
files into `~/Music/mp3-on-spotify` and remove the tracks from Music. `make restore-archived` moves
them back.

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 archive-matched
```

The rest of the music follows with `make archive-rest`, after `make youtube-sync`. Every remaining
music file goes into the same archive, and the dry run counts them by why they may go: on Spotify,
on YouTube (a pick of theirs is in the playlist), let go (played fewer than `FAVOURITE_PLAYS` times,
or left out in review) and no copy anywhere (a favourite on neither). The last kind is filed under
`_no-copy-anywhere/` in the archive, apart, so it can be looked through before the archive is
emptied. Voice memos, audiobooks and the other non-music tracks stay in Music. `CONFIRM=yes` moves
the files, with the same backup, manifest and `make restore-archived` as above.

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 archive-rest
```

`make help` lists every target.

## Tests

```bash
make -C /Users/kuba/Developer/macos-music-to-spotify-converter-2026-09 test
```

`make test` is the gate: the repository is local and single-user, and the exporter needs Music.app
on the Mac, so there is no CI workflow. `make smoke-export` runs the exporter against the real
library and validates the result.
