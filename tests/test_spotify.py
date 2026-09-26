import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from music2spotify.spotify import RateLimited, SpotifyClient, SpotifyError, TokenStore
from tests.fake_spotify import FakeSpotify


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class SpotifyClientTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSpotify()
        self.base = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wall = Clock()
        self.tokens = TokenStore(Path(self.tmp.name, "token.json"), "client-id", self.base, self.wall)
        self.tokens.save({"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600})
        self.slept = []
        self.client = SpotifyClient(self.tokens, f"{self.base}/v1", min_interval=0,
                                    max_retry_wait=60, sleep=self.slept.append)

    def test_search_asks_for_ten_tracks_in_the_users_market(self):
        self.fake.add_track("Paper Lanterns", ["The Quiet Engines"])
        items = self.client.search_tracks("track:paper lanterns artist:the quiet engines")
        self.assertEqual([i["name"] for i in items], ["Paper Lanterns"])
        query = self.fake.calls("GET", "/v1/search")[0].query
        self.assertEqual((query["limit"], query["market"], query["type"]), ("10", "from_token", "track"))

    def test_an_expired_token_is_refreshed_before_the_request(self):
        self.wall.now += 3600
        self.client.me()
        self.assertEqual(self.fake.requests[0].path, "/api/token")
        self.assertEqual(self.fake.requests[0].body["grant_type"], "refresh_token")
        self.assertEqual(json.loads(self.tokens.path.read_text())["access_token"], self.fake.access_token)

    def test_a_rejected_token_is_refreshed_once_and_the_request_retried(self):
        self.fake.access_token = "rotated-elsewhere"
        self.assertEqual(self.client.me()["id"], "tester")
        self.assertEqual([r.path for r in self.fake.requests], ["/v1/me", "/api/token", "/v1/me"])

    def test_a_refresh_keeps_the_old_refresh_token_when_spotify_sends_none(self):
        self.tokens.save({"access_token": "access-9", "expires_in": 3600})
        self.assertEqual(json.loads(self.tokens.path.read_text())["refresh_token"], "refresh-1")

    def test_rate_limit_waits_for_retry_after_then_retries(self):
        self.fake.fail_next(429, {"Retry-After": "7"})
        self.assertEqual(self.client.me()["id"], "tester")
        self.assertEqual(self.slept, [8])

    def test_a_retry_after_longer_than_the_limit_stops_the_run(self):
        self.fake.fail_next(429, {"Retry-After": "7200"})
        with self.assertRaises(RateLimited) as raised:
            self.client.me()
        self.assertEqual(raised.exception.retry_after, 7200)
        self.assertIn("cached", str(raised.exception))

    def test_server_errors_are_retried_with_backoff(self):
        self.fake.fail_next(502)
        self.fake.fail_next(503)
        self.assertEqual(self.client.me()["id"], "tester")
        self.assertEqual(self.slept, [1, 2])

    def test_a_client_error_is_raised_with_spotifys_message(self):
        with self.assertRaisesRegex(SpotifyError, "404"):
            self.client.playlist("missing")

    def test_requests_are_paced(self):
        mono = Clock(0.0)
        paced = SpotifyClient(self.tokens, f"{self.base}/v1", min_interval=0.5,
                              sleep=lambda s: setattr(mono, "now", mono.now + s) or self.slept.append(s),
                              clock=mono)
        paced.me()
        mono.now += 0.2
        paced.me()
        self.assertEqual(self.slept, [0.3])

    def test_playlist_items_are_replaced_in_batches_of_one_hundred(self):
        playlist = self.client.create_playlist("Added / 2013 / Trap", "Music: Added / 2013")
        uris = [f"spotify:track:{i:022d}" for i in range(250)]
        self.client.set_playlist_items(playlist["id"], uris)
        self.assertEqual(self.fake.playlists[playlist["id"]]["uris"], uris)
        calls = [(r.method, len(r.body["uris"])) for r in self.fake.requests if r.path.endswith("/items")]
        self.assertEqual(calls, [("PUT", 100), ("POST", 100), ("POST", 50)])
        self.assertEqual(self.client.playlist_uris(playlist["id"]), uris)

    def test_new_playlists_are_private(self):
        playlist = self.client.create_playlist("Mix", "")
        self.assertFalse(self.fake.playlists[playlist["id"]]["public"])

    def test_the_token_file_is_readable_only_by_the_owner(self):
        mode = stat.S_IMODE(os.stat(self.tokens.path).st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
