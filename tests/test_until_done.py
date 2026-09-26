import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "until-done.sh"


class UntilDoneTest(unittest.TestCase):
    """scripts/until-done.sh against a stand-in for docker that plays back exit codes."""

    def run_script(self, mode, exit_codes):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "retry-after-seconds").write_text("0")
            (tmp / "codes").write_text("\n".join(map(str, exit_codes)) + "\n")
            docker = tmp / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                'for last; do :; done; echo "$last" >> "$FAKE/calls"\n'
                'code=$(head -n 1 "$FAKE/codes"); sed -i.bak 1d "$FAKE/codes"; exit "$code"\n')
            docker.chmod(0o755)
            env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", FAKE=str(tmp),
                       DATA_DIR=str(tmp), QUOTA_MARGIN="0")
            done = subprocess.run(["sh", str(SCRIPT), mode], env=env, capture_output=True, text=True)
            calls = (tmp / "calls").read_text().split()
        return done.returncode, calls, done.stdout

    def test_match_runs_again_after_a_quota_block_until_it_finishes(self):
        code, calls, out = self.run_script("match", [3, 3, 0])
        self.assertEqual((code, calls), (0, ["match", "match", "match"]))
        self.assertIn("Waiting 0s for the Spotify quota", out)

    def test_migrate_syncs_after_every_run_including_the_blocked_ones(self):
        code, calls, _ = self.run_script("migrate", [3, 0, 0, 0])
        self.assertEqual((code, calls), (0, ["match", "sync", "match", "sync"]))

    def test_a_real_failure_stops_the_loop_with_its_exit_code(self):
        code, calls, _ = self.run_script("migrate", [2])
        self.assertEqual((code, calls), (2, ["match"]))

    def test_a_failed_sync_stops_the_loop(self):
        code, calls, _ = self.run_script("migrate", [3, 1])
        self.assertEqual((code, calls), (1, ["match", "sync"]))


if __name__ == "__main__":
    unittest.main()
