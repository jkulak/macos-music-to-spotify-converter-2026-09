import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from music2spotify.archive import LET_GO, NO_COPY, NO_COPY_FOLDER, ON_SPOTIFY, ON_YOUTUBE, REMOVED, ArchiveEntry, Manifest
from music2spotify.cli import main
from music2spotify.collection import FIELDS, NOT_ARCHIVED, collection_rows, write_collection
from music2spotify.library import parse_library
from music2spotify.matching import MATCHED, NOT_FOUND, SKIPPED
from music2spotify.pipeline import UNREVIEWED, MatchResult, Override, save_matches, save_overrides
from music2spotify.youtube import Found, Pick, PlaylistState, save_found
from tests.builders import candidate, playlist, snapshot, track


def pick(video_id: str) -> Pick:
    return Pick(video_id, "Night Drive", ["Kilo"], "", 202.0, "song", MATCHED, 0.9)


class Collection:
    """A small library with one track for every fate a track can have."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.archive = self.root / "archive"
        self.on_spotify = track(name="Paper Lanterns", artist="Quiet", albumArtist="Quiet", album="Harbour",
                                trackNumber=2, plays=3, rating=80, duration=201.4)
        self.unreviewed = track(name="Lantern Walk", artist="Quiet", albumArtist="Quiet", album="Harbour",
                                trackNumber=1, plays=5)
        self.on_youtube = track(name="Night Drive", artist="Kilo", albumArtist="Kilo", album="Roads", plays=40)
        self.let_go = track(name="Seldom", artist="Kilo", albumArtist="Kilo", album="Roads", plays=4)
        self.only_copy = track(name="Garage\tJingle\n", artist="Radio Kilo", albumArtist="Radio Kilo",
                               album="Jingles", plays=20)
        self.kept = track(name="Cloud Song", artist="Zephyr", albumArtist="Zephyr", album="Up", plays=1)
        self.memo = track(name="Memo", artist="Kuba", albumArtist="Kuba", genre="Voice Memo", plays=80)
        self.raw = [self.on_spotify, self.unreviewed, self.on_youtube, self.let_go, self.only_copy, self.kept,
                    self.memo]
        folder = playlist("Added", kind="folder")
        self.playlists = [folder,
                          playlist("2013-04 Trap", parent=folder["id"], tracks=[self.on_spotify, self.on_youtube]),
                          playlist("Best", kind="smart", tracks=[self.on_spotify, self.on_spotify])]
        self.library = parse_library(snapshot(self.raw, self.playlists))
        self.results = {t["id"]: MatchResult(t["id"], NOT_FOUND) for t in self.raw}
        self.results[self.on_spotify["id"]] = MatchResult(
            self.on_spotify["id"], MATCHED, candidate("Paper Lanterns", ["Quiet"], id="1" * 22))
        self.results[self.unreviewed["id"]] = MatchResult(
            self.unreviewed["id"], MATCHED, candidate("Lantern Walk (Remix)", ["Quiet"], id="2" * 22),
            source="override")
        self.results[self.memo["id"]] = MatchResult(self.memo["id"], SKIPPED, source="library")
        self.overrides = {self.unreviewed["id"]: Override(self.unreviewed["id"], "2" * 22, UNREVIEWED)}
        self.found = {self.on_youtube["id"]: Found(self.on_youtube["id"], [pick("v1"), pick("v2")]),
                      self.only_copy["id"]: Found(self.only_copy["id"], [pick("v3")])}
        self.added = ["v1"]
        self.archived = {
            self.on_spotify["id"]: self.entry(self.on_spotify, "Quiet/Harbour/02 Paper Lanterns.mp3", ON_SPOTIFY,
                                              "spotify:track:" + "1" * 22),
            self.unreviewed["id"]: self.entry(self.unreviewed, "Quiet/Harbour/01 Lantern Walk.mp3", ON_SPOTIFY,
                                              "spotify:track:" + "2" * 22),
            self.on_youtube["id"]: self.entry(self.on_youtube, "Kilo/Roads/Night Drive.mp3", ON_YOUTUBE),
            self.let_go["id"]: self.entry(self.let_go, "Kilo/Roads/Seldom.mp3", LET_GO),
            self.only_copy["id"]: self.entry(self.only_copy, f"{NO_COPY_FOLDER}/Radio Kilo - Garage Jingle.mp3",
                                             NO_COPY),
        }

    def entry(self, raw, relative, why, spotify=""):
        return ArchiveEntry(raw["id"], Path("/media") / relative, self.archive / relative, 1000, spotify,
                            f"{raw['artist']} — {raw['name']}", REMOVED, why)


class CollectionRowsTest(Collection, unittest.TestCase):
    def rows(self):
        return collection_rows(self.library, self.results, self.overrides, self.found, self.added,
                               self.archived, self.archive)

    def row(self, raw):
        return next(r for r in self.rows() if r["title"] == " ".join(raw["name"].split()))

    def test_every_track_of_the_snapshot_gets_one_row_music_or_not(self):
        self.assertEqual(len(self.rows()), len(self.raw))
        self.assertEqual(set(self.rows()[0]), set(FIELDS))

    def test_rows_follow_the_albums_by_album_artist_album_and_track_number(self):
        titles = [r["title"] for r in self.rows()]
        self.assertEqual(titles, ["Night Drive", "Seldom", "Memo", "Lantern Walk", "Paper Lanterns",
                                  "Garage Jingle", "Cloud Song"])

    def test_an_archived_track_says_why_it_went_and_where_its_file_is_in_the_archive(self):
        row = self.row(self.only_copy)
        self.assertEqual(row["fate"], NO_COPY)
        self.assertEqual(row["archived as"], f"{NO_COPY_FOLDER}/Radio Kilo - Garage Jingle.mp3")
        self.assertEqual(self.row(self.let_go)["fate"], LET_GO)

    def test_a_track_on_spotify_carries_the_link_of_the_track_it_was_matched_to(self):
        row = self.row(self.on_spotify)
        self.assertEqual(row["fate"], ON_SPOTIFY)
        self.assertEqual(row["spotify"], "https://open.spotify.com/track/" + "1" * 22)

    def test_a_track_taken_without_review_says_spotify_may_hold_another_version(self):
        self.assertEqual(self.row(self.unreviewed)["fate"], f"{ON_SPOTIFY}, taken without review")

    def test_only_youtube_picks_that_went_into_the_playlist_are_listed(self):
        self.assertEqual(self.row(self.on_youtube)["youtube"], "https://music.youtube.com/watch?v=v1")
        self.assertEqual(self.row(self.only_copy)["youtube"], "")

    def test_a_track_that_is_not_music_says_it_stayed_in_music(self):
        row = self.row(self.memo)
        self.assertEqual(row["fate"], "not music (voice memo), stayed in Music")
        self.assertEqual(row["archived as"], "")

    def test_a_music_track_whose_file_never_left_music_says_it_was_not_archived(self):
        self.assertEqual(self.row(self.kept)["fate"], NOT_ARCHIVED)

    def test_playlists_carry_their_folder_path_and_each_appears_once(self):
        self.assertEqual(self.row(self.on_spotify)["playlists"], "Added / 2013-04 Trap | Best")
        self.assertEqual(self.row(self.let_go)["playlists"], "")

    def test_length_stars_and_dates_are_written_for_people(self):
        row = self.row(self.on_spotify)
        self.assertEqual((row["time"], row["stars"], row["plays"]), ("3:21", "4", "3"))
        self.assertEqual((row["added"], row["last played"]), ("2022-02-06", "2012-05-04"))
        self.assertEqual(self.row(self.kept)["stars"], "")

    def test_a_tab_or_line_break_in_a_tag_never_splits_a_track_over_two_lines(self):
        path = self.root / "out" / "collection.tsv"
        write_collection(self.rows(), path)
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), len(self.raw) + 1)
        self.assertEqual(lines[0].split("\t"), FIELDS)
        self.assertTrue(all(len(line.split("\t")) == len(FIELDS) for line in lines))
        self.assertIn("Radio Kilo\tGarage Jingle\tJingles", path.read_text(encoding="utf-8"))


class CollectionCommandTest(Collection, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "library.json").write_text(json.dumps(snapshot(self.raw, self.playlists)), encoding="utf-8")
        save_matches(self.data / "matches.json", self.results)
        save_overrides(self.data / "overrides.csv", self.overrides)
        save_found(self.data / "youtube-matches.json", self.found)
        state = PlaylistState(self.data / "youtube-state.json")
        state.playlist_id, state.added = "PL1", self.added
        state.save()
        manifest = Manifest([self.data / "archive-manifest.csv", self.archive / "manifest.csv"])
        for entry in self.archived.values():
            manifest.record(entry)
        self.out = self.root / "Music" / "music-collection.tsv"

    def run_cli(self):
        out, err = io.StringIO(), io.StringIO()
        env = {"DATA_DIR": str(self.data), "REPORTS_DIR": str(self.root / "reports"),
               "ARCHIVE_DIR": str(self.archive), "COLLECTION_FILE": str(self.out)}
        with mock.patch.dict("os.environ", env), redirect_stdout(out), redirect_stderr(err):
            code = main(["collection"])
        self.assertEqual(code, 0, err.getvalue())
        return out.getvalue()

    def snapshot_of_data(self):
        return {p.name: p.read_bytes() for p in sorted(self.data.iterdir())}

    def test_make_collection_writes_every_track_where_collection_file_points_and_says_so(self):
        output = self.run_cli()
        self.assertIn(f"{len(self.raw)} tracks, one per line, in {self.out}", output)
        lines = self.out.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), len(self.raw) + 1)
        self.assertIn("Kilo\tNight Drive\tRoads", "\n".join(lines))

    def test_the_command_changes_nothing_but_the_list(self):
        before = self.snapshot_of_data()
        self.run_cli()
        self.assertEqual(self.snapshot_of_data(), before)
        self.assertFalse((self.root / "reports").exists())

    def test_a_file_restored_to_music_is_not_listed_as_archived(self):
        manifest = Manifest([self.data / "archive-manifest.csv", self.archive / "manifest.csv"])
        entry = manifest.entries[self.let_go["id"]]
        entry.state = "restored"
        manifest.record(entry)
        self.run_cli()
        line = next(l for l in self.out.read_text(encoding="utf-8").splitlines() if "\tSeldom\t" in l)
        self.assertEqual(dict(zip(FIELDS, line.split("\t")))["fate"], NOT_ARCHIVED)

    def test_the_list_is_written_without_youtube_data_when_youtube_match_never_ran(self):
        (self.data / "youtube-matches.json").unlink()
        (self.data / "youtube-state.json").unlink()
        self.run_cli()
        self.assertEqual(len(self.out.read_text(encoding="utf-8").splitlines()), len(self.raw) + 1)


if __name__ == "__main__":
    unittest.main()
