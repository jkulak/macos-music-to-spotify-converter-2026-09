import curses
import tempfile
import unittest
from pathlib import Path

from music2spotify.library import parse_library
from music2spotify.matching import ALTERNATIVE, MATCHED, NOT_FOUND, SKIPPED, UNCERTAIN
from music2spotify.pipeline import NONE, SKIP, MatchResult, Override, load_overrides, save_overrides
from music2spotify.players import Player
from music2spotify.review import (
    DECIDED,
    NOT_FOUND_TAB,
    TO_DECIDE,
    ReviewSession,
    UiState,
    badge,
    detail_lines,
    fit,
    handle,
    width_of,
)
from tests.builders import candidate, playlist, snapshot, track

LINKED = "6rqhFgbbKwnb9MLmUQDhG6"


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name, "overrides.csv")
        self.maybe = track(name="Harbour", artist="Kilo", plays=10)
        self.other = track(name="Night Drive (Velvet Kid Remix)", artist="Kilo", plays=90)
        self.lost = track(name="Zażółć", artist="Gęślą Jaźń", plays=40)
        self.done = track(name="Paper Lanterns", plays=5)
        self.pinned = track(name="Low Tide", plays=1)
        self.memo = track(name="Memo", genre="Voice Memo")
        raw = [self.maybe, self.other, self.lost, self.done, self.pinned, self.memo]
        self.library = parse_library(snapshot(raw, [playlist("Best / Mix", tracks=[self.other])]))
        self.results = {
            self.maybe["id"]: MatchResult(self.maybe["id"], UNCERTAIN, candidate("Harbour", ["Kilo"], id="1" * 22),
                                          notes=["length differs by 40s"]),
            self.other["id"]: MatchResult(self.other["id"], ALTERNATIVE, candidate("Night Drive", ["Kilo"], id="2" * 22),
                                          notes=["Music has velvet kid remix, Spotify has the original"]),
            self.lost["id"]: MatchResult(self.lost["id"], NOT_FOUND, notes=["no search results"]),
            self.done["id"]: MatchResult(self.done["id"], MATCHED, candidate("Paper Lanterns", ["Q"], id="3" * 22)),
            self.pinned["id"]: MatchResult(self.pinned["id"], NOT_FOUND, source="override"),
            self.memo["id"]: MatchResult(self.memo["id"], SKIPPED),
        }
        save_overrides(self.path, {self.pinned["id"]: Override(self.pinned["id"], NONE, "rejected in review")})
        self.session = ReviewSession(self.library, self.results, self.path)

    def names(self, tab, query=""):
        return [it.track.name for it in self.session.items_in(tab, query)]

    def item(self, raw):
        return self.session.items[raw["id"]]

    def test_the_lists_hold_open_decisions_missing_tracks_and_decided_ones_most_played_first(self):
        self.assertEqual(self.names(TO_DECIDE), ["Night Drive (Velvet Kid Remix)", "Harbour"])
        self.assertEqual(self.names(NOT_FOUND_TAB), ["Zażółć"])
        self.assertEqual(self.names(DECIDED), ["Low Tide"])

    def test_the_filter_ignores_case_and_polish_letters(self):
        self.assertEqual(self.names(NOT_FOUND_TAB, "gesla JAZN"), ["Zażółć"])
        self.assertEqual(self.names(TO_DECIDE, "velvet"), ["Night Drive (Velvet Kid Remix)"])

    def test_taking_the_candidate_is_saved_at_once(self):
        self.assertTrue(self.session.accept(self.item(self.maybe)))
        saved = load_overrides(self.path)[self.maybe["id"]]
        self.assertEqual((saved.spotify, saved.note), ("1" * 22, "taken in review"))
        self.assertEqual(self.session.counts(TO_DECIDE), (1, 2))
        self.assertIn("Harbour", self.names(DECIDED))

    def test_a_track_with_nothing_found_cannot_be_taken_but_can_be_linked_by_hand(self):
        lost = self.item(self.lost)
        self.assertFalse(self.session.accept(lost))
        self.assertFalse(self.session.link(lost, "https://open.spotify.com/album/" + LINKED))
        self.assertTrue(self.session.link(lost, f"https://open.spotify.com/track/{LINKED}?si=abc"))
        self.assertEqual(load_overrides(self.path)[self.lost["id"]].spotify, LINKED)

    def test_not_on_spotify_and_leave_out_are_saved_and_undo_removes_them(self):
        self.session.reject(self.item(self.other))
        self.session.leave_out(self.item(self.maybe))
        saved = load_overrides(self.path)
        self.assertEqual((saved[self.other["id"]].spotify, saved[self.maybe["id"]].spotify), (NONE, SKIP))
        self.assertTrue(self.session.undo(self.item(self.other)))
        self.assertNotIn(self.other["id"], load_overrides(self.path))
        self.assertFalse(self.session.undo(self.item(self.lost)))

    def test_decisions_made_elsewhere_are_kept_when_saving(self):
        self.session.accept(self.item(self.maybe))
        self.assertIn(self.pinned["id"], load_overrides(self.path))

    def test_the_badge_shows_the_decision_or_the_kind_of_doubt(self):
        self.assertEqual(badge(self.session, self.item(self.maybe)), ("•", "warn"))
        self.assertEqual(badge(self.session, self.item(self.other)), ("•", "alt"))
        self.session.accept(self.item(self.maybe))
        self.session.link(self.item(self.lost), LINKED)
        self.assertEqual(badge(self.session, self.item(self.maybe)), ("✓", "good"))
        self.assertEqual(badge(self.session, self.item(self.lost)), ("↗", "accent"))

    def test_details_put_both_sides_and_the_reason_next_to_each_other(self):
        text = [line for line, _ in detail_lines(self.session, self.item(self.other))]
        self.assertIn("Night Drive (Velvet Kid Remix)", text)
        self.assertIn("SPOTIFY · another version", text)
        self.assertIn("Music has velvet kid remix, Spotify has the original", text)
        self.assertIn("in Best / Mix", text)
        self.assertEqual(text[-1], "undecided")


