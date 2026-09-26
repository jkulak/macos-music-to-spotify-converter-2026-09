import csv
import tempfile
import unittest
from pathlib import Path

from music2spotify.library import parse_library
from music2spotify.matching import ALTERNATIVE, MATCHED, NOT_FOUND, SKIPPED, UNCERTAIN
from music2spotify.pipeline import NONE, MatchResult
from music2spotify.reports import coverage, read_decisions, render_markdown, write_reports
from tests.builders import candidate, playlist, snapshot, track


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as rows:
        return list(csv.DictReader(rows))


class ReportsTest(unittest.TestCase):
    def setUp(self):
        self.hit = track(name="Paper Lanterns", plays=40)
        self.miss = track(name="Night Drive (Velvet Kid Bootleg)", artist="Kilo", plays=3)
        self.popular_miss = track(name="Zażółć", artist="Gęślą Jaźń", plays=90)
        self.maybe = track(name="Harbour", artist="Kilo", plays=10)
        self.memo = track(name="New Recording", genre="Voice Memo", plays=0)
        folder = playlist("Added", kind="folder", id="F1")
        mix = playlist("2013-04 Mix", parent="F1",
                       tracks=[self.hit, self.miss, self.popular_miss, self.maybe, self.memo])
        self.library = parse_library(snapshot(
            [self.hit, self.miss, self.popular_miss, self.maybe, self.memo], [folder, mix]))
        self.results = {
            self.hit["id"]: MatchResult(self.hit["id"], MATCHED, candidate("Paper Lanterns", ["The Quiet Engines"], id="1" * 22)),
            self.miss["id"]: MatchResult(self.miss["id"], NOT_FOUND, candidate("Night Drive", ["Kilo"]),
                                         title=0.6, artist=1.0),
            self.popular_miss["id"]: MatchResult(self.popular_miss["id"], NOT_FOUND, notes=["no search results"]),
            self.maybe["id"]: MatchResult(self.maybe["id"], UNCERTAIN, candidate("Harbour", ["Kilo"], id="2" * 22),
                                          score=0.8, notes=["length differs by 40s"]),
            self.memo["id"]: MatchResult(self.memo["id"], SKIPPED, notes=["voice memo"], source="library"),
        }
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_not_found_tracks_are_listed_most_played_first_with_their_playlists(self):
        write_reports(self.library, self.results, self.dir)
        rows = read_csv(self.dir / "not-found.csv")
        self.assertEqual([r["title"] for r in rows], ["Zażółć", "Night Drive (Velvet Kid Bootleg)"])
        self.assertEqual(rows[0]["playlists"], "Added / 2013-04 Mix")
        self.assertEqual(rows[1]["why"], "closest result too different (title 60%, artist 100%)")
        self.assertTrue(rows[1]["closest_url"].startswith("https://open.spotify.com/track/"))
        self.assertIn("open.spotify.com/search/", rows[0]["search_url"])

    def test_review_lists_uncertain_matches_with_a_link_and_an_empty_accept_column(self):
        write_reports(self.library, self.results, self.dir)
        [row] = read_csv(self.dir / "review.csv")
        self.assertEqual((row["accept"], row["status"], row["why"]), ("", UNCERTAIN, "length differs by 40s"))
        self.assertEqual(row["spotify_url"], "https://open.spotify.com/track/" + "2" * 22)

    def test_csv_files_keep_polish_letters_for_spreadsheet_apps(self):
        write_reports(self.library, self.results, self.dir)
        raw = (self.dir / "not-found.csv").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn("Gęślą Jaźń", raw.decode("utf-8-sig"))

    def test_coverage_counts_matched_missing_and_non_music_per_playlist(self):
        [row] = coverage(self.library, self.results)
        self.assertEqual((row.total, row.non_music, row.matched, row.review, len(row.missing)), (5, 1, 1, 1, 3))
        self.assertEqual(row.percent, "25%")

    def test_the_markdown_report_shows_totals_and_what_each_playlist_misses(self):
        text = render_markdown(self.library, self.results)
        self.assertIn("| **Music** | **4** | 100% |", text)
        self.assertIn("| On Spotify, same version | 1 | 25.0% |", text)
        self.assertIn("| Added / 2013-04 Mix | regular | 5 | 1 | 1 | 1 | 3 | 25% |", text)
        self.assertIn("<details><summary>Added / 2013-04 Mix — 3 missing</summary>", text)
        self.assertIn("- voice memo: 1", text)

    def test_music_tracks_matching_one_spotify_track_are_listed_as_duplicates(self):
        twin = MatchResult(self.maybe["id"], MATCHED, self.results[self.hit["id"]].candidate)
        text = render_markdown(self.library, {**self.results, self.maybe["id"]: twin})
        self.assertIn("## Several Music tracks, one Spotify track", text)


class DecisionsTest(unittest.TestCase):
    def write(self, name, fields, rows):
        with Path(self.tmp.name, name).open("w", encoding="utf-8-sig", newline="") as out:
            writer = csv.DictWriter(out, fields)
            writer.writeheader()
            writer.writerows(rows)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_accepted_rejected_and_hand_found_tracks_become_overrides(self):
        self.write("review.csv", ["accept", "status", "spotify_url", "track_id"], [
            {"accept": "y", "status": ALTERNATIVE, "spotify_url": "https://open.spotify.com/track/" + "3" * 22, "track_id": "A"},
            {"accept": "N", "status": UNCERTAIN, "spotify_url": "https://open.spotify.com/track/" + "4" * 22, "track_id": "B"},
            {"accept": "", "status": UNCERTAIN, "spotify_url": "https://open.spotify.com/track/" + "5" * 22, "track_id": "C"},
        ])
        self.write("not-found.csv", ["spotify", "track_id"], [
            {"spotify": "https://open.spotify.com/track/" + "6" * 22 + "?si=x", "track_id": "D"},
            {"spotify": "", "track_id": "E"},
        ])
        decisions, problems = read_decisions(Path(self.tmp.name))
        self.assertEqual(problems, [])
        self.assertEqual([(d.track_id, d.spotify) for d in decisions],
                         [("A", "3" * 22), ("B", NONE), ("D", "6" * 22)])

    def test_an_unclear_answer_is_reported_with_its_line(self):
        self.write("review.csv", ["accept", "status", "spotify_url", "track_id"],
                   [{"accept": "maybe", "status": UNCERTAIN, "spotify_url": "", "track_id": "A"}])
        _, problems = read_decisions(Path(self.tmp.name))
        self.assertEqual(problems, ["review.csv:2: accept='maybe' is neither yes nor no"])


if __name__ == "__main__":
    unittest.main()
