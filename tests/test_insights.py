import unittest

from music2spotify.insights import build_insights, canonical_genres, is_rock
from music2spotify.library import parse_library
from music2spotify.matching import MATCHED, NOT_FOUND, SKIPPED
from music2spotify.pipeline import MatchResult
from tests.builders import candidate, snapshot, track


def library(*raw_tracks, missing=()):
    lib = parse_library(snapshot(list(raw_tracks)))
    results = {}
    for n, t in enumerate(lib.tracks.values()):
        if not t.is_music:
            results[t.id] = MatchResult(t.id, SKIPPED)
        elif t.name in missing:
            results[t.id] = MatchResult(t.id, NOT_FOUND)
        else:
            results[t.id] = MatchResult(t.id, MATCHED, candidate(t.name, [t.artist], id=f"{n:022d}"))
    return lib, results


def named(lib, results, playlist):
    by_uri = {results[i].uri: lib.tracks[i].name for i in lib.tracks if results[i].uri}
    return [by_uri[u] for u in playlist.uris]


def insight(playlists, name):
    [found] = [p for p in playlists if p.name == name]
    return found


class RankingsTest(unittest.TestCase):
    def test_top_100_is_filled_with_the_most_played_songs_that_are_on_spotify(self):
        lib, results = library(track(name="A", plays=9), track(name="B", plays=30),
                               track(name="C", plays=20), missing={"C"})
        top = insight(build_insights(lib, results), "Top 100")
        self.assertEqual(named(lib, results, top), ["B", "A"])
        self.assertEqual(top.description, "Most played in Music · 2 of 3 tracks on Spotify")
        self.assertEqual(top.folder_path, ["Insights", "Rankings"])

    def test_rock_is_any_genre_containing_rock_as_in_the_music_smart_playlists(self):
        for genre in ("Rock", "Punkrock", "Rockabilly", "Surf Rock", "Electro-Rock", "rock"):
            self.assertTrue(is_rock(parse_library(snapshot([track(genre=genre)])).tracks.popitem()[1]), genre)
        lib, results = library(track(name="Riff", genre="Punkrock", plays=5),
                               track(name="Beat", genre="Hip-Hop", plays=50))
        playlists = build_insights(lib, results)
        self.assertEqual(named(lib, results, insight(playlists, "Top 100 · Rock")), ["Riff"])
        self.assertEqual(named(lib, results, insight(playlists, "Top 100 · Not rock")), ["Beat"])

    def test_top_artists_take_the_five_most_played_songs_of_the_most_played_artists(self):
        songs = [track(name=f"K{i}", artist="Kilo", plays=10 + i) for i in range(7)]
        songs += [track(name="M1", artist="Moth", plays=50)]
        lib, results = library(*songs)
        picks = named(lib, results, insight(build_insights(lib, results), "Top 10 artists · 5 each"))
        self.assertEqual(picks, ["K6", "K5", "K4", "K3", "K2", "M1"])

    def test_non_music_never_appears_in_insights(self):
        lib, results = library(track(name="Memo", genre="Voice Memo", plays=999, rating=100))
        self.assertEqual(build_insights(lib, results), [])


class StableSetTest(unittest.TestCase):
    def test_an_insight_exists_as_soon_as_the_library_has_songs_for_it_even_before_matching(self):
        lib, results = library(track(name="Riff", genre="Punkrock", plays=5, rating=100),
                               missing={"Riff"})
        playlists = build_insights(lib, results)
        five = insight(playlists, "5 stars")
        self.assertEqual((five.uris, five.music_tracks), ([], 1))
        self.assertIn("Top 10 artists · 5 each", [p.name for p in playlists])

    def test_the_set_of_insights_does_not_change_as_more_tracks_are_matched(self):
        songs = [track(name=f"S{i}", genre="Grime", plays=i, bpm=140) for i in range(60)]
        before = build_insights(*library(*songs, missing={f"S{i}" for i in range(60)}))
        after = build_insights(*library(*songs))
        self.assertEqual([p.key for p in before], [p.key for p in after])


