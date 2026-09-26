import tempfile
import unittest
from pathlib import Path

from music2spotify.library import parse_library
from music2spotify.matching import MATCHED, NOT_FOUND, SKIPPED
from music2spotify.pipeline import MatchResult
from music2spotify.plan import build_plan, folder_count, render_plan
from music2spotify.spotify import SpotifyClient, TokenStore
from music2spotify.sync import (
    CREATED,
    DESCRIBED,
    RECREATED,
    RENAMED,
    UNCHANGED,
    UPDATED,
    SyncState,
    sync,
)
from tests.builders import candidate, playlist, snapshot, track
from tests.fake_spotify import FakeSpotify


def library_with_results():
    hit = track(name="Paper Lanterns", plays=5)
    top = track(name="Night Drive", plays=90)
    twin = track(name="Night Drive (duplicate file)", plays=1)
    miss = track(name="Unreleased", plays=50)
    memo = track(name="Memo", genre="Voice Memo", plays=0)
    added = playlist("Added", kind="folder", id="F1")
    year = playlist("2013", kind="folder", parent="F1", id="F2")
    mix = playlist("2013-04 Mix", parent="F2", tracks=[hit, twin, top, miss, memo], id="P1")
    smart = playlist("Never played", kind="smart", tracks=[memo], id="P2")
    library = parse_library(snapshot([hit, top, twin, miss, memo], [added, year, mix, smart]))
    night = candidate("Night Drive", ["Kilo"], id="N" * 22)
    results = {
        hit["id"]: MatchResult(hit["id"], MATCHED, candidate("Paper Lanterns", ["Q"], id="P" * 22)),
        top["id"]: MatchResult(top["id"], MATCHED, night),
        twin["id"]: MatchResult(twin["id"], MATCHED, night),
        miss["id"]: MatchResult(miss["id"], NOT_FOUND),
        memo["id"]: MatchResult(memo["id"], SKIPPED, notes=["voice memo"]),
    }
    return library, results


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.library, self.results = library_with_results()
        self.plan = build_plan(self.library, self.results, "2026-09-26")

    def test_the_plan_starts_with_all_songs_then_mirrors_music_in_sidebar_order(self):
        self.assertEqual([p.display_name("path") for p in self.plan],
                         ["All songs", "Added / 2013 / 2013-04 Mix", "Never played"])
        self.assertEqual([p.display_name("plain") for p in self.plan],
                         ["All songs", "2013-04 Mix", "Never played"])

    def test_all_songs_holds_every_matched_track_once_most_played_first(self):
        self.assertEqual(self.plan[0].uris, ["spotify:track:" + "N" * 22, "spotify:track:" + "P" * 22])

    def test_playlists_keep_music_order_and_merge_tracks_that_matched_the_same_song(self):
        mix = self.plan[1]
        self.assertEqual(mix.uris, ["spotify:track:" + "P" * 22, "spotify:track:" + "N" * 22])
        self.assertEqual((mix.music_tracks, mix.missing), (4, 2))
        self.assertEqual(mix.description,
                         "Music: Added / 2013 / 2013-04 Mix · 2 of 4 tracks on Spotify · 1 duplicate merged")

    def test_a_smart_playlist_says_when_its_contents_were_taken(self):
        self.assertIn("smart playlist as of 2026-09-26", self.plan[2].description)

    def test_an_empty_playlist_is_still_planned_so_the_structure_is_complete(self):
        self.assertEqual((self.plan[2].uris, self.plan[2].music_tracks), ([], 0))

    def test_the_checklist_lists_folders_before_their_playlists(self):
        text = render_plan(self.plan, "path", "mp3-collection")
        self.assertEqual(folder_count(self.plan), 2)
        self.assertIn("- **mp3-collection/**\n  - [ ] `All songs` — 2 of 4\n  - [ ] **Added/**\n"
                      "    - [ ] **2013/**\n      - [ ] `Added / 2013 / 2013-04 Mix` — 2 of 4", text)
        self.assertIn("make sync PLAYLIST_NAMES=plain", text)


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSpotify()
        base = self.fake.start()
        self.addCleanup(self.fake.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tokens = TokenStore(Path(tmp.name, "token.json"), "cid", base)
        tokens.save({"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600})
        self.client = SpotifyClient(tokens, f"{base}/v1", min_interval=0)
        self.state_path = Path(tmp.name, "state.json")
        library, results = library_with_results()
        self.plan = build_plan(library, results, "2026-09-26")

    def run_sync(self, style="path"):
        return sync(self.plan, self.client, SyncState(self.state_path), style)

    def test_the_first_sync_creates_private_playlists_with_their_tracks(self):
        actions = self.run_sync()
        self.assertEqual(set(actions.values()), {CREATED})
        by_name = {p["name"]: p for p in self.fake.playlists.values()}
        self.assertEqual(set(by_name), {"All songs", "Added / 2013 / 2013-04 Mix", "Never played"})
        self.assertEqual(by_name["Added / 2013 / 2013-04 Mix"]["uris"], self.plan[1].uris)
        self.assertFalse(any(p["public"] for p in self.fake.playlists.values()))

    def test_a_second_sync_creates_nothing_and_changes_nothing(self):
        self.run_sync()
        writes = len([r for r in self.fake.requests if r.method in ("POST", "PUT")])
        actions = self.run_sync()
        self.assertEqual(set(actions.values()), {UNCHANGED})
        self.assertEqual(len(self.fake.playlists), 3)
        self.assertEqual(len([r for r in self.fake.requests if r.method in ("POST", "PUT")]), writes)

    def test_switching_to_plain_names_renames_the_same_playlists(self):
        self.run_sync()
        ids = set(self.fake.playlists)
        actions = self.run_sync("plain")
        self.assertEqual(set(self.fake.playlists), ids)
        self.assertEqual(actions["music:P1"], RENAMED)
        self.assertEqual(self.fake.playlists[SyncState(self.state_path).playlists["music:P1"]["id"]]["name"],
                         "2013-04 Mix")

    def test_changed_matches_replace_the_tracks_of_the_existing_playlist(self):
        self.run_sync()
        self.plan[1].uris.append("spotify:track:" + "Z" * 22)
        actions = self.run_sync()
        self.assertEqual(actions["music:P1"], f"{DESCRIBED}, {UPDATED}")
        self.assertEqual(self.fake.playlists[SyncState(self.state_path).playlists["music:P1"]["id"]]["uris"][-1],
                         "spotify:track:" + "Z" * 22)

    def test_a_playlist_removed_from_the_library_is_created_again(self):
        self.run_sync()
        gone = SyncState(self.state_path).playlists["music:P1"]["id"]
        del self.fake.playlists[gone]
        actions = self.run_sync()
        self.assertEqual(actions["music:P1"], RECREATED)
        self.assertEqual(len(self.fake.playlists), 3)

    def test_playlists_the_tool_did_not_create_are_left_alone(self):
        own = self.client.create_playlist("My own mix", "hands off")
        self.client.set_playlist_items(own["id"], ["spotify:track:" + "O" * 22])
        self.run_sync()
        self.assertEqual(self.fake.playlists[own["id"]]["name"], "My own mix")
        self.assertEqual(self.fake.playlists[own["id"]]["uris"], ["spotify:track:" + "O" * 22])


if __name__ == "__main__":
    unittest.main()
