import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from music2spotify.archive import (
    MOVED,
    REMOVED,
    Manifest,
    backup_library,
    move_files,
    plan_archive,
    remove_from_music,
    restore,
    write_plan,
)
from music2spotify.library import parse_library
from music2spotify.matching import MATCHED, NOT_FOUND
from music2spotify.pipeline import MatchResult
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


if __name__ == "__main__":
    unittest.main()
