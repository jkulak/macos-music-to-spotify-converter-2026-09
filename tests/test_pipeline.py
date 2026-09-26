import tempfile
import unittest
from pathlib import Path

from music2spotify.library import parse_library
from music2spotify.matching import MATCHED, NOT_FOUND, SKIPPED
from music2spotify.pipeline import (
    NONE,
    SKIP,
    JsonLinesCache,
    MatchResult,
    Override,
    Sources,
    load_matches,
    load_overrides,
    match_library,
    match_track,
    save_matches,
    save_overrides,
    spotify_track_id,
)
from music2spotify.spotify import RateLimited
from tests.builders import library_track, snapshot, track

SPOTIFY_ID = "4uLU6hMCjMI75M1A2tKUQC"
FIRST_QUERY = "track:paper lanterns artist:the quiet engines"


def api_track(name, artist="The Quiet Engines", seconds=201.5, id=SPOTIFY_ID, album=None):
    item = {"uri": f"spotify:track:{id}", "name": name, "artists": [{"name": artist}],
            "duration_ms": int(seconds * 1000)}
    if album is not False:
        item["album"] = album or {"id": "AL1", "name": "Harbour Lights", "album_type": "album"}
    return item


def api_album(name="Harbour Lights", artist="The Quiet Engines", id="AL1", total=3):
    return {"id": id, "name": name, "artists": [{"name": artist}], "album_type": "album",
            "release_date": "2009-05-01", "total_tracks": total}


class FakeClient:
    """Answers like SpotifyClient from canned data and counts the searches."""

    def __init__(self, tracks=None, albums=None, listings=None, search_quota=None):
        self.tracks, self.albums, self.listings = tracks or {}, albums or {}, listings or {}
        self.search_quota = search_quota
        self.calls = []

    def _search(self):
        if self.search_quota is not None:
            if self.search_quota == 0:
                raise RateLimited(86_000, "QUOTA_EXCEEDED")
            self.search_quota -= 1

    def search_tracks(self, query):
        self.calls.append(("search", query))
        self._search()
        return self.tracks.get(query, [])

    def search_albums(self, query):
        self.calls.append(("album search", query))
        self._search()
        return self.albums.get(query, [])

    def album_tracks(self, album_id):
        self.calls.append(("album tracks", album_id))
        return self.listings.get(album_id, [])

    def track(self, track_id):
        self.calls.append(("track", track_id))
        return api_track("Paper Lanterns (Live)", id=track_id)

    def searches(self):
        return [c for c in self.calls if c[0] in ("search", "album search")]


class MatchTrackTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache_path = Path(tmp.name, "cache.jsonl")
        self.song = library_track(name="Paper Lanterns", artist="The Quiet Engines")

    def sources(self, client):
        return Sources(JsonLinesCache(self.cache_path), client)

    def test_searches_are_cached_and_not_repeated_on_the_next_run(self):
        client = FakeClient(tracks={FIRST_QUERY: [api_track("Paper Lanterns")]})
        first = match_track(self.song, self.sources(client))
        again = match_track(self.song, self.sources(client))
        self.assertEqual((first.status, again.status), (MATCHED, MATCHED))
        self.assertEqual(client.searches(), [("search", FIRST_QUERY)])

    def test_matching_stops_searching_once_the_same_version_is_found(self):
        client = FakeClient(tracks={FIRST_QUERY: [api_track("Paper Lanterns")]})
        match_track(self.song, self.sources(client))
        self.assertEqual(len(client.searches()), 1)

    def test_a_track_that_is_not_found_tries_every_query(self):
        client = FakeClient()
        result = match_track(self.song, self.sources(client))
        self.assertEqual((result.status, result.notes), (NOT_FOUND, ["no search results"]))
        self.assertEqual(len(client.searches()), 2)

    def test_the_number_of_searches_can_be_capped(self):
        client = FakeClient()
        result = match_track(self.song, self.sources(client), max_queries=1)
        self.assertEqual(len(client.searches()), 1)
        self.assertEqual(result.unsearched, 0)

    def test_offline_matching_uses_only_cached_searches_and_counts_the_rest(self):
        result = match_track(self.song, self.sources(None))
        self.assertEqual((result.status, result.unsearched, result.notes), (NOT_FOUND, 2, []))

    def test_album_candidates_settle_a_track_without_searching(self):
        client = FakeClient()
        from music2spotify.matching import Candidate
        album_track = Candidate.from_api(api_track("Paper Lanterns"))
        result = match_track(self.song, self.sources(client), extra=[album_track])
        self.assertEqual(result.status, MATCHED)
        self.assertEqual(client.calls, [])

    def test_non_music_is_skipped_without_searching(self):
        client = FakeClient()
        memo = library_track(name="New Recording 12", genre="Voice Memo")
        result = match_track(memo, self.sources(client))
        self.assertEqual((result.status, result.notes, client.calls), (SKIPPED, ["voice memo"], []))

    def test_overrides_pin_a_track_skip_it_or_mark_it_missing(self):
        sources = self.sources(FakeClient())
        pinned = match_track(self.song, sources, Override(self.song.id, SPOTIFY_ID, "chosen by hand"))
        self.assertEqual((pinned.status, pinned.uri, pinned.source),
                         (MATCHED, f"spotify:track:{SPOTIFY_ID}", "override"))
        self.assertEqual(match_track(self.song, sources, Override(self.song.id, SKIP)).status, SKIPPED)
        missing = match_track(self.song, sources, Override(self.song.id, NONE))
        self.assertEqual((missing.status, missing.notes), (NOT_FOUND, ["confirmed not on Spotify"]))


class MatchLibraryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = JsonLinesCache(Path(tmp.name, "cache.jsonl"))

    def album_library(self, names=("Paper Lanterns", "Harbour", "Low Tide")):
        songs = [track(name=n, album="Harbour Lights (Deluxe Version)") for n in names]
        return parse_library(snapshot(songs))

    def test_tracks_sharing_an_album_are_matched_from_one_album_search(self):
        library = self.album_library()
        listing = [api_track(n, id=f"{i:022d}", album=False)
                   for i, n in enumerate(["Paper Lanterns", "Harbour", "Low Tide"])]
        client = FakeClient(albums={"album:harbour lights artist:the quiet engines": [api_album()]},
                            listings={"AL1": listing})
        results = match_library(library, Sources(self.cache, client), {})
        self.assertEqual({r.status for r in results.values()}, {MATCHED})
        self.assertEqual(client.searches(), [("album search", "album:harbour lights artist:the quiet engines")])
        self.assertEqual({r.candidate.album for r in results.values()}, {"Harbour Lights"})

    def test_tracks_the_album_does_not_settle_are_searched_one_by_one(self):
        library = self.album_library()
        listing = [api_track(n, id=f"{i:022d}", album=False)
                   for i, n in enumerate(["Paper Lanterns", "Harbour"])]
        client = FakeClient(albums={"album:harbour lights artist:the quiet engines": [api_album()]},
                            listings={"AL1": listing})
        results = match_library(library, Sources(self.cache, client), {})
        low_tide = next(r for r in results.values() if library.tracks[r.track_id].name == "Low Tide")
        self.assertEqual(low_tide.status, NOT_FOUND)
        self.assertIn(("search", "track:low tide artist:the quiet engines"), client.calls)

    def test_a_second_edition_is_opened_only_when_the_first_leaves_most_tracks_open(self):
        library = self.album_library()
        editions = [api_album(id="AL1"), api_album(id="AL2", total=20)]
        full = [api_track(n, id=f"{i:022d}", album=False)
                for i, n in enumerate(["Paper Lanterns", "Harbour", "Low Tide"])]
        client = FakeClient(albums={"album:harbour lights artist:the quiet engines": editions},
                            listings={"AL1": full[:1], "AL2": full})
        match_library(library, Sources(self.cache, client), {})
        self.assertEqual([c for c in client.calls if c[0] == "album tracks"],
                         [("album tracks", "AL1"), ("album tracks", "AL2")])

    def test_every_track_gets_its_first_search_before_any_gets_its_second(self):
        library = parse_library(snapshot([track(name="One", album=""), track(name="Two", album="")]))
        client = FakeClient()
        match_library(library, Sources(self.cache, client), {})
        self.assertEqual([q for _, q in client.searches()], [
            "track:one artist:the quiet engines", "track:two artist:the quiet engines",
            "the quiet engines one", "the quiet engines two"])

    def test_a_used_up_search_quota_ends_searching_but_the_run_still_finishes(self):
        library = parse_library(snapshot([track(name="One", album=""), track(name="Two", album=""),
                                          track(name="Memo", genre="Voice Memo")]))
        client = FakeClient(search_quota=1)
        sources = Sources(self.cache, client)
        results = match_library(library, sources, {})
        self.assertEqual(len(results), 3)
        self.assertEqual(sources.blocked.reason, "QUOTA_EXCEEDED")
        self.assertEqual(len(client.searches()), 2)  # the one allowed, the one refused
        self.assertEqual([r.unsearched for r in results.values()], [1, 2, 0])

    def test_a_blocked_search_quota_does_not_stop_album_track_lists(self):
        library = self.album_library()
        sources = Sources(self.cache, FakeClient(search_quota=0))
        sources.search_albums("anything")
        self.assertIsNotNone(sources.search_budget.blocked)
        self.assertIsNone(sources.catalogue_budget.blocked)
        from music2spotify.albums import AlbumHit
        self.assertEqual(sources.album_tracks(AlbumHit.from_api(api_album())), [])

    def test_progress_is_reported_per_stage(self):
        library = parse_library(snapshot([track(name="One", album="")]))
        seen = []
        match_library(library, Sources(self.cache, FakeClient()), {}, lambda *args: seen.append(args))
        self.assertEqual(seen, [("search 1", 1, 1), ("search 2", 1, 1)])


