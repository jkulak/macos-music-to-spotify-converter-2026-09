import json
import tempfile
import unittest
from pathlib import Path

from music2spotify.library import LibraryError, load_library, parse_library
from tests.builders import playlist, snapshot, track


def nested_library():
    a, b, c = track(name="A"), track(name="B"), track(name="C")
    added = playlist("Added", kind="folder", id="F1")
    year = playlist("2013", kind="folder", parent="F1", id="F2")
    trap = playlist("2013-04 Trap", parent="F2", tracks=[a, b])
    smart = playlist("Never played", kind="smart", tracks=[c])
    return parse_library(snapshot([a, b, c], [added, year, trap, smart]))


class LoadingASnapshotTest(unittest.TestCase):
    def test_loading_a_snapshot_rebuilds_the_folder_tree_in_sidebar_order(self):
        library = nested_library()
        self.assertEqual([p.name for p in library.roots], ["Added", "Never played"])
        self.assertEqual(
            [p.name for p in library.walk()],
            ["Added", "2013", "2013-04 Trap", "Never played"],
        )

    def test_a_playlist_path_lists_every_folder_it_is_nested_in(self):
        trap = nested_library().playlists()[0]
        self.assertEqual(trap.folder_path, ["Added", "2013"])
        self.assertEqual(trap.path, ["Added", "2013", "2013-04 Trap"])

    def test_folders_are_not_counted_as_playlists(self):
        library = nested_library()
        self.assertEqual([p.name for p in library.playlists()], ["2013-04 Trap", "Never played"])
        self.assertEqual([p.name for p in library.folders()], ["Added", "2013"])

    def test_playlist_tracks_keep_the_order_music_shows_them_in(self):
        library = nested_library()
        trap = library.playlists()[0]
        self.assertEqual([t.name for t in library.playlist_tracks(trap)], ["A", "B"])

    def test_playlist_items_missing_from_the_library_are_reported(self):
        a = track()
        broken = playlist("Broken", tracks=[a, {"id": "DEADBEEF00000000"}])
        library = parse_library(snapshot([a], [broken]))
        self.assertEqual(len(library.playlist_tracks(library.playlists()[0])), 1)
        self.assertIn("1 item(s) that are not in the library", library.problems[0])

    def test_a_playlist_whose_parent_is_missing_is_kept_at_the_top_level_and_reported(self):
        orphan = playlist("Orphan", parent="NOPE000000000000")
        library = parse_library(snapshot([], [orphan]))
        self.assertEqual([p.name for p in library.roots], ["Orphan"])
        self.assertIn("not in the snapshot", library.problems[0])

    def test_exporter_warnings_become_problems(self):
        library = parse_library(snapshot([], [], warnings=["Dj: could not read persistentID"]))
        self.assertEqual(library.problems, ["exporter: Dj: could not read persistentID"])

    def test_a_track_listed_twice_is_kept_once_and_reported(self):
        a = track(id="0000000000000001")
        library = parse_library(snapshot([a, dict(a)]))
        self.assertEqual(len(library.tracks), 1)
        self.assertIn("appears twice", library.problems[0])

    def test_an_unsupported_snapshot_format_is_refused(self):
        with self.assertRaisesRegex(LibraryError, "re-run `make export`"):
            parse_library(dict(snapshot([]), format=99))

    def test_a_missing_snapshot_file_explains_how_to_create_it(self):
        with self.assertRaisesRegex(LibraryError, "run `make export` first"):
            load_library(Path("/nonexistent/library.json"))

    def test_the_snapshot_is_read_from_disk_as_the_exporter_writes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "library.json"
            path.write_text(json.dumps(snapshot([track(name="Zażółć")])), encoding="utf-8")
            library = load_library(path)
        self.assertEqual([t.name for t in library.tracks.values()], ["Zażółć"])


class TrackFieldsTest(unittest.TestCase):
    def only_track(self, **fields):
        return next(iter(parse_library(snapshot([track(**fields)])).tracks.values()))

    def test_zero_year_and_zero_bpm_mean_unknown(self):
        t = self.only_track(year=0, bpm=0)
        self.assertIsNone(t.year)
        self.assertIsNone(t.bpm)

    def test_stars_follow_the_music_rating_scale_of_twenty_per_star(self):
        self.assertEqual(self.only_track(rating=80).stars, 4)
        self.assertEqual(self.only_track(rating=0).stars, 0)

    def test_never_played_tracks_have_no_last_played_date(self):
        t = self.only_track(plays=0, lastPlayed=None)
        self.assertIsNone(t.last_played)
        self.assertEqual(t.plays, 0)

    def test_dates_are_read_as_utc(self):
        t = self.only_track(lastPlayed="2016-10-11T11:03:09.000Z")
        self.assertEqual((t.last_played.year, t.last_played.utcoffset().total_seconds()), (2016, 0))


class NonMusicTest(unittest.TestCase):
    def reason(self, **fields):
        return next(iter(parse_library(snapshot([track(**fields)])).tracks.values())).non_music_reason()

    def test_voice_memos_audiobooks_learning_material_and_ringtones_are_not_music(self):
        self.assertEqual(self.reason(genre="Voice Memo"), "voice memo")
        self.assertEqual(self.reason(genre="Audiobook"), "audiobook")
        self.assertEqual(self.reason(genre="Learning material"), "learning material")
        self.assertEqual(self.reason(genre="ringtone"), "ringtone")

    def test_radio_streams_and_documents_are_not_music(self):
        self.assertEqual(self.reason(**{"class": "urlTrack", "kind": "Internet audio stream"}),
                         "radio stream")
        self.assertEqual(self.reason(kind="PDF document", genre="Rock"), "document")

    def test_songs_with_unusual_genres_are_still_music(self):
        self.assertIsNone(self.reason(genre="Effects"))
        self.assertIsNone(self.reason(genre=""))


if __name__ == "__main__":
    unittest.main()