class GenresTest(unittest.TestCase):
    def test_spelling_variants_of_a_genre_are_merged_under_the_most_common_one(self):
        lib, _ = library(track(genre="Hip-Hop"), track(genre="Hip-Hop"), track(genre="Hip-hop"))
        self.assertEqual(canonical_genres(list(lib.tracks.values())), {"hip hop": "Hip-Hop"})

    def test_only_genres_with_at_least_fifty_tracks_get_a_top_25(self):
        songs = [track(name=f"H{i}", genre="Hip-hop" if i % 3 == 0 else "Hip-Hop", plays=i)
                 for i in range(50)]
        songs += [track(name=f"J{i}", genre="Jazz") for i in range(49)]
        lib, results = library(*songs)
        genre_lists = [p for p in build_insights(lib, results) if p.folder_path == ["Insights", "Genres"]]
        self.assertEqual([p.name for p in genre_lists], ["Top 25 · Hip-Hop"])
        self.assertEqual(len(genre_lists[0].uris), 25)
        self.assertEqual(named(lib, results, genre_lists[0])[0], "H49")


class RatingsTest(unittest.TestCase):
    def setUp(self):
        self.lib, self.results = library(
            track(name="Gem", rating=80, plays=40, lastPlayed="2012-01-01T00:00:00Z"),
            track(name="Star", rating=100, plays=10, lastPlayed="2026-01-01T00:00:00Z"),
            track(name="New", rating=80, ratingKind="computed", plays=2, lastPlayed="2025-01-01T00:00:00Z"),
            track(name="Meh", rating=60, plays=90),
        )
        self.playlists = build_insights(self.lib, self.results)

    def names(self, name):
        return named(self.lib, self.results, insight(self.playlists, name))

    def test_five_stars_holds_only_five_star_songs(self):
        self.assertEqual(self.names("5 stars"), ["Star"])

    def test_four_stars_and_up_puts_better_ratings_first_and_counts_album_ratings(self):
        self.assertEqual(self.names("4 stars and up"), ["Star", "Gem", "New"])

    def test_forgotten_gems_are_loved_and_much_played_but_not_since_2016(self):
        self.assertEqual(self.names("Forgotten gems"), ["Gem"])

    def test_undiscovered_are_well_rated_but_hardly_played(self):
        self.assertEqual(self.names("Undiscovered"), ["New"])


class TimelineAndTempoTest(unittest.TestCase):
    def test_last_played_years_need_fifty_songs_and_keep_the_top_100(self):
        songs = [track(name=f"S{i}", plays=i, lastPlayed="2010-06-01T00:00:00Z") for i in range(120)]
        songs += [track(name="Lone", lastPlayed="2019-06-01T00:00:00Z")]
        lib, results = library(*songs)
        years = [p for p in build_insights(lib, results) if p.name.startswith("Last played")]
        self.assertEqual([p.name for p in years], ["Last played in 2010"])
        self.assertEqual(len(years[0].uris), 100)
        self.assertEqual(named(lib, results, years[0])[0], "S119")

    def test_release_decades_ignore_unknown_and_impossible_years(self):
        songs = [track(name=f"N{i}", year=1990 + i % 10) for i in range(30)]
        songs += [track(name="Typo", year=2930), track(name="Unknown", year=0)]
        lib, results = library(*songs)
        decades = [p.name for p in build_insights(lib, results) if p.name.startswith("Released")]
        self.assertEqual(decades, ["Released in the 1990s"])

    def test_bpm_playlists_run_from_slowest_to_fastest(self):
        lib, results = library(track(name="Fast", bpm=176), track(name="Slow", bpm=170, plays=1),
                               track(name="Off", bpm=150))
        dnb = insight(build_insights(lib, results), "170–176 BPM")
        self.assertEqual(named(lib, results, dnb), ["Slow", "Fast"])
        self.assertEqual(dnb.folder_path, ["Insights", "BPM"])


if __name__ == "__main__":
    unittest.main()
