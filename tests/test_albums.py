import unittest

from music2spotify.albums import AlbumHit, album_core, album_groups, rank_albums
from music2spotify.library import parse_library
from tests.builders import snapshot, track


def tracks(*raw):
    return list(parse_library(snapshot(list(raw))).tracks.values())


def hit(name, artist="The Quiet Engines", total=10, id="A"):
    return AlbumHit(id, name, (artist,), "album", "2009", total)


class AlbumCoreTest(unittest.TestCase):
    def test_packaging_words_are_dropped_from_album_titles(self):
        self.assertEqual(album_core("Harbour Lights (Deluxe Version)"), "harbour lights")
        self.assertEqual(album_core("Night Drive - EP"), "night drive")
        self.assertEqual(album_core("Harbour Lights [Remastered 2011]"), "harbour lights")


class AlbumGroupsTest(unittest.TestCase):
    def test_tracks_are_grouped_by_album_artist_and_title(self):
        groups = album_groups(tracks(track(album="Harbour Lights"), track(album="Harbour Lights"),
                                     track(album="Other")))
        self.assertEqual([(g.artist, g.album, len(g.track_ids)) for g in groups],
                         [("The Quiet Engines", "Harbour Lights", 2)])

    def test_a_single_track_from_an_album_is_left_to_track_search(self):
        self.assertEqual(album_groups(tracks(track(album="Harbour Lights"))), [])

    def test_a_compilation_by_mixed_artists_is_searched_by_title_only(self):
        [group] = album_groups(tracks(track(artist="Kilo", albumArtist="Various Artists", album="Now 44"),
                                      track(artist="Moth", albumArtist="Various Artists", album="Now 44")))
        self.assertEqual(group.artist, "")
        self.assertEqual(group.queries(), ["album:now 44"])

    def test_an_album_without_album_artist_by_one_artist_keeps_that_artist(self):
        [group] = album_groups(tracks(track(artist="Kilo", albumArtist="", album="Beats"),
                                      track(artist="Kilo feat. Moth", albumArtist="", album="Beats")))
        self.assertEqual(group.queries(), ["album:beats artist:kilo", "kilo beats"])


class RankAlbumsTest(unittest.TestCase):
    def setUp(self):
        [self.group] = album_groups(tracks(track(album="Harbour Lights (Deluxe Version)"),
                                           track(album="Harbour Lights (Deluxe Version)")))

    def test_albums_with_another_title_or_artist_are_not_candidates(self):
        self.assertEqual(rank_albums(self.group, [hit("Low Tide"), hit("Harbour Lights", "Kilo")]), [])

    def test_the_edition_closest_in_size_comes_first(self):
        ranked = rank_albums(self.group, [hit("Harbour Lights", total=40, id="big"),
                                          hit("Harbour Lights", total=3, id="fit")])
        self.assertEqual([h.id for h in ranked], ["fit", "big"])

    def test_an_edition_with_fewer_tracks_than_the_library_has_comes_last(self):
        ranked = rank_albums(self.group, [hit("Harbour Lights", total=1, id="short"),
                                          hit("Harbour Lights", total=12, id="full")])
        self.assertEqual([h.id for h in ranked], ["full", "short"])


if __name__ == "__main__":
    unittest.main()
