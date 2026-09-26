import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from music2spotify.cli import main
from tests.builders import playlist, snapshot, track


class CommandLineTest(unittest.TestCase):
    def run_cli(self, data, *argv):
        with tempfile.TemporaryDirectory() as tmp:
            if data is not None:
                Path(tmp, "library.json").write_text(json.dumps(data), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict("os.environ", {"DATA_DIR": tmp}), \
                    redirect_stdout(out), redirect_stderr(err):
                code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_validate_accepts_a_consistent_snapshot(self):
        a = track()
        code, out, _ = self.run_cli(snapshot([a], [playlist("Mix", tracks=[a])]), "validate")
        self.assertEqual(code, 0)
        self.assertIn("ok: 1 tracks, 1 playlists, 0 folders", out)

    def test_validate_fails_when_a_playlist_item_is_missing(self):
        broken = playlist("Mix", tracks=[{"id": "DEADBEEF00000000"}])
        code, _, err = self.run_cli(snapshot([track()], [broken]), "validate")
        self.assertEqual(code, 1)
        self.assertIn("not in the library", err)

    def test_summary_prints_the_folder_tree_with_track_counts(self):
        a = track()
        folder = playlist("Best", kind="folder", id="F1")
        code, out, _ = self.run_cli(
            snapshot([a, track(genre="Voice Memo")],
                     [folder, playlist("Top", kind="smart", parent="F1", tracks=[a])]),
            "summary",
        )
        self.assertEqual(code, 0)
        self.assertIn("2 tracks, 1 of them not music: 1 voice memo", out)
        self.assertIn("Best/\n  Top [smart] 1", out)

    def test_a_missing_snapshot_is_an_error_with_a_hint(self):
        code, _, err = self.run_cli(None, "summary")
        self.assertEqual(code, 2)
        self.assertIn("make export", err)


class MatchCommandTest(unittest.TestCase):
    """The match command end to end, against the fake Spotify."""

    def setUp(self):
        from tests.fake_spotify import FakeSpotify

        self.fake = FakeSpotify()
        self.base = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"DATA_DIR": f"{self.tmp.name}/data", "REPORTS_DIR": f"{self.tmp.name}/reports",
                    "SPOTIFY_CLIENT_ID": "cid", "SPOTIFY_API_BASE": f"{self.base}/v1",
                    "SPOTIFY_ACCOUNTS_BASE": self.base, "SPOTIFY_MIN_INTERVAL": "0"}
        Path(self.env["DATA_DIR"]).mkdir()
        Path(self.env["DATA_DIR"], "spotify-token.json").write_text(json.dumps(
            {"access_token": "access-1", "refresh_token": "refresh-1", "expires_at": 4102444800}))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", self.env), redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_match_finds_tracks_writes_reports_and_a_rerun_uses_the_cache(self):
        self.fake.add_track("Paper Lanterns", ["The Quiet Engines"], duration_ms=201_500)
        found, lost = track(name="Paper Lanterns"), track(name="Unreleased Demo Tape", artist="Nobody")
        Path(self.env["DATA_DIR"], "library.json").write_text(
            json.dumps(snapshot([found, lost], [playlist("Mix", tracks=[found, lost])])))

        code, out, err = self.run_cli("match")
        self.assertEqual(code, 0, err)
        self.assertIn("2 tracks: 1 matched, 1 not_found", out)
        reports = Path(self.env["REPORTS_DIR"])
        self.assertTrue((reports / "report.md").exists())
        self.assertIn("Unreleased Demo Tape", (reports / "not-found.csv").read_text(encoding="utf-8-sig"))

        searches = len(self.fake.calls("GET", "/v1/search"))
        code, _, _ = self.run_cli("match")
        self.assertEqual(len(self.fake.calls("GET", "/v1/search")), searches)

    def test_accept_turns_a_hand_found_link_into_an_override_used_by_the_next_match(self):
        spotify_track = self.fake.add_track("Lost Session", ["Nobody"])
        lost = track(name="Demo Tape", artist="Nobody")
        Path(self.env["DATA_DIR"], "library.json").write_text(json.dumps(snapshot([lost])))
        self.run_cli("match")
        not_found = Path(self.env["REPORTS_DIR"], "not-found.csv")
        text = not_found.read_text(encoding="utf-8-sig").replace(
            "\n,", f"\nhttps://open.spotify.com/track/{spotify_track['id']},", 1)
        not_found.write_text(text, encoding="utf-8-sig")

        code, out, _ = self.run_cli("accept")
        self.assertEqual(code, 0)
        self.assertIn("1 decisions saved", out)
        code, out, _ = self.run_cli("match")
        self.assertIn("1 tracks: 1 matched", out)

    def test_match_without_a_client_id_explains_the_setup(self):
        self.env["SPOTIFY_CLIENT_ID"] = ""
        Path(self.env["DATA_DIR"], "library.json").write_text(json.dumps(snapshot([track()])))
        code, _, err = self.run_cli("match")
        self.assertEqual(code, 2)
        self.assertIn("SPOTIFY_CLIENT_ID is not set", err)

    def test_plan_then_sync_create_the_playlists_and_write_the_checklist(self):
        self.fake.add_track("Paper Lanterns", ["The Quiet Engines"], duration_ms=201_500)
        song = track(name="Paper Lanterns")
        Path(self.env["DATA_DIR"], "library.json").write_text(json.dumps(
            snapshot([song], [playlist("Best", kind="folder", id="F1"),
                              playlist("Top", kind="smart", parent="F1", tracks=[song])])))
        self.run_cli("match")

        code, out, _ = self.run_cli("plan")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"(\d+) playlists in \d+ folders, .*; \1 would be created")
        self.assertEqual(self.fake.playlists, {})

        code, out, err = self.run_cli("sync")
        self.assertEqual(code, 0, err)
        names = {p["name"] for p in self.fake.playlists.values()}
        self.assertLessEqual({"All songs", "Best / Top", "Insights / Rankings / Top 100"}, names)
        checklist = Path(self.env["REPORTS_DIR"], "plan.md").read_text(encoding="utf-8")
        self.assertIn("`Best / Top` — 1 of 1", checklist)
        self.assertIn("| Best / Top | 1 | 1 | 0 | created |", checklist)

    def test_plan_refuses_to_run_before_every_track_is_matched(self):
        Path(self.env["DATA_DIR"], "library.json").write_text(json.dumps(snapshot([track()])))
        code, _, err = self.run_cli("plan")
        self.assertEqual(code, 2)
        self.assertIn("does not exist; run `make match` first", err)

    def test_a_used_up_search_quota_saves_what_was_found_and_the_next_run_finishes(self):
        for name in ("Paper Lanterns", "Harbour", "Low Tide"):
            self.fake.add_track(name, ["The Quiet Engines"], album="Harbour Lights", duration_ms=201_500)
        self.fake.add_track("Night Drive", ["Kilo"], album="Single", duration_ms=201_500)
        songs = [track(name=n, album="Harbour Lights") for n in ("Paper Lanterns", "Harbour", "Low Tide")]
        loner = track(name="Night Drive", artist="Kilo", album="")
        Path(self.env["DATA_DIR"], "library.json").write_text(json.dumps(snapshot([*songs, loner])))

        self.fake.search_quota = 1  # enough for the album search only
        code, out, err = self.run_cli("match")
        self.assertEqual(code, 3)
        self.assertIn("4 tracks: 3 matched, 1 not_found", out)
        self.assertIn("QUOTA_EXCEEDED", err)
        self.assertIn("1 tracks still have searches to run", err)
        self.assertEqual(Path(self.env["DATA_DIR"], "retry-after-seconds").read_text(), "86000")

        self.fake.search_quota = None
        code, out, _ = self.run_cli("match")
        self.assertEqual(code, 0)
        self.assertIn("4 tracks: 4 matched", out)
        self.assertFalse(Path(self.env["DATA_DIR"], "retry-after-seconds").exists())
