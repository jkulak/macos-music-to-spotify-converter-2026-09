import unittest

from music2spotify.normalize import (
    Version,
    fold,
    parse_title,
    split_artists,
    versions_compatible,
)


class FoldingTest(unittest.TestCase):
    def test_folding_removes_accents_including_polish_letters(self):
        self.assertEqual(fold("Zejdź w dół, Łódź"), "zejdz w dol lodz")

    def test_folding_spells_the_ampersand_and_drops_punctuation(self):
        self.assertEqual(fold("Caspa & Rusko"), "caspa and rusko")
        self.assertEqual(fold("Don't Stop (Me Now)!"), "dont stop me now")

    def test_folding_keeps_stylised_names_comparable(self):
        self.assertEqual(fold("A$AP Rocky"), "asap rocky")


class TitleParsingTest(unittest.TestCase):
    def test_a_remix_in_brackets_is_a_distinct_version_credited_to_the_remixer(self):
        title = parse_title("Night Drive (Velvet Kid Trap Remix)")
        self.assertEqual(title.core, "night drive")
        self.assertEqual(title.versions, {Version("remix", "velvet kid")})

    def test_a_spotify_style_dash_suffix_is_read_like_a_bracket(self):
        self.assertEqual(parse_title("Night Drive - Velvet Kid Remix").versions,
                         {Version("remix", "velvet kid")})
        self.assertTrue(parse_title("Paper Lanterns - Remastered 2011").cut)

    def test_featured_artists_are_taken_out_of_the_title(self):
        for title in ("Harbour (feat. Mira Vale)", "Harbour [ft. Mira Vale]", "Harbour Feat. Mira Vale"):
            parsed = parse_title(title)
            self.assertEqual((parsed.core, parsed.featured), ("harbour", ("mira vale",)), title)

    def test_remaster_radio_edit_and_original_mix_are_cuts_of_the_same_version(self):
        for title in ("Song (Original Mix)", "Song - Radio Edit", "Song (Remastered)", "Song [Explicit]"):
            parsed = parse_title(title)
            self.assertEqual((parsed.core, parsed.versions, parsed.cut), ("song", frozenset(), True), title)

    def test_camelot_keys_and_bpm_annotations_are_ignored(self):
        parsed = parse_title("Hold On (Acapella) - 12b - 78.17")
        self.assertEqual(parsed.core, "hold on")
        self.assertEqual(parsed.versions, {Version("acapella")})

    def test_brackets_that_name_nothing_stay_part_of_the_title(self):
        self.assertEqual(parse_title("Crazy (Love)").core, "crazy love")

    def test_remixer_names_go_into_the_search_words(self):
        self.assertEqual(parse_title("Night Drive (Velvet Kid Remix)").search_words,
                         "night drive velvet kid remix")

    def test_live_acoustic_and_instrumental_takes_are_distinct_versions(self):
        self.assertEqual(parse_title("Song - Live From Tokyo").versions, {Version("live")})
        self.assertEqual(parse_title("Song (Acoustic)").versions, {Version("acoustic")})
        self.assertEqual(parse_title("Song [Instrumental]").versions, {Version("instrumental")})


class VersionsTest(unittest.TestCase):
    def test_the_same_remix_spelled_differently_is_compatible(self):
        ours = parse_title("Night Drive (Velvet Kid Trap Remix)").versions
        theirs = parse_title("Night Drive - Velvet Kid Remix").versions
        self.assertTrue(versions_compatible(ours, theirs))

    def test_a_remix_and_the_original_are_different_versions(self):
        self.assertFalse(versions_compatible(parse_title("Night Drive (Velvet Kid Remix)").versions,
                                             parse_title("Night Drive").versions))

    def test_remixes_by_different_people_are_different_versions(self):
        self.assertFalse(versions_compatible(parse_title("Night Drive (Velvet Kid Remix)").versions,
                                             parse_title("Night Drive (Orbital Moth Remix)").versions))


class ArtistSplittingTest(unittest.TestCase):
    def test_artists_are_split_on_commas_ampersands_x_and_feat(self):
        self.assertEqual(split_artists("B-Side, Nova Crew & Mira Vale"), ["b side", "nova crew", "mira vale"])
        self.assertEqual(split_artists("Kilo x Moth"), ["kilo", "moth"])
        self.assertEqual(split_artists("Kilo feat. Moth"), ["kilo", "moth"])


if __name__ == "__main__":
    unittest.main()


class VersionSpellingTest(unittest.TestCase):
    def test_a_hyphenated_re_work_is_a_distinct_version(self):
        self.assertEqual(parse_title("Harbour (Velvet Kid Re-Work)").versions, {Version("re work", "velvet kid")})

    def test_a_remix_named_at_the_end_without_brackets_is_a_distinct_version(self):
        parsed = parse_title("Cruel Lanterns Velvet Remix")
        self.assertEqual((parsed.core, parsed.versions), ("cruel lanterns velvet", {Version("remix")}))

    def test_a_title_that_is_only_the_word_remix_stays_a_title(self):
        self.assertEqual(parse_title("Remix").core, "remix")
