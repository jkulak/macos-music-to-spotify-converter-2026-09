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
