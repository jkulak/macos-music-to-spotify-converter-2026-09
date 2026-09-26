import unittest
from dataclasses import replace

from music2spotify.matching import (
    ALTERNATIVE,
    MATCHED,
    NOT_FOUND,
    UNCERTAIN,
    best_assessment,
    readings,
    search_queries,
)
from tests.builders import candidate, library_track


def status(track, *candidates):
    return best_assessment(track, list(candidates)).status


class MatchingTest(unittest.TestCase):
    def test_the_same_song_with_a_remastered_title_is_a_match(self):
        track = library_track(name="Paper Lanterns", artist="The Quiet Engines", duration=201)
        self.assertEqual(status(track, candidate("Paper Lanterns - Remastered 2011",
                                                 ["The Quiet Engines"], 203)), MATCHED)

    def test_a_featured_artist_credited_separately_on_spotify_is_still_a_match(self):
        track = library_track(name="Harbour (feat. Mira Vale)", artist="Kilo", duration=180)
        self.assertEqual(status(track, candidate("Harbour", ["Kilo", "Mira Vale"], 181)), MATCHED)

    def test_a_duo_tagged_with_an_ampersand_matches_both_credited_artists(self):
        track = library_track(name="Cockpit", artist="Kilo & Moth", duration=125)
        self.assertEqual(status(track, candidate("Cockpit", ["Kilo", "Moth"], 125)), MATCHED)

    def test_when_only_the_original_exists_a_remix_is_offered_as_an_alternative(self):
        track = library_track(name="Night Drive (Velvet Kid Remix)", artist="Kilo", duration=245)
        best = best_assessment(track, [candidate("Night Drive", ["Kilo"], 220)])
        self.assertEqual(best.status, ALTERNATIVE)
        self.assertIn("Music has velvet kid remix, Spotify has the original", best.notes)

    def test_the_right_remix_is_a_match(self):
        track = library_track(name="Night Drive (Velvet Kid Remix)", artist="Kilo", duration=245)
        self.assertEqual(status(track, candidate("Night Drive", ["Kilo"], 220),
                                candidate("Night Drive - Velvet Kid Remix", ["Kilo", "Velvet Kid"], 246)),
                         MATCHED)

    def test_a_much_longer_or_shorter_recording_is_only_uncertain(self):
        track = library_track(name="Paper Lanterns", artist="The Quiet Engines", duration=201)
        best = best_assessment(track, [candidate("Paper Lanterns", ["The Quiet Engines"], 260)])
        self.assertEqual(best.status, UNCERTAIN)
        self.assertIn("length differs by 59s", best.notes)

    def test_a_radio_edit_of_the_same_song_matches_despite_the_length(self):
        track = library_track(name="Lemon Tree (Original Mix)", artist="Kilo", duration=390)
        self.assertEqual(status(track, candidate("Lemon Tree - Radio Edit", ["Kilo"], 200)), MATCHED)

    def test_a_different_artist_with_the_same_title_is_not_a_match(self):
        track = library_track(name="Paper Lanterns", artist="The Quiet Engines", duration=201)
        self.assertEqual(status(track, candidate("Paper Lanterns", ["Harpo Lindqvist"], 201)), NOT_FOUND)

    def test_artist_and_title_typed_into_the_title_field_are_read_both_ways(self):
        track = library_track(name="Kilo - Night Drive", artist="Kilo", duration=220)
        self.assertEqual(status(track, candidate("Night Drive", ["Kilo"], 220)), MATCHED)
        swapped = library_track(name="Night Drive - Kilo", artist="Various Artists", duration=220)
        self.assertEqual(status(swapped, candidate("Night Drive", ["Kilo"], 220)), MATCHED)

    def test_polish_titles_without_diacritics_on_one_side_still_match(self):
        track = library_track(name="Zejdz w dol", artist="Kilo", duration=194)
        self.assertEqual(status(track, candidate("Zejdź w dół", ["Kilo"], 194)), MATCHED)

    def test_a_track_without_an_artist_is_at_most_uncertain(self):
        track = library_track(name="Paper Lanterns", artist="", albumArtist="", duration=201)
        self.assertEqual(status(track, candidate("Paper Lanterns", ["The Quiet Engines"], 201)), UNCERTAIN)

    def test_a_track_that_cannot_be_played_in_the_market_is_not_a_match(self):
        track = library_track(name="Paper Lanterns", artist="The Quiet Engines", duration=201)
        greyed = replace(candidate("Paper Lanterns", ["The Quiet Engines"], 201), playable=False)
        best = best_assessment(track, [greyed])
        self.assertEqual(best.status, NOT_FOUND)
        self.assertIn("on Spotify but not playable in this market (would be matched)", best.notes)

    def test_the_original_album_is_preferred_over_a_compilation(self):
        track = library_track(name="Paper Lanterns", artist="The Quiet Engines", album="Harbour Lights")
        original = candidate("Paper Lanterns", ["The Quiet Engines"], album="Harbour Lights")
        compilation = candidate("Paper Lanterns", ["The Quiet Engines"], album="Now 44",
                                album_type="compilation")
        self.assertEqual(best_assessment(track, [compilation, original]).candidate, original)


class QueriesTest(unittest.TestCase):
    def test_queries_start_with_title_and_main_artist_filters(self):
        track = library_track(name="Harbour (feat. Mira Vale)", artist="Kilo & Moth")
        self.assertEqual(search_queries(track)[:2], ["track:harbour artist:kilo", "kilo harbour"])

    def test_a_remix_also_searches_with_the_remixer_name(self):
        track = library_track(name="Night Drive (Velvet Kid Remix)", artist="Kilo")
        self.assertIn("night drive velvet kid remix kilo", search_queries(track))

    def test_a_track_without_an_artist_is_searched_by_title(self):
        track = library_track(name="Paper Lanterns", artist="", albumArtist="")
        self.assertEqual(search_queries(track), ["track:paper lanterns"])

    def test_a_version_suffix_after_a_dash_is_not_read_as_an_artist(self):
        track = library_track(name="Lemon Tree - Original Mix", artist="Kilo")
        self.assertEqual([r.label for r in readings(track)], ["as tagged"])


if __name__ == "__main__":
    unittest.main()


class ReadingsTest(unittest.TestCase):
    def test_a_title_prefixed_with_another_artist_is_also_read_as_artist_and_title(self):
        track = library_track(name="Kilo - Night Drive", artist="Uploader Channel")
        self.assertIn("artist - title in the title field", [r.label for r in readings(track)])

    def test_a_title_after_the_dash_may_carry_its_own_remix(self):
        track = library_track(name="Kilo - Night Drive (Velvet Kid Remix)", artist="Kilo")
        artist_first = [r for r in readings(track) if r.label == "artist - title in the title field"]
        self.assertEqual(artist_first[0].title.core, "night drive")
