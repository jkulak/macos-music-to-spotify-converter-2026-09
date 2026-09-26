// Snapshot of the Music.app library, printed to stdout as JSON.
//
//   osascript -l JavaScript exporter/export_library.js > data/library.json
//
// This is the only part of the project that runs on the host: Music.app is
// reachable only through Apple events. Everything downstream reads the JSON.
//
// Track properties are fetched with bulk getters (tracks.name(), ...): one
// Apple event per property instead of one per track and property, which keeps
// a 6,000-track library to about a second.

const TRACK_FIELDS = {
  id: "persistentID",
  class: "class",
  kind: "kind",
  mediaKind: "mediaKind",
  name: "name",
  artist: "artist",
  albumArtist: "albumArtist",
  album: "album",
  composer: "composer",
  genre: "genre",
  year: "year",
  duration: "duration",
  trackNumber: "trackNumber",
  trackCount: "trackCount",
  discNumber: "discNumber",
  discCount: "discCount",
  compilation: "compilation",
  plays: "playedCount",
  lastPlayed: "playedDate",
  skips: "skippedCount",
  rating: "rating",
  ratingKind: "ratingKind",
  albumRating: "albumRating",
  favorited: "favorited",
  disliked: "disliked",
  dateAdded: "dateAdded",
  bpm: "bpm",
  cloudStatus: "cloudStatus",
};

function bulk(elements, property, warnings, what) {
  try {
    return elements[property]();
  } catch (error) {
    warnings.push(`${what}: could not read ${property}: ${error}`);
    return null;
  }
}

function exportTracks(music, warnings) {
  const tracks = music.libraryPlaylists[0].tracks;
  const columns = {};
  for (const [key, property] of Object.entries(TRACK_FIELDS)) {
    columns[key] = bulk(tracks, property, warnings, "library");
  }
  const count = columns.id.length;
  const rows = [];
  for (let i = 0; i < count; i++) {
    const row = {};
    for (const key of Object.keys(TRACK_FIELDS)) {
      row[key] = columns[key] === null ? null : columns[key][i];
    }
    rows.push(row);
  }
  return rows;
}

function optional(read) {
  try {
    return read();
  } catch (error) {
    return null;
  }
}

function exportPlaylists(music, warnings) {
  const result = [];
  music.playlists().forEach((playlist, position) => {
    const cls = playlist.class();
    if (cls === "libraryPlaylist") return;
    const specialKind = optional(() => playlist.specialKind()) || "none";
    let kind;
    if (cls === "folderPlaylist") kind = "folder";
    else if (specialKind !== "none") return; // built-ins such as "Music"
    else if (cls === "userPlaylist") kind = playlist.smart() ? "smart" : "regular";
    else kind = "other";

    const name = playlist.name();
    // Bulk getters fail on an empty element list, so empty playlists skip them.
    const isEmpty = kind === "folder" || playlist.tracks.length === 0;
    result.push({
      id: playlist.persistentID(),
      name: name,
      kind: kind,
      class: cls,
      parentId: optional(() => playlist.parent.persistentID()),
      position: position,
      description: optional(() => playlist.description()) || "",
      // A folder's tracks are the union of its children, not content of its own.
      trackIds: isEmpty ? [] : bulk(playlist.tracks, "persistentID", warnings, name) || [],
    });
  });
  return result;
}

function run() {
  const music = Application("Music");
  const warnings = [];
  const tracks = exportTracks(music, warnings);
  const playlists = exportPlaylists(music, warnings);
  return JSON.stringify({
    format: 1,
    exportedAt: new Date().toISOString(),
    source: { app: "Music", version: optional(() => music.version()) },
    tracks: tracks,
    playlists: playlists,
    warnings: warnings,
  });
}
