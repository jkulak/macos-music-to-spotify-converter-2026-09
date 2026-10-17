import csv
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
    JsonLinesCache,
    MatchResult,
    Override,
    save_matches,
    save_overrides,
)
from music2spotify.youtube import (
    DECIDED_NOT_ON_SPOTIFY,
    EXACT,
    NOT_ON_SPOTIFY,
    NOTHING,
    SIMILAR,
    UNSURE_ON_SPOTIFY,
    Found,
    Pick,
    PlaylistState,
    Searcher,
    Selection,
    Wanted,
    YouTubeError,
    choose,
    clean_title,
    playlist_order,
    select_favourites,
    slim,
    sync_playlist,
    write_youtube_reports,
    youtube_queries,
)
from tests.builders import candidate, library_track, snapshot, track


def song(video_id, title, artists, seconds=202, album="Harbour Lights"):
    return {"resultType": "song", "videoId": video_id, "title": title,
            "artists": [{"name": a, "id": None} for a in artists], "album": {"name": album, "id": "MPRE"},
            "duration": f"{seconds // 60}:{seconds % 60:02d}", "duration_seconds": seconds,
            "videoType": "MUSIC_VIDEO_TYPE_ATV"}


def video(video_id, title, uploader, seconds=202):
    return {"resultType": "video", "videoId": video_id, "title": title,
            "artists": [{"name": uploader, "id": None}], "album": None,
            "duration": f"{seconds // 60}:{seconds % 60:02d}", "duration_seconds": seconds,
            "videoType": "MUSIC_VIDEO_TYPE_UGC"}


def results(*raw, kind="song"):
    return [(slim(r), kind) for r in raw]


