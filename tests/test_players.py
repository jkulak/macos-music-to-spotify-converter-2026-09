import unittest

from music2spotify.players import Player

SPOTIFY_ID = "4uLU6hMCjMI75M1A2tKUQC"


class PlayerTest(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.player = Player(run=self.commands.append)

    def test_the_spotify_candidate_is_played_in_the_spotify_app(self):
        self.player.play_spotify(SPOTIFY_ID, "Kilo - Night Drive")
        self.assertEqual(self.commands, [["osascript", "-e",
                                          f'tell application "Spotify" to play track "spotify:track:{SPOTIFY_ID}"']])
        self.assertEqual(self.player.now_playing, "Spotify: Kilo - Night Drive")

    def test_the_library_file_is_played_in_music_by_persistent_id(self):
        self.player.play_music("A93A0E83B49727FE", "Kilo - Night Drive")
        self.assertIn('whose persistent ID is "A93A0E83B49727FE"', self.commands[0][2])
        self.assertTrue(self.commands[0][2].startswith('tell application "Music" to play'))

    def test_switching_apps_pauses_the_other_one_first(self):
        self.player.play_music("A93A0E83B49727FE", "x")
        self.player.play_spotify(SPOTIFY_ID, "x")
        self.assertEqual(self.commands[1], ["osascript", "-e", 'tell application "Music" to pause'])

    def test_pause_talks_only_to_the_app_that_was_started(self):
        self.player.pause()
        self.assertEqual(self.commands, [])
        self.player.play_spotify(SPOTIFY_ID, "x")
        self.player.pause()
        self.assertEqual(self.commands[-1], ["osascript", "-e", 'tell application "Spotify" to pause'])
        self.assertEqual(self.player.now_playing, "")

    def test_ids_that_could_break_out_of_the_script_are_refused(self):
        with self.assertRaises(ValueError):
            self.player.play_spotify('x" & do shell script "rm', "x")
        with self.assertRaises(ValueError):
            self.player.play_music('A9" to quit', "x")
        self.assertEqual(self.commands, [])

    def test_a_missing_track_opens_a_spotify_search_with_the_text_encoded(self):
        self.player.open_search("Gęślą Jaźń & Kilo")
        self.assertEqual(self.commands, [["open", "spotify:search:G%C4%99%C5%9Bl%C4%85%20Ja%C5%BA%C5%84%20%26%20Kilo"]])

    def test_a_youtube_music_search_opens_in_the_browser_with_the_text_encoded(self):
        self.player.open_youtube("Gęślą Jaźń & Kilo #1 / ?")
        self.assertEqual(self.commands, [[
            "open", "https://music.youtube.com/search?q=G%C4%99%C5%9Bl%C4%85+Ja%C5%BA%C5%84+%26+Kilo+%231+%2F+%3F"]])


if __name__ == "__main__":
    unittest.main()