class OverridesFileTest(unittest.TestCase):
    def test_spotify_references_are_accepted_as_uri_link_or_bare_id(self):
        for text in (f"spotify:track:{SPOTIFY_ID}",
                     f"https://open.spotify.com/track/{SPOTIFY_ID}?si=abc123",
                     f"https://open.spotify.com/intl-pl/track/{SPOTIFY_ID}",
                     SPOTIFY_ID):
            self.assertEqual(spotify_track_id(text), SPOTIFY_ID, text)
        self.assertIsNone(spotify_track_id("https://open.spotify.com/album/" + SPOTIFY_ID))

    def test_overrides_survive_a_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "overrides.csv")
            saved = {"A": Override("A", SPOTIFY_ID, "by hand"), "B": Override("B", SKIP, "")}
            save_overrides(path, saved)
            self.assertEqual(load_overrides(path), saved)

    def test_an_override_that_is_not_a_track_is_refused_with_its_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "overrides.csv")
            path.write_text("track_id,spotify,note\nA,maybe,\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "overrides.csv:2"):
                load_overrides(path)


class SavedMatchesTest(unittest.TestCase):
    def test_match_results_survive_a_save_and_load(self):
        from tests.builders import candidate

        results = {"A": MatchResult("A", MATCHED, candidate("Song", ["Kilo"]), score=0.97,
                                    duration_delta=1.5, notes=["read as tagged"])}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "matches.json")
            save_matches(path, results)
            self.assertEqual(load_matches(path), results)


if __name__ == "__main__":
    unittest.main()


class AlbumFromMatchedSiblingTest(unittest.TestCase):
    def test_a_matched_track_names_its_album_so_the_rest_needs_no_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = JsonLinesCache(Path(tmp, "cache.jsonl"))
            songs = [track(name=n, album="Harbour Lights") for n in ("Paper Lanterns", "Harbour")]
            library = parse_library(snapshot(songs))
            listing = [api_track(n, id=f"{i:022d}", album=False)
                       for i, n in enumerate(["Paper Lanterns", "Harbour"])]
            client = FakeClient(tracks={FIRST_QUERY: [api_track("Paper Lanterns", id="0" * 22)]},
                                listings={"AL1": listing})
            match_track(library.tracks[songs[0]["id"]], Sources(cache, client))  # an earlier run
            client.calls.clear()
            results = match_library(library, Sources(cache, client), {})
        self.assertEqual({r.status for r in results.values()}, {MATCHED})
        self.assertEqual(client.searches(), [])
        self.assertEqual(client.calls, [("album tracks", "AL1")])
