import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from music2spotify.cli import main
from music2spotify.library import parse_library
from music2spotify.matching import ALTERNATIVE, MATCHED, NOT_FOUND, SKIPPED, UNCERTAIN
from music2spotify.pipeline import (
    NONE,
    SKIP,
    UNREVIEWED,
    MatchResult,
    Override,
    accept_open_proposals,
    load_overrides,
    save_matches,
    save_overrides,
)
from tests.builders import candidate, snapshot, track


class AcceptOpenProposalsTest(unittest.TestCase):
    def setUp(self):
        self.maybe = track(name="Harbour", artist="Kilo", plays=3)
        self.other = track(name="Night Drive (Velvet Kid Remix)", artist="Kilo", plays=40)
        self.taken = track(name="Low Tide", plays=12)
        self.left_out = track(name="Undertow", plays=15)
        self.lost = track(name="Zażółć", artist="Gęślą Jaźń", plays=50)
        self.done = track(name="Paper Lanterns", plays=5)
        self.memo = track(name="Harbour memo", genre="Voice Memo")
        raw = [self.maybe, self.other, self.taken, self.left_out, self.lost, self.done, self.memo]
        self.raw = raw
        self.library = parse_library(snapshot(raw))
        self.results = {
            self.maybe["id"]: MatchResult(self.maybe["id"], UNCERTAIN, candidate("Harbour", ["Kilo"], id="1" * 22)),
            self.other["id"]: MatchResult(self.other["id"], ALTERNATIVE, candidate("Night Drive", ["Kilo"], id="2" * 22)),
            self.taken["id"]: MatchResult(self.taken["id"], UNCERTAIN, candidate("Low Tide", ["Q"], id="3" * 22)),
            self.left_out["id"]: MatchResult(self.left_out["id"], ALTERNATIVE, candidate("Undertow", ["Q"], id="4" * 22)),
            self.lost["id"]: MatchResult(self.lost["id"], NOT_FOUND, candidate("Zazolc 2", ["Other"], id="5" * 22)),
            self.done["id"]: MatchResult(self.done["id"], MATCHED, candidate("Paper Lanterns", ["Q"], id="6" * 22)),
            self.memo["id"]: MatchResult(self.memo["id"], SKIPPED, candidate("Harbour", ["Kilo"], id="7" * 22)),
        }
        self.overrides = {
            self.taken["id"]: Override(self.taken["id"], "8" * 22, "linked by hand in review"),
            self.left_out["id"]: Override(self.left_out["id"], SKIP, "left out in review"),
        }

    def test_every_undecided_uncertain_or_other_version_proposal_is_taken_with_a_note(self):
        accepted = accept_open_proposals(self.library, self.results, self.overrides)
        self.assertEqual(accepted, [Override(self.maybe["id"], "1" * 22, UNREVIEWED),
                                    Override(self.other["id"], "2" * 22, UNREVIEWED)])

    def test_decisions_already_made_are_never_replaced(self):
        accepted = {o.track_id for o in accept_open_proposals(self.library, self.results, self.overrides)}
        self.assertNotIn(self.taken["id"], accepted)
        self.assertNotIn(self.left_out["id"], accepted)

    def test_tracks_not_found_matched_or_not_music_are_left_alone(self):
        accepted = {o.track_id for o in accept_open_proposals(self.library, self.results, {})}
        for raw in (self.lost, self.done, self.memo):
            self.assertNotIn(raw["id"], accepted)


class AcceptAllCommandTest(AcceptOpenProposalsTest):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", {"DATA_DIR": self.data, "FAVOURITE_PLAYS": "10"}), \
                redirect_stdout(out), redirect_stderr(err):
            code = main(["accept-all", *argv])
        self.assertEqual(code, 0, err.getvalue())
        return out.getvalue()

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = tmp.name
        Path(self.data, "library.json").write_text(json.dumps(snapshot(self.raw)), encoding="utf-8")
        save_matches(Path(self.data, "matches.json"), self.results)
        self.overrides[self.lost["id"]] = Override(self.lost["id"], NONE, "not on Spotify (review)")
        self.path = Path(self.data, "overrides.csv")
        save_overrides(self.path, self.overrides)

    def test_it_saves_the_proposals_and_says_how_many_of_each_kind_and_how_many_are_favourites(self):
        out = self.run_cli()
        self.assertIn("2 proposals accepted without review: 1 probably the same, 1 another version; "
                      "1 of them played 10 times or more.", out)
        self.assertIn("make apply", out)
        saved = load_overrides(self.path)
        self.assertEqual(saved[self.maybe["id"]].spotify, "1" * 22)
        self.assertEqual(saved[self.other["id"]].note, UNREVIEWED)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        self.run_cli()
        before = self.path.read_bytes()
        out = self.run_cli()
        self.assertIn("0 proposals accepted without review", out)
        self.assertEqual(self.path.read_bytes(), before)

    def test_undo_removes_exactly_what_it_wrote_and_keeps_every_reviewed_decision(self):
        self.run_cli()
        out = self.run_cli("--undo")
        self.assertIn("2 proposals accepted without review taken back", out)
        self.assertEqual(load_overrides(self.path), self.overrides)

    def test_the_overrides_file_is_replaced_whole_and_no_side_file_is_left_behind(self):
        self.run_cli()
        self.assertEqual(sorted(p.name for p in Path(self.data).iterdir()),
                         ["library.json", "matches.json", "overrides.csv"])


if __name__ == "__main__":
    unittest.main()
