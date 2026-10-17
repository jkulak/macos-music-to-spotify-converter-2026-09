import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from music2spotify import archive
from music2spotify.archive import (
    LET_GO,
    MOVED,
    NO_COPY,
    NO_COPY_FOLDER,
    ON_SPOTIFY,
    ON_YOUTUBE,
    REMOVED,
    Manifest,
    backup_library,
    move_files,
    plan_archive,
    plan_moves,
    remove_from_music,
    rest_reasons,
    restore,
    write_plan,
)
from music2spotify.cli import main
from music2spotify.library import parse_library
from music2spotify.matching import MATCHED, NOT_FOUND, SKIPPED
from music2spotify.pipeline import SKIP, MatchResult, Override, save_matches, save_overrides
from music2spotify.youtube import Found, Pick, PlaylistState, save_found
from tests.builders import candidate, snapshot, track


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.media = self.root / "Media" / "Music"
        self.archive = self.root / "archive"
        self.on_spotify = track(name="Paper Lanterns")
        self.missing = track(name="Unreleased")
        self.fileless = track(name="Ghost")
        self.library = parse_library(snapshot([self.on_spotify, self.missing, self.fileless]))
        self.results = {
            self.on_spotify["id"]: MatchResult(self.on_spotify["id"], MATCHED, candidate("Paper Lanterns", ["Q"], id="1" * 22)),
            self.missing["id"]: MatchResult(self.missing["id"], NOT_FOUND),
            self.fileless["id"]: MatchResult(self.fileless["id"], MATCHED, candidate("Ghost", ["Q"], id="2" * 22)),
        }
        self.song = self.media / "The Quiet Engines" / "Harbour Lights" / "03 Paper Lanterns.mp3"
        self.song.parent.mkdir(parents=True)
        self.song.write_bytes(b"ID3" + b"\0" * 997)
        lost = self.media / "Kilo" / "Unreleased.mp3"
        lost.parent.mkdir(parents=True)
        lost.write_bytes(b"x")
        self.locations = {self.on_spotify["id"]: str(self.song), self.missing["id"]: str(lost),
                          self.fileless["id"]: None}
        self.manifest_paths = [self.root / "data" / "archive-manifest.csv", self.archive / "manifest.csv"]

    def plan(self, archived=()):
        return plan_archive(self.library, self.results, self.locations, self.media, self.archive, archived)

    def test_only_tracks_on_spotify_are_planned_keeping_artist_and_album_folders(self):
        entries, problems = self.plan()
        self.assertEqual([e.track_id for e in entries], [self.on_spotify["id"]])
        self.assertEqual(entries[0].target, self.archive / "The Quiet Engines" / "Harbour Lights" / "03 Paper Lanterns.mp3")
        self.assertEqual((entries[0].size, entries[0].spotify), (1000, "spotify:track:" + "1" * 22))
        self.assertEqual(problems, [f"{self.library.tracks[self.fileless['id']].artist} — Ghost: Music has no file for it"])

    def test_the_dry_run_plan_lists_every_file_and_moves_nothing(self):
        entries, _ = self.plan()
        write_plan(entries, self.root / "plan.csv")
        rows = list(csv.DictReader((self.root / "plan.csv").open(encoding="utf-8-sig")))
        self.assertEqual(rows[0]["source"], str(self.song))
        self.assertTrue(self.song.exists())

    def test_moving_records_each_file_in_both_manifests(self):
        entries, _ = self.plan()
        manifest = Manifest(self.manifest_paths)
        self.assertEqual(move_files(entries, manifest), [])
        self.assertFalse(self.song.exists())
        self.assertTrue(entries[0].target.exists())
        for path in self.manifest_paths:
            self.assertEqual(Manifest([path]).entries[self.on_spotify["id"]].state, MOVED)

    def test_a_second_run_skips_what_is_already_archived(self):
        entries, _ = self.plan()
        manifest = Manifest(self.manifest_paths)
        move_files(entries, manifest)
        again, problems = self.plan(Manifest(self.manifest_paths).archived())
        self.assertEqual((again, problems[:0]), ([], []))

    def test_a_file_already_at_the_target_is_left_alone_and_reported(self):
        entries, _ = self.plan()
        entries[0].target.parent.mkdir(parents=True)
        entries[0].target.write_bytes(b"other")
        problems = move_files(entries, Manifest(self.manifest_paths))
        self.assertIn("already exists", problems[0])
        self.assertTrue(self.song.exists())

    def test_tracks_are_removed_from_music_only_after_their_file_moved(self):
        entries, _ = self.plan()
        manifest = Manifest(self.manifest_paths)
        calls = []
        def music(argv):
            calls.append(argv)
            return json.dumps([])
        self.assertEqual(remove_from_music(manifest, run=music), [])
        self.assertEqual(calls, [])  # nothing moved yet, nothing removed
        move_files(entries, manifest)
        remove_from_music(manifest, run=music)
        self.assertEqual(calls[0][-1], self.on_spotify["id"])
        self.assertEqual(Manifest(self.manifest_paths).entries[self.on_spotify["id"]].state, REMOVED)

    def test_removal_goes_in_batches_and_reports_tracks_music_no_longer_has(self):
        manifest = Manifest(self.manifest_paths)
        entries, _ = self.plan()
        move_files(entries, manifest)
        problems = remove_from_music(manifest, run=lambda argv: json.dumps(argv[5:]), batch=1)
        self.assertIn("no longer in the Music library", problems[0])

    def test_restore_moves_every_file_back(self):
        entries, _ = self.plan()
        manifest = Manifest(self.manifest_paths)
        move_files(entries, manifest)
        self.assertEqual(restore(Manifest(self.manifest_paths)), [])
        self.assertTrue(self.song.exists())
        self.assertFalse(entries[0].target.exists())

    def test_restore_never_overwrites_a_file_that_came_back_meanwhile(self):
        entries, _ = self.plan()
        manifest = Manifest(self.manifest_paths)
        move_files(entries, manifest)
        self.song.write_bytes(b"new")
        problems = restore(Manifest(self.manifest_paths))
        self.assertIn("archive copy kept", problems[0])
        self.assertEqual(self.song.read_bytes(), b"new")

    def test_the_music_library_is_copied_next_to_the_archive_once(self):
        bundle = self.root / "Music Library.musiclibrary"
        bundle.mkdir()
        (bundle / "Library.musicdb").write_bytes(b"db")
        copy = backup_library(bundle, self.archive, date(2026, 10, 9))
        self.assertEqual(copy.name, "_music-library-backup-2026-10-09.musiclibrary")
        self.assertEqual((copy / "Library.musicdb").read_bytes(), b"db")
        self.assertEqual(backup_library(bundle, self.archive, date(2026, 10, 9)), copy)