class KeysTest(SessionTest):
    def press(self, *keys, tab=TO_DECIDE):
        commands = []
        ui = UiState(tab=tab)
        player = Player(run=commands.append)
        for key in keys:
            handle(key, ui, self.session, player, screen=None)
        return ui, commands

    def test_y_takes_the_selected_track_and_moves_to_the_next(self):
        ui, _ = self.press("y")
        self.assertEqual(load_overrides(self.path)[self.other["id"]].spotify, "2" * 22)
        self.assertEqual(ui.cursor[TO_DECIDE], 1)

    def test_p_and_m_play_the_two_versions_and_space_pauses(self):
        _, commands = self.press("p", "m", " ")
        self.assertIn("2" * 22, commands[0][2])
        self.assertIn(self.other["id"], commands[2][2])
        self.assertEqual(commands[-1], ["osascript", "-e", 'tell application "Music" to pause'])

    def test_o_searches_spotify_when_nothing_was_found(self):
        _, commands = self.press("o", tab=NOT_FOUND_TAB)
        self.assertTrue(commands[0][1].startswith("spotify:search:"))

    def test_moving_stays_inside_the_list_and_tabs_switch(self):
        ui, _ = self.press(curses.KEY_UP, "j", "j", "j")
        self.assertEqual(ui.cursor[TO_DECIDE], 1)
        ui, _ = self.press("3")
        self.assertEqual(ui.tab, DECIDED)

    def test_q_quits(self):
        self.assertFalse(handle("q", UiState(), self.session, Player(run=lambda a: None), None))


class TextFitTest(unittest.TestCase):
    def test_text_is_cut_with_an_ellipsis_and_padded_to_the_width(self):
        self.assertEqual(fit("Paper Lanterns", 8), "Paper L…")
        self.assertEqual(fit("Kilo", 6), "Kilo  ")

    def test_wide_characters_count_twice(self):
        self.assertEqual(width_of("リピート"), 8)
        self.assertEqual(width_of(fit("リピート・アフター", 7)), 7)


if __name__ == "__main__":
    unittest.main()


class PastedLinkTest(SessionTest):
    def linked(self):
        from tests.builders import candidate as make
        return make("Cockpit (Original Mix)", ["Kilo"], 240, album="Cockpit EP", id=LINKED)

    def test_a_pasted_link_shows_which_track_it_points_at(self):
        found = self.linked()
        session = ReviewSession(self.library, self.results, self.path, lookup=lambda i: found if i == LINKED else None)
        item = session.items[self.other["id"]]
        self.assertTrue(session.link(item, f"https://open.spotify.com/track/{LINKED}?si=x"))
        decision_line = [t for t, _ in detail_lines(session, item)][-2]
        self.assertEqual(decision_line, "↗ your link: Kilo - Cockpit (Original Mix) (Cockpit EP · 2009 · 4:00)")

    def test_play_plays_the_pasted_link_instead_of_the_candidate(self):
        session = ReviewSession(self.library, self.results, self.path, lookup=lambda i: self.linked())
        item = session.items[self.other["id"]]
        session.link(item, LINKED)
        commands = []
        handle("p", UiState(), session, Player(run=commands.append), screen=None)
        self.assertIn(f"spotify:track:{LINKED}", commands[0][2])

    def test_a_link_that_cannot_be_looked_up_is_still_saved_and_playable(self):
        def offline(track_id):
            raise OSError("no network")
        session = ReviewSession(self.library, self.results, self.path, lookup=offline)
        item = session.items[self.other["id"]]
        self.assertTrue(session.link(item, LINKED))
        self.assertEqual(session.spotify_to_play(item).id, LINKED)
        self.assertIn(f"open.spotify.com/track/{LINKED}", [t for t, _ in detail_lines(session, item)][-2])
