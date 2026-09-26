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
    cached_lookup,
    cached_search,
    load_matches,
    load_overrides,
    match_library,
    match_track,
    save_matches,
    save_overrides,
    spotify_track_id,
)
from tests.builders import library_track, snapshot, track

SPOTIFY_ID = "4uLU6hMCjMI75M1A2tKUQC"


def api_track(name, artist, seconds=201.5, id=SPOTIFY_ID):
    return {"uri": f"spotify:track:{id}", "name": name, "artists": [{"name": artist}],
            "album": {"name": "Harbour Lights", "album_type": "album"},
            "duration_ms": int(seconds * 1000)}


class Recorder:
    def __init__(self, results):
        self.results = results
        self.queries = []

    def __call__(self, query):
        self.queries.append(query)
        return self.results.get(query, [])


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name, "cache.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_searches_are_cached_and_not_repeated_on_the_next_run(self):
        fetch = Recorder({"track:paper lanterns artist:the quiet engines":
                          [api_track("Paper Lanterns", "The Quiet Engines")]})
        song = library_track(name="Paper Lanterns", artist="The Quiet Engines")
        first = match_track(song, cached_search(JsonLinesCache(self.cache_path), fetch), lambda i: None)
        again = match_track(song, cached_search(JsonLinesCache(self.cache_path), fetch), lambda i: None)
        self.assertEqual((first.status, again.status), (MATCHED, MATCHED))
        self.assertEqual(len(fetch.queries), 1)

    def test_matching_stops_searching_once_the_same_version_is_found(self):
        fetch = Recorder({"track:paper lanterns artist:the quiet engines":
                          [api_track("Paper Lanterns", "The Quiet Engines")]})
        song = library_track(name="Paper Lanterns", artist="The Quiet Engines")
        match_track(song, cached_search(JsonLinesCache(self.cache_path), fetch), lambda i: None)
        self.assertEqual(fetch.queries, ["track:paper lanterns artist:the quiet engines"])

    def test_a_track_that_is_not_found_tries_every_query(self):
        fetch = Recorder({})
        song = library_track(name="Paper Lanterns", artist="The Quiet Engines")
        result = match_track(song, cached_search(JsonLinesCache(self.cache_path), fetch), lambda i: None)
        self.assertEqual(result.status, NOT_FOUND)
        self.assertEqual(result.notes, ["no search results"])
        self.assertEqual(len(fetch.queries), 2)

    def test_offline_matching_uses_only_cached_searches_and_counts_the_rest(self):
        song = library_track(name="Paper Lanterns", artist="The Quiet Engines")
        result = match_track(song, cached_search(JsonLinesCache(self.cache_path), None), lambda i: None)
        self.assertEqual((result.status, result.unsearched, result.notes), (NOT_FOUND, 2, []))

    def test_non_music_is_skipped_without_searching(self):
        fetch = Recorder({})
        memo = library_track(name="New Recording 12", genre="Voice Memo")
        result = match_track(memo, cached_search(JsonLinesCache(self.cache_path), fetch), lambda i: None)
        self.assertEqual((result.status, result.notes, fetch.queries), (SKIPPED, ["voice memo"], []))

    def test_overrides_pin_a_track_skip_it_or_mark_it_missing(self):
        song = library_track(name="Paper Lanterns", artist="The Quiet Engines")
        lookup = cached_lookup(JsonLinesCache(self.cache_path),
                               lambda i: api_track("Paper Lanterns (Live)", "The Quiet Engines", id=i))
        never = cached_search(JsonLinesCache(self.cache_path), None)
        pinned = match_track(song, never, lookup, Override(song.id, SPOTIFY_ID, "chosen by hand"))
        self.assertEqual((pinned.status, pinned.uri, pinned.source),
                         (MATCHED, f"spotify:track:{SPOTIFY_ID}", "override"))
        self.assertEqual(match_track(song, never, lookup, Override(song.id, SKIP)).status, SKIPPED)
        missing = match_track(song, never, lookup, Override(song.id, NONE))
        self.assertEqual((missing.status, missing.notes), (NOT_FOUND, ["confirmed not on Spotify"]))

    def test_matching_a_library_reports_progress_for_every_track(self):
        a, b = track(name="One"), track(name="Two", genre="Voice Memo")
        library = parse_library(snapshot([a, b]))
        seen = []
        results = match_library(library, cached_search(JsonLinesCache(self.cache_path), Recorder({})),
                                lambda i: None, {}, lambda done, total, counts: seen.append((done, total)))
        self.assertEqual(seen, [(1, 2), (2, 2)])
        self.assertEqual({r.status for r in results.values()}, {NOT_FOUND, SKIPPED})


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