class RestLibrary:
    """Seven tracks, one for every way a file can leave Music (and one that never does)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.media = self.root / "Media" / "Music"
        self.archive = self.root / "archive"
        self.data = self.root / "data"
        self.on_spotify = track(name="Paper Lanterns", plays=3)
        self.on_youtube = track(name="Night Drive", artist="Kilo", plays=40)
        self.not_synced = track(name="Harbour", artist="Kilo", plays=30)
        self.rare = track(name="Seldom", artist="Kilo", plays=4)
        self.left_out = track(name="Undertow", artist="Kilo", plays=50)
        self.only_copy = track(name="Garage Jingle", artist="Radio Kilo", plays=20)
        self.memo = track(name="Memo", artist="Kuba", genre="Voice Memo", plays=80)
        self.raw = [self.on_spotify, self.on_youtube, self.not_synced, self.rare, self.left_out,
                    self.only_copy, self.memo]
        self.library = parse_library(snapshot(self.raw))
        self.results = {t["id"]: MatchResult(t["id"], NOT_FOUND) for t in self.raw}
        self.results[self.on_spotify["id"]] = MatchResult(
            self.on_spotify["id"], MATCHED, candidate("Paper Lanterns", ["Q"], id="1" * 22))
        self.results[self.left_out["id"]] = MatchResult(self.left_out["id"], SKIPPED, source="override")
        self.results[self.memo["id"]] = MatchResult(self.memo["id"], SKIPPED, source="library")
        self.overrides = {self.left_out["id"]: Override(self.left_out["id"], SKIP, "left out in review")}
        self.locations = {}
        for raw in self.raw:
            path = self.media / raw["artist"] / raw["album"] / f"{raw['name']}.mp3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 1000)
            self.locations[raw["id"]] = str(path)
        self.manifest_paths = [self.data / "archive-manifest.csv", self.archive / "manifest.csv"]


class ArchiveRestTest(RestLibrary, unittest.TestCase):
    def reasons(self):
        return rest_reasons(self.library, self.results, self.overrides, {self.on_youtube["id"]}, 10)

    def plan(self, archived=()):
        return plan_moves(self.library, self.reasons(), self.results, self.locations, self.media,
                          self.archive, archived)

    def test_tracks_on_spotify_and_favourites_with_a_pick_in_the_youtube_playlist_may_go(self):
        reasons = self.reasons()
        self.assertEqual(reasons[self.on_spotify["id"]], ON_SPOTIFY)
        self.assertEqual(reasons[self.on_youtube["id"]], ON_YOUTUBE)

    def test_tracks_played_less_than_the_threshold_or_left_out_in_review_are_let_go(self):
        reasons = self.reasons()
        self.assertEqual(reasons[self.rare["id"]], LET_GO)
        self.assertEqual(reasons[self.left_out["id"]], LET_GO)

    def test_a_favourite_on_neither_spotify_nor_the_youtube_playlist_has_no_copy_anywhere(self):
        reasons = self.reasons()
        self.assertEqual(reasons[self.only_copy["id"]], NO_COPY)
        self.assertEqual(reasons[self.not_synced["id"]], NO_COPY)  # picks found, playlist not synced

    def test_tracks_that_are_not_music_are_never_given_a_reason_or_planned(self):
        self.assertNotIn(self.memo["id"], self.reasons())
        self.assertNotIn(self.memo["id"], [e.track_id for e in self.plan()[0]])

    def test_files_with_no_copy_anywhere_are_planned_into_a_folder_of_their_own(self):
        targets = {e.track_id: e.target for e in self.plan()[0]}
        self.assertEqual(targets[self.only_copy["id"]],
                         self.archive / NO_COPY_FOLDER / "Radio Kilo" / "Harbour Lights" / "Garage Jingle.mp3")
        self.assertEqual(targets[self.rare["id"]], self.archive / "Kilo" / "Harbour Lights" / "Seldom.mp3")

    def test_the_reason_goes_into_the_plan_and_the_manifest(self):
        entries, _ = self.plan()
        write_plan(entries, self.root / "plan.csv")
        rows = list(csv.DictReader((self.root / "plan.csv").read_text(encoding="utf-8-sig").splitlines()))
        self.assertEqual({r["label"].split(" — ")[1]: r["why"] for r in rows}["Garage Jingle"], NO_COPY)
        move_files(entries, Manifest(self.manifest_paths))
        reloaded = Manifest(self.manifest_paths).entries
        self.assertEqual(reloaded[self.on_youtube["id"]].why, ON_YOUTUBE)
        self.assertEqual(reloaded[self.on_spotify["id"]].spotify, "spotify:track:" + "1" * 22)
        self.assertEqual(reloaded[self.rare["id"]].spotify, "")

    def test_a_manifest_written_before_reasons_existed_reads_as_tracks_on_spotify(self):
        old = self.data / "archive-manifest.csv"
        old.parent.mkdir(parents=True)
        old.write_text("track_id,source,target,size,spotify,label,state\n"
                       "AB,/m/a.mp3,/a/a.mp3,10,spotify:track:x,Q — A,removed from Music\n", encoding="utf-8")
        self.assertEqual(Manifest([old]).entries["AB"].why, ON_SPOTIFY)

    def test_an_interrupted_run_carries_on_where_it_stopped_and_moves_nothing_twice(self):
        entries, _ = self.plan()
        move_files(entries[:2], Manifest(self.manifest_paths))
        rest, problems = self.plan(Manifest(self.manifest_paths).archived())
        self.assertEqual(([e.track_id for e in rest], problems), ([e.track_id for e in entries[2:]], []))
        self.assertEqual(move_files(rest, Manifest(self.manifest_paths)), [])
        self.assertEqual(len(Manifest(self.manifest_paths).archived()), 6)


class ArchiveRestCommandTest(RestLibrary, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.reports = self.root / "reports"
        self.bundle = self.root / "Music Library.musiclibrary"
        self.bundle.mkdir()
        (self.data / "library.json").parent.mkdir(parents=True, exist_ok=True)
        (self.data / "library.json").write_text(json.dumps(snapshot(self.raw)), encoding="utf-8")
        save_matches(self.data / "matches.json", self.results)
        save_overrides(self.data / "overrides.csv", self.overrides)
        pick = lambda video_id: Pick(video_id, "Night Drive", ["Kilo"], "", 202.0, "song", MATCHED, 0.9)
        save_found(self.data / "youtube-matches.json",
                   {self.on_youtube["id"]: Found(self.on_youtube["id"], [pick("v1")]),
                    self.not_synced["id"]: Found(self.not_synced["id"], [pick("v2")])})
        state = PlaylistState(self.data / "youtube-state.json")
        state.playlist_id, state.added = "PL1", ["v1"]
        state.save()
        self.removed = []

    def run_cli(self, *argv):
        def music(argv):
            self.removed += argv[5:]
            return json.dumps([])

        real_remove = archive.remove_from_music
        out, err = io.StringIO(), io.StringIO()
        env = {"DATA_DIR": str(self.data), "REPORTS_DIR": str(self.reports), "ARCHIVE_DIR": str(self.archive),
               "MUSIC_MEDIA_DIR": str(self.media), "MUSIC_LIBRARY": str(self.bundle), "FAVOURITE_PLAYS": "10"}
        with mock.patch.dict("os.environ", env), \
                mock.patch("music2spotify.archive.music_locations", lambda: self.locations), \
                mock.patch("music2spotify.archive.remove_from_music",
                           lambda manifest, progress=None: real_remove(manifest, run=music, progress=progress)), \
                redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        self.assertEqual(code, 0, err.getvalue())
        return out.getvalue()

    def test_the_dry_run_counts_the_files_per_reason_and_moves_nothing(self):
        out = self.run_cli("archive-rest")
        self.assertIn("6 music files, 0.0 GB", out)
        for line in ("on Spotify            1", "on YouTube            1", "let go                2",
                     "no copy anywhere      2"):
            self.assertIn(line, out)
        self.assertIn("1 of them have YouTube picks not in the playlist yet: run make youtube-sync", out)
        self.assertIn("1 tracks that are not music", out)
        self.assertIn("make archive-rest CONFIRM=yes", out)
        self.assertTrue(all(Path(p).exists() for p in self.locations.values()))
        self.assertTrue((self.reports / "archive-rest-plan.csv").exists())
        self.assertEqual(self.removed, [])

    def test_confirm_moves_every_music_file_out_of_music_and_restore_brings_them_back(self):
        out = self.run_cli("archive-rest", "--confirm")
        self.assertIn("Removing 6 tracks from Music", out)
        self.assertEqual(sorted(self.removed), sorted(t["id"] for t in self.raw if t is not self.memo))
        self.assertTrue(Path(self.locations[self.memo["id"]]).exists())
        self.assertTrue((self.archive / NO_COPY_FOLDER / "Radio Kilo" / "Harbour Lights" / "Garage Jingle.mp3").exists())
        self.assertTrue(any(self.archive.glob("_music-library-backup-*.musiclibrary")))
        self.assertIn("0 music files", self.run_cli("archive-rest"))

        self.run_cli("restore-archived")
        self.assertTrue(all(Path(p).exists() for p in self.locations.values()))


if __name__ == "__main__":
    unittest.main()
