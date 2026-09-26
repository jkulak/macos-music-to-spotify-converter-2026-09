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