class FakeYouTube:
    """Searches answered from a table; one playlist store, as YouTube Music keeps it."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.searches = []
        self.created = []
        self.playlists = {}
        self.edits = []
        self.unavailable = set()
        self.refuse_create = False

    def search(self, query, filter=None, limit=20):
        self.searches.append((filter, query))
        return self.answers.get((filter, query), [])

    def create_playlist(self, title, description, privacy_status="PRIVATE"):
        self.created.append((title, privacy_status))
        if self.refuse_create:
            return {"error": "no"}
        playlist_id = f"PL{len(self.created)}"
        self.playlists[playlist_id] = []
        return playlist_id

    def get_playlist(self, playlistId, limit=100):  # noqa: N803
        return {"id": playlistId, "tracks": [{"videoId": v} for v in self.playlists[playlistId]]}

    def add_playlist_items(self, playlistId, videoIds, duplicates=False):  # noqa: N803
        self.edits.append(list(videoIds))
        items = self.playlists[playlistId]
        if any(v in self.unavailable for v in videoIds) or any(v in items for v in videoIds):
            return {"actions": []}  # YouTube answers a refused edit without a status
        items.extend(videoIds)
        return {"status": "STATUS_SUCCEEDED", "playlistEditResults": []}


class SelectFavouritesTest(unittest.TestCase):
    def setUp(self):
        self.lost = track(name="Night Drive", artist="Kilo", plays=40)
        self.lost_less = track(name="Harbour", artist="Kilo", plays=12)
        self.missing = track(name="Zażółć", artist="Gęślą Jaźń", plays=15)
        self.unsure = track(name="Low Tide (Velvet Kid Remix)", artist="Q", plays=30)
        self.linked = track(name="Paper Lanterns", plays=50)
        self.left_out = track(name="Undertow", plays=25)
        self.on_spotify = track(name="Lighthouse", plays=60)
        self.rare = track(name="Seldom", plays=9)
        self.memo = track(name="Night Drive memo", genre="Voice Memo", plays=80)
        raw = [self.lost, self.lost_less, self.missing, self.unsure, self.linked, self.left_out,
               self.on_spotify, self.rare, self.memo]
        self.library = parse_library(snapshot(raw))
        spotify = candidate("Low Tide", ["Q"], id="1" * 22)
        self.results = {
            self.lost["id"]: MatchResult(self.lost["id"], NOT_FOUND),
            self.lost_less["id"]: MatchResult(self.lost_less["id"], ALTERNATIVE, candidate("Harbour", ["Kilo"])),
            self.missing["id"]: MatchResult(self.missing["id"], NOT_FOUND),
            self.unsure["id"]: MatchResult(self.unsure["id"], MATCHED, spotify, source="override"),
            self.linked["id"]: MatchResult(self.linked["id"], MATCHED, candidate("Paper Lanterns", ["Q"])),
            self.left_out["id"]: MatchResult(self.left_out["id"], SKIPPED),
            self.on_spotify["id"]: MatchResult(self.on_spotify["id"], MATCHED, candidate("Lighthouse", ["Q"])),
            self.rare["id"]: MatchResult(self.rare["id"], NOT_FOUND),
            self.memo["id"]: MatchResult(self.memo["id"], SKIPPED),
        }
        self.overrides = {
            self.missing["id"]: Override(self.missing["id"], NONE, "not on Spotify (review)"),
            self.unsure["id"]: Override(self.unsure["id"], "1" * 22, UNREVIEWED),
            self.linked["id"]: Override(self.linked["id"], "2" * 22, "linked by hand in review"),
            self.left_out["id"]: Override(self.left_out["id"], SKIP, "left out in review"),
        }
        self.selection = select_favourites(self.library, self.results, self.overrides, 10)

    def wanted(self):
        return {w.track.id: w for w in self.selection.wanted}

    def test_favourites_spotify_lacks_are_wanted_most_played_first(self):
        self.assertEqual([w.track.id for w in self.selection.wanted],
                         [self.lost["id"], self.unsure["id"], self.missing["id"], self.lost_less["id"]])
        self.assertEqual(self.wanted()[self.lost["id"]].why, NOT_ON_SPOTIFY)

    def test_a_favourite_marked_not_on_spotify_in_review_is_wanted(self):
        self.assertEqual(self.wanted()[self.missing["id"]].why, DECIDED_NOT_ON_SPOTIFY)

    def test_a_favourite_accepted_without_review_is_wanted_with_what_spotify_has_instead(self):
        wanted = self.wanted()[self.unsure["id"]]
        self.assertEqual((wanted.why, wanted.spotify), (UNSURE_ON_SPOTIFY, "Q - Low Tide"))

    def test_favourites_linked_by_hand_or_matched_exactly_are_not_wanted(self):
        self.assertNotIn(self.linked["id"], self.wanted())
        self.assertNotIn(self.on_spotify["id"], self.wanted())

    def test_favourites_left_out_in_review_are_let_go_and_not_searched(self):
        self.assertNotIn(self.left_out["id"], self.wanted())
        self.assertEqual([t.id for t in self.selection.let_go], [self.left_out["id"]])

    def test_tracks_played_less_than_the_threshold_and_anything_not_music_are_ignored(self):
        self.assertNotIn(self.rare["id"], self.wanted())
        self.assertNotIn(self.memo["id"], self.wanted())


class CleanTitleTest(unittest.TestCase):
    def test_what_only_a_video_upload_adds_is_dropped_from_the_title(self):
        for raw in ("Night Drive (Official Video)", "Night Drive [Official Music Video]",
                    "Night Drive (Lyrics)", "Night Drive | Official Audio", "Night Drive HD",
                    "Night Drive (Visualizer)"):
            self.assertEqual(clean_title(raw), "Night Drive", raw)

    def test_a_version_in_brackets_stays_even_next_to_video_noise(self):
        self.assertEqual(clean_title("Night Drive (Velvet Kid Remix) [Official Video]"),
                         "Night Drive (Velvet Kid Remix)")
        self.assertEqual(clean_title("Night Drive (Official Remix)"), "Night Drive (Official Remix)")


class ChooseTest(unittest.TestCase):
    def setUp(self):
        self.track = library_track(name="Night Drive", artist="Kilo", album="Harbour Lights")
        self.remix = library_track(name="Night Drive (Velvet Kid Remix)", artist="Kilo")

    def test_the_exact_recording_is_the_only_pick(self):
        picks = choose(self.track, results(song("a", "Night Drive", ["Kilo"]),
                                           song("b", "Night Drive (Live)", ["Kilo"]),
                                           song("c", "Night Drive", ["Kilo"], seconds=260)))
        self.assertEqual([(p.video_id, p.status) for p in picks], [("a", MATCHED)])

    def test_an_art_track_wins_over_a_video_of_the_same_recording(self):
        found = (results(video("v", "Kilo - Night Drive (Official Video)", "Kilo", seconds=201), kind="video")
                 + results(song("s", "Night Drive", ["Kilo"], seconds=206, album="Other")))
        self.assertEqual([(p.video_id, p.kind) for p in choose(self.track, found)], [("s", "song")])

    def test_without_the_exact_recording_the_two_closest_plausible_results_are_picked(self):
        picks = choose(self.remix, results(song("far", "Night Drive (Other Guy Remix)", ["Kilo"], seconds=260),
                                           song("near", "Night Drive", ["Kilo"], seconds=202),
                                           song("mid", "Night Drive (Extended Mix)", ["Kilo"], seconds=230)))
        self.assertEqual([p.video_id for p in picks], ["near", "mid"])
        self.assertEqual({p.status for p in picks}, {ALTERNATIVE})

    def test_one_plausible_result_means_one_pick(self):
        picks = choose(self.track, results(song("long", "Night Drive", ["Kilo"], seconds=262),
                                           song("other", "Sunrise", ["Kilo"])))
        self.assertEqual([(p.video_id, p.status) for p in picks], [("long", UNCERTAIN)])

    def test_another_artist_or_another_title_is_never_picked(self):
        picks = choose(self.track, results(song("x", "Night Drive", ["Someone Else"]),
                                           song("y", "Sunrise Avenue", ["Kilo"]),
                                           song("z", "Completely Different", ["Nobody"])))
        self.assertEqual(picks, [])

    def test_a_video_titled_artist_dash_title_by_an_uploader_counts_as_that_artist(self):
        picks = choose(self.track, results(video("u", "Kilo - Night Drive (Official Video)", "Random Uploads"),
                                           kind="video"))
        self.assertEqual([(p.video_id, p.status, p.artists, p.title) for p in picks],
                         [("u", MATCHED, ["Kilo"], "Night Drive")])


class SearcherTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache_path = Path(tmp.name, "youtube-cache.jsonl")
        self.track = library_track(name="Night Drive", artist="Kilo")
        self.query = youtube_queries(self.track)[0]

    def searcher(self, fake):
        return Searcher(JsonLinesCache(self.cache_path), lambda: fake, min_interval=0)

    def test_videos_are_searched_only_when_the_songs_lack_the_exact_recording(self):
        exact = FakeYouTube({("songs", self.query): [song("a", "Night Drive", ["Kilo"])]})
        found = self.searcher(exact).find(self.track)
        self.assertEqual((found.status, exact.searches), (EXACT, [("songs", self.query)]))

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache_path = Path(tmp.name, "youtube-cache.jsonl")
        close = FakeYouTube({("songs", self.query): [song("b", "Night Drive", ["Kilo"], seconds=262)],
                             ("videos", self.query): [video("c", "Kilo - Night Drive", "Kilo")]})
        found = self.searcher(close).find(self.track)
        self.assertEqual(close.searches, [("songs", self.query), ("videos", self.query)])
        self.assertEqual([p.video_id for p in found.picks], ["c"])

    def test_searches_are_cached_so_running_again_asks_youtube_nothing(self):
        first = FakeYouTube({("songs", self.query): [song("b", "Night Drive", ["Kilo"], seconds=262)]})
        before = self.searcher(first).find(self.track)
        again = FakeYouTube()
        after = self.searcher(again).find(self.track)
        self.assertEqual(again.searches, [])
        self.assertEqual(after.picks, before.picks)

    def test_a_failed_search_says_how_to_carry_on_and_keeps_the_searches_made_before_it(self):
        class Failing(FakeYouTube):
            def search(self, query, filter=None, limit=20):
                if filter == "videos":
                    raise ConnectionError("offline")
                return super().search(query, filter, limit)

        with self.assertRaisesRegex(YouTubeError, "run the same command again"):
            self.searcher(Failing()).find(self.track)
        self.assertIn(f"songs:{self.query}", JsonLinesCache(self.cache_path))


def _pick(video_id, status=MATCHED, kind="song"):
    return Pick(video_id, "Night Drive", ["Kilo"], "", 202.0, kind, status, 0.9)


class ReportsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.exact = library_track(name="Night Drive", artist="Kilo", plays=40)
        self.close = library_track(name="Harbour | Live", artist="Kilo", plays=30)
        self.none = library_track(name="Zażółć", artist="Gęślą Jaźń", plays=20)
        self.gone = library_track(name="Undertow", plays=25)
        self.selection = Selection([Wanted(self.exact, NOT_ON_SPOTIFY), Wanted(self.close, NOT_ON_SPOTIFY),
                                    Wanted(self.none, DECIDED_NOT_ON_SPOTIFY)], [self.gone])
        self.found = {self.exact.id: Found(self.exact.id, [_pick("e")]),
                      self.close.id: Found(self.close.id, [_pick("c1", UNCERTAIN), _pick("c2", ALTERNATIVE, "video")]),
                      self.none.id: Found(self.none.id)}
        self.paths = write_youtube_reports(self.selection, self.found, self.dir, 10)

    def test_the_csv_has_a_row_per_pick_and_one_for_each_track_with_nothing_found(self):
        with self.paths[0].open(encoding="utf-8-sig", newline="") as rows:
            rows = list(csv.DictReader(rows))
        self.assertEqual([(r["title"], r["youtube"], r["pick"], r["url"][-2:]) for r in rows],
                         [("Night Drive", EXACT, "1", "=e"), ("Harbour | Live", SIMILAR, "1", "c1"),
                          ("Harbour | Live", SIMILAR, "2", "c2"), ("Zażółć", NOTHING, "", "")])
        self.assertEqual(rows[2]["match"], "another version")

    def test_the_summary_counts_the_tracks_lists_nothing_found_and_left_out_separately(self):
        text = self.paths[1].read_text(encoding="utf-8")
        self.assertIn("3 music tracks played 10 times or more: 1 found as the same recording, "
                      "1 with up to 2 close results, 1 with nothing close enough. 3 videos", text)
        nothing = text.split("## Nothing found on YouTube Music", 1)[1]
        self.assertIn("Gęślą Jaźń - Zażółć", nothing.split("##", 1)[0])
        self.assertIn("Undertow", text.split("## Left out in review", 1)[1])
        self.assertIn("Harbour \\| Live", text)
        self.assertIn("(https://music.youtube.com/watch?v=c2)", text)


class SyncPlaylistTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_path = Path(tmp.name, "youtube-state.json")
        self.fake = FakeYouTube()
        self.steps = []

    def sync(self, video_ids):
        return sync_playlist(video_ids, self.fake, PlaylistState(self.state_path), "Favourites",
                             lambda *step: self.steps.append(step))

    def test_the_first_sync_creates_one_private_playlist_and_adds_the_videos_in_order(self):
        outcome = self.sync(["a", "b", "c"])
        self.assertEqual(self.fake.created, [("Favourites", "PRIVATE")])
        self.assertEqual(self.fake.playlists["PL1"], ["a", "b", "c"])
        self.assertEqual((outcome.created, outcome.added, outcome.already), (True, ["a", "b", "c"], 0))
        self.assertEqual(json.loads(self.state_path.read_text())["playlist_id"], "PL1")

    def test_running_it_again_adds_only_what_is_missing_and_never_a_video_twice(self):
        self.sync(["a", "b"])
        outcome = self.sync(["a", "b", "c"])
        self.assertEqual(len(self.fake.created), 1)
        self.assertEqual(self.fake.playlists["PL1"], ["a", "b", "c"])
        self.assertEqual((outcome.created, outcome.added, outcome.already), (False, ["c"], 2))

    def test_a_video_removed_from_the_playlist_by_hand_is_not_added_back(self):
        self.sync(["a", "b"])
        self.fake.playlists["PL1"].remove("a")
        self.assertEqual(self.sync(["a", "b"]).added, [])
        self.assertEqual(self.fake.playlists["PL1"], ["b"])

    def test_one_unavailable_video_does_not_keep_the_rest_of_its_batch_out(self):
        self.fake.unavailable = {"b"}
        outcome = self.sync(["a", "b", "c"])
        self.assertEqual((outcome.added, outcome.failed), (["a", "c"], ["b"]))
        self.assertEqual(self.fake.playlists["PL1"], ["a", "c"])
        self.fake.unavailable = set()
        self.assertEqual(self.sync(["a", "b", "c"]).added, ["b"])

    def test_large_lists_go_in_batches_and_each_batch_is_remembered_as_it_lands(self):
        ids = [f"v{n}" for n in range(120)]
        self.sync(ids)
        self.assertEqual([len(e) for e in self.fake.edits], [50, 50, 20])
        self.assertEqual(self.steps, [("added to the playlist", 50, 120), ("added to the playlist", 100, 120),
                                      ("added to the playlist", 120, 120)])
        self.assertEqual(len(PlaylistState(self.state_path).added), 120)

    def test_a_playlist_youtube_did_not_create_stops_the_sync_and_nothing_is_remembered(self):
        self.fake.refuse_create = True
        with self.assertRaisesRegex(YouTubeError, "make youtube-auth"):
            self.sync(["a"])
        self.assertFalse(self.state_path.exists())

    def test_most_played_tracks_go_first_and_a_video_two_tracks_share_goes_in_once(self):
        one, two = library_track(name="A", plays=50), library_track(name="B", plays=20)
        selection = Selection([Wanted(one, NOT_ON_SPOTIFY), Wanted(two, NOT_ON_SPOTIFY)], [])
        found = {one.id: Found(one.id, [_pick("x", UNCERTAIN), _pick("y", UNCERTAIN)]),
                 two.id: Found(two.id, [_pick("y")])}
        self.assertEqual(playlist_order(selection, found), ["x", "y"])


class YouTubeCommandsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name, "data")
        self.reports = Path(tmp.name, "reports")
        self.data.mkdir()
        self.lost = track(name="Night Drive", artist="Kilo", plays=40)
        self.close = track(name="Harbour", artist="Kilo", plays=20)
        self.done = track(name="Paper Lanterns", plays=50)
        raw = [self.lost, self.close, self.done]
        Path(self.data, "library.json").write_text(json.dumps(snapshot(raw)), encoding="utf-8")
        save_matches(self.data / "matches.json", {
            self.lost["id"]: MatchResult(self.lost["id"], NOT_FOUND),
            self.close["id"]: MatchResult(self.close["id"], NOT_FOUND),
            self.done["id"]: MatchResult(self.done["id"], MATCHED, candidate("Paper Lanterns", ["Q"])),
        })
        save_overrides(self.data / "overrides.csv", {})
        library = parse_library(snapshot(raw))
        lost_query = youtube_queries(library.tracks[self.lost["id"]])[0]
        close_query = youtube_queries(library.tracks[self.close["id"]])[0]
        self.youtube = FakeYouTube({
            ("songs", lost_query): [song("exact1", "Night Drive", ["Kilo"])],
            ("songs", close_query): [song("close1", "Harbour", ["Kilo"], seconds=262),
                                     song("close2", "Harbour (Live)", ["Kilo"])],
        })

    def run_cli(self, *argv, expect=0):
        out, err = io.StringIO(), io.StringIO()
        env = {"DATA_DIR": str(self.data), "REPORTS_DIR": str(self.reports), "FAVOURITE_PLAYS": "10",
               "YOUTUBE_PLAYLIST": "Favourites", "YOUTUBE_AUTH_FILE": ""}
        with mock.patch.dict("os.environ", env), mock.patch("music2spotify.youtube.MIN_INTERVAL", 0), \
                mock.patch("music2spotify.youtube.anonymous_client", lambda: self.youtube), \
                redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        self.assertEqual(code, expect, err.getvalue())
        return out.getvalue() + err.getvalue()

    def test_youtube_match_says_how_long_it_takes_then_writes_the_matches_and_the_reports(self):
        out = self.run_cli("youtube-match")
        self.assertIn("2 favourites (played 10 times or more) not on Spotify in the exact version; "
                      "2 not searched before", out)
        self.assertIn("2 tracks: 1 found as the same recording, 1 with close results, 0 with nothing "
                      "close enough; 3 videos for the playlist.", out)
        saved = json.loads((self.data / "youtube-matches.json").read_text(encoding="utf-8"))["tracks"]
        self.assertEqual([p["video_id"] for p in saved[self.close["id"]]["picks"]], ["close1", "close2"])
        self.assertTrue((self.reports / "youtube.csv").exists())
        self.assertTrue((self.reports / "youtube.md").exists())

    def test_youtube_sync_without_a_login_says_to_run_youtube_auth_and_changes_nothing(self):
        self.run_cli("youtube-match")
        out = self.run_cli("youtube-sync", expect=2)
        self.assertIn("make youtube-auth", out)
        self.assertFalse((self.data / "youtube-state.json").exists())

    def test_youtube_sync_fills_the_playlist_once_most_played_first(self):
        self.run_cli("youtube-match")
        with mock.patch("music2spotify.cli.signed_in_client", lambda path: self.youtube):
            first = self.run_cli("youtube-sync")
            second = self.run_cli("youtube-sync")
        self.assertEqual(self.youtube.playlists["PL1"], ["exact1", "close1", "close2"])
        self.assertIn("Playlist created. 3 videos added, 0 were there already.", first)
        self.assertIn("0 videos added, 3 were there already.", second)
        self.assertIn("https://music.youtube.com/playlist?list=PL1", second)


if __name__ == "__main__":
    unittest.main()
