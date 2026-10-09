"""A terminal UI for the decisions matching leaves to Kuba.

Three lists: tracks with an uncertain match or only another version on
Spotify, tracks not found at all, and everything already decided. Each can be
played in both apps for comparison, and every decision lands in
data/overrides.csv immediately, so quitting at any point loses nothing;
`make apply` then pushes the decisions to Spotify.

The session (lists, decisions, persistence) is kept apart from the drawing so
it can be tested without a terminal.
"""

from __future__ import annotations

import curses
import locale
import os
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .library import Library, Track
from .matching import ALTERNATIVE, NOT_FOUND, UNCERTAIN, Candidate
from .normalize import fold
from .pipeline import NONE, SKIP, MatchResult, Override, load_overrides, save_overrides, spotify_track_id
from .players import Player
from .reports import length, memberships

TO_DECIDE, NOT_FOUND_TAB, DECIDED = 0, 1, 2
TAB_NAMES = ("To decide", "Not found", "Decided")
NOTE = {"accept": "taken in review", "none": "not on Spotify (review)",
        "skip": "left out in review", "link": "linked by hand in review"}


@dataclass
class Item:
    track: Track
    result: MatchResult
    playlists: list[str]

    @property
    def label(self) -> str:
        return f"{self.track.artist or '(no artist)'} — {self.track.name}"


class ReviewSession:
    def __init__(self, library: Library, results: dict[str, MatchResult], overrides_path: Path,
                 lookup: Callable[[str], Candidate | None] | None = None,
                 cached: Callable[[str], Candidate | None] | None = None):
        self.overrides_path = overrides_path
        self.overrides = load_overrides(overrides_path)
        # lookup may ask Spotify (used once, when a link is pasted); cached only
        # reads what is already known, so drawing the screen never waits on the network.
        self._lookup = lookup or (lambda track_id: None)
        self._cached = cached or (lambda track_id: None)
        self._linked: dict[str, Candidate | None] = {}
        member_of = memberships(library)
        items = {i: Item(library.tracks[i], r, member_of.get(i, []))
                 for i, r in results.items() if i in library.tracks and library.tracks[i].is_music}
        self.items = items
        order = sorted(items.values(), key=lambda it: (-it.track.plays, it.label.casefold()))
        # The first two lists are fixed when the session starts, so a track does
        # not jump away the moment it is decided; its badge changes instead.
        self._lists = {
            TO_DECIDE: [it for it in order if it.result.status in (UNCERTAIN, ALTERNATIVE)],
            NOT_FOUND_TAB: [it for it in order if it.result.status == NOT_FOUND
                            and it.result.source != "override"],
        }
        self._order = order

    def items_in(self, tab: int, query: str = "") -> list[Item]:
        if tab == DECIDED:
            found = [it for it in self._order if it.track.id in self.overrides]
        else:
            found = self._lists[tab]
        if query:
            words = fold(query).split()
            found = [it for it in found
                     if all(w in fold(f"{it.track.artist} {it.track.name} {it.track.album}") for w in words)]
        return found

    def counts(self, tab: int) -> tuple[int, int]:
        """(decided, total) for a tab."""
        items = self.items_in(tab)
        return sum(1 for it in items if it.track.id in self.overrides), len(items)

    def decision(self, item: Item) -> Override | None:
        return self.overrides.get(item.track.id)

    def accept(self, item: Item) -> bool:
        if item.result.candidate is None:
            return False
        self._set(Override(item.track.id, item.result.candidate.id, NOTE["accept"]))
        return True

    def reject(self, item: Item) -> None:
        self._set(Override(item.track.id, NONE, NOTE["none"]))

    def leave_out(self, item: Item) -> None:
        self._set(Override(item.track.id, SKIP, NOTE["skip"]))

    def link(self, item: Item, text: str) -> bool:
        track_id = spotify_track_id(text)
        if track_id is None:
            return False
        self._set(Override(item.track.id, track_id, NOTE["link"]))
        try:
            self._linked[track_id] = self._lookup(track_id)
        except Exception:  # noqa: BLE001 - offline or logged out: the ID alone is enough to save
            self._linked[track_id] = None
        return True

    def linked_track(self, track_id: str) -> Candidate | None:
        """What a pasted link points at, when known, for showing and playing it."""
        if track_id not in self._linked:
            self._linked[track_id] = self._cached(track_id)
        return self._linked[track_id]

    def spotify_to_play(self, item: Item) -> Candidate | None:
        """The Spotify track that will be used: the pasted link if any, else the candidate."""
        decided = self.decision(item)
        if decided and decided.spotify not in (NONE, SKIP) and (
                item.result.candidate is None or decided.spotify != item.result.candidate.id):
            known = self.linked_track(decided.spotify)
            return known or Candidate(f"spotify:track:{decided.spotify}", decided.spotify, (), "", "", "", 0)
        return item.result.candidate

    def undo(self, item: Item) -> bool:
        if self.overrides.pop(item.track.id, None) is None:
            return False
        self._save()
        return True

    def _set(self, override: Override) -> None:
        self.overrides[override.track_id] = override
        self._save()

    def _save(self) -> None:
        # Written to a side file and moved into place, so a crash or Ctrl-C in the
        # middle of a write never leaves a half-written overrides file.
        tmp = self.overrides_path.with_suffix(".csv.tmp")
        save_overrides(tmp, self.overrides)
        os.replace(tmp, self.overrides_path)


# --- text helpers ----------------------------------------------------------------

def width_of(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def fit(text: str, width: int) -> str:
    """Cut to a display width (wide characters count twice), ellipsis included,
    and pad to exactly that width."""
    if width <= 0:
        return ""
    if width_of(text) <= width:
        return text + " " * (width - width_of(text))
    out, used = [], 0
    for char in text:
        w = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + w > width - 1:
            break
        out.append(char)
        used += w
    return "".join(out) + "…" + " " * (width - used - 1)


def badge(session: ReviewSession, item: Item) -> tuple[str, str]:
    """A one-character mark and its style for the list."""
    decided = session.decision(item)
    if decided is None:
        return "•", {UNCERTAIN: "warn", ALTERNATIVE: "alt"}.get(item.result.status, "bad")
    if decided.spotify == NONE:
        return "✗", "bad"
    if decided.spotify == SKIP:
        return "⊘", "dim"
    if item.result.candidate and decided.spotify == item.result.candidate.id:
        return "✓", "good"
    return "↗", "accent"


def list_line(item: Item, width: int) -> str:
    return fit(f"{item.track.plays:>4}  {item.label}", width)


STATUS_TITLE = {UNCERTAIN: ("probably the same", "warn"), ALTERNATIVE: ("another version", "alt"),
                NOT_FOUND: ("closest result", "bad")}


def describe_decision(decided: Override | None, item: Item,
                      linked: Candidate | None = None) -> tuple[str, str]:
    if decided is None:
        return "undecided", "dim"
    if decided.spotify == NONE:
        return "✗ not on Spotify — stays missing", "bad"
    if decided.spotify == SKIP:
        return "⊘ left out of Spotify", "dim"
    if item.result.candidate and decided.spotify == item.result.candidate.id:
        return "✓ take this Spotify track", "good"
    if linked:
        details = " · ".join(x for x in (linked.album, linked.release_date[:4], length(linked.duration)) if x)
        return f"↗ your link: {linked.label} ({details})", "accent"
    return f"↗ your link: open.spotify.com/track/{decided.spotify}", "accent"


def detail_lines(session: ReviewSession, item: Item) -> list[tuple[str, str]]:
    t, r = item.track, item.result
    stars = "★" * t.stars + "☆" * (5 - t.stars) if t.stars else "unrated"
    last = t.last_played.strftime("%Y-%m-%d") if t.last_played else "never"
    lines: list[tuple[str, str]] = [
        ("MUSIC", "heading"),
        (t.name, "bold"),
        (t.artist or "(no artist)", "normal"),
        (" · ".join(x for x in (t.album, str(t.year or ""), length(t.duration), t.genre) if x), "dim"),
        (f"{t.plays} plays · {stars} · last played {last}", "dim"),
    ]
    if item.playlists:
        more = f" (+{len(item.playlists) - 2})" if len(item.playlists) > 2 else ""
        lines.append(("in " + " · ".join(item.playlists[:2]) + more, "dim"))
    lines.append(("", "normal"))
    title, style = STATUS_TITLE.get(r.status, ("Spotify", "accent"))
    candidate = r.candidate
    if candidate:
        lines += [
            (f"SPOTIFY · {title}", "heading-" + style),
            (candidate.name, "bold"),
            (", ".join(candidate.artists), "normal"),
            (" · ".join(x for x in (candidate.album, candidate.release_date[:4],
                                    length(candidate.duration)) if x), "dim"),
        ]
        if not candidate.playable:
            lines.append(("not playable in this market", "bad"))
    else:
        lines.append(("SPOTIFY · nothing close found", "heading-bad"))
    if r.notes:
        lines += [("", "normal"), ("WHY", "heading"), *[(n, "normal") for n in r.notes]]
    decided = session.decision(item)
    linked = (session.linked_track(decided.spotify)
              if decided and decided.spotify not in (NONE, SKIP) else None)
    lines += [("", "normal"), ("DECISION", "heading"), describe_decision(decided, item, linked)]
    if decided and decided.note:
        lines.append((decided.note, "dim"))
    return lines


HINTS = [("y", "take"), ("n", "not on Spotify"), ("x", "leave out"), ("l", "link"), ("u", "undo"),
         ("p", "▶ Spotify"), ("m", "▶ Music"), ("␣", "pause"), ("o", "open"), ("/", "filter")]
ALWAYS = [("?", "help"), ("q", "quit")]  # shown on the right however narrow the window

HELP = [
    ("↑ ↓  j k", "move · PgUp PgDn · g G top/bottom"),
    ("1 2 3  ⇥", "switch list"),
    ("y", "take the Spotify track shown"),
    ("n", "not on Spotify: the song stays missing, no more proposals"),
    ("x", "leave the song out of Spotify altogether"),
    ("l", "paste a Spotify track link you found yourself"),
    ("u", "undo the decision"),
    ("p", "play the Spotify track in the Spotify app"),
    ("m", "play your file in Music"),
    ("space", "pause whatever is playing"),
    ("o", "open the track in Spotify, or search for it"),
    ("/", "filter by artist, title or album · Esc clears"),
    ("q", "quit · decisions are already saved; then run make apply"),
]


# --- drawing --------------------------------------------------------------------

@dataclass
class UiState:
    tab: int = TO_DECIDE
    cursor: list[int] = field(default_factory=lambda: [0, 0, 0])
    top: list[int] = field(default_factory=lambda: [0, 0, 0])
    query: str = ""
    message: str = ""
    help: bool = False


class Screen:
    """Draws the session on a curses window."""

    def __init__(self, window, session: ReviewSession, player: Player):
        self.win, self.session, self.player = window, session, player
        self.styles = self._styles()

    @staticmethod
    def _styles() -> dict[str, int]:
        plain = {name: curses.A_NORMAL for name in
                 ("normal", "dim", "bold", "heading", "good", "bad", "warn", "alt", "accent",
                  "selected", "bar", "tab", "tab-active", "key")}
        plain.update(bold=curses.A_BOLD, heading=curses.A_BOLD, selected=curses.A_REVERSE,
                     bar=curses.A_REVERSE, **{"tab-active": curses.A_REVERSE})
        if not curses.has_colors():
            return plain
        curses.start_color()
        curses.use_default_colors()
        rich = curses.COLORS >= 256
        pairs = {
            "accent": (45 if rich else curses.COLOR_CYAN, -1),
            "good": (114 if rich else curses.COLOR_GREEN, -1),
            "bad": (203 if rich else curses.COLOR_RED, -1),
            "warn": (221 if rich else curses.COLOR_YELLOW, -1),
            "alt": (177 if rich else curses.COLOR_MAGENTA, -1),
            "dim": (245 if rich else curses.COLOR_WHITE, -1),
            "selected": (255 if rich else curses.COLOR_BLACK, 238 if rich else curses.COLOR_CYAN),
            "bar": (16 if rich else curses.COLOR_BLACK, 45 if rich else curses.COLOR_CYAN),
            "tab-active": (16 if rich else curses.COLOR_BLACK, 221 if rich else curses.COLOR_YELLOW),
            "key": (16 if rich else curses.COLOR_BLACK, 245 if rich else curses.COLOR_WHITE),
        }
        styles = dict(plain)
        for number, (name, (fg, bg)) in enumerate(pairs.items(), start=1):
            curses.init_pair(number, fg, bg)
            styles[name] = curses.color_pair(number)
        styles["heading"] = styles["accent"] | curses.A_BOLD
        for name in ("good", "bad", "warn", "alt", "accent"):
            styles[f"heading-{name}"] = styles[name] | curses.A_BOLD
        styles["bold"] = curses.A_BOLD
        styles["bar"] |= curses.A_BOLD
        return styles

    def put(self, y: int, x: int, text: str, style: str = "normal") -> None:
        height, width = self.win.getmaxyx()
        if 0 <= y < height and x < width:
            try:
                self.win.addstr(y, x, fit(text, width - x) if width_of(text) > width - x else text,
                                self.styles.get(style, curses.A_NORMAL))
            except curses.error:
                pass  # the bottom-right cell cannot be written; nothing is lost

    def box(self, y: int, x: int, h: int, w: int, title: str = "") -> None:
        dim = "dim"
        self.put(y, x, "╭" + "─" * (w - 2) + "╮", dim)
        for row in range(y + 1, y + h - 1):
            self.put(row, x, "│", dim)
            self.put(row, x + w - 1, "│", dim)
        self.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", dim)
        if title:
            self.put(y, x + 2, f" {title} ", "heading")

    def draw(self, ui: UiState) -> None:
        self.win.erase()
        height, width = self.win.getmaxyx()
        if height < 18 or width < 80:
            self.put(0, 0, "Make the window at least 80×18 to review.", "warn")
            self.win.refresh()
            return
        decided, total = self.session.counts(TO_DECIDE)
        done = int(20 * decided / total) if total else 20
        bar = f" ♫ music2spotify · review   decided {decided}/{total} " + "█" * done + "░" * (20 - done) + " "
        self.put(0, 0, fit(bar, width), "bar")

        x = 1
        for tab, name in enumerate(TAB_NAMES):
            d, n = self.session.counts(tab)
            label = f" {tab + 1} {name} {n} " if tab == DECIDED else f" {tab + 1} {name} {d}/{n} "
            self.put(2, x, label, "tab-active" if tab == ui.tab else "dim")
            x += width_of(label) + 1
        if ui.query:
            self.put(2, x + 2, f"filter: {ui.query}", "accent")

        list_w = max(38, width // 2)
        body_h = height - 7
        self.box(3, 0, body_h + 2, list_w, TAB_NAMES[ui.tab])
        self.box(3, list_w, body_h + 2, width - list_w, "Details")
        items = self.session.items_in(ui.tab, ui.query)
        cursor = min(ui.cursor[ui.tab], max(0, len(items) - 1))
        ui.cursor[ui.tab] = cursor
        top = ui.top[ui.tab]
        if cursor < top:
            top = cursor
        elif cursor >= top + body_h:
            top = cursor - body_h + 1
        ui.top[ui.tab] = top
        for row, item in enumerate(items[top:top + body_h]):
            mark, mark_style = badge(self.session, item)
            selected = top + row == cursor
            line = list_line(item, list_w - 5)
            self.put(4 + row, 1, " ", "selected" if selected else "normal")
            self.put(4 + row, 2, mark, mark_style)
            self.put(4 + row, 3, " " + line, "selected" if selected else "normal")
        if not items:
            self.put(5, 3, "nothing here" + (" for this filter" if ui.query else ""), "dim")
        elif len(items) > body_h:
            self.put(3 + body_h + 1, list_w - 14, f" {cursor + 1}/{len(items)} ", "dim")

        if items:
            for row, (text, style) in enumerate(detail_lines(self.session, items[cursor])[:body_h]):
                self.put(4 + row, list_w + 2, fit(text, width - list_w - 4), style)

        status = ui.message or (f"♪ {self.player.now_playing}" if self.player.now_playing else "")
        self.put(height - 2, 1, fit(status, width - 2), "accent")
        def hint_width(pairs):
            return sum(width_of(k) + width_of(w) + 4 for k, w in pairs)

        right = width - hint_width(ALWAYS) - 1
        x = 1
        for key, what in HINTS:
            if x + width_of(key) + width_of(what) + 4 > right:
                break
            x = self._hint(height - 1, x, key, what)
        x = right
        for key, what in ALWAYS:
            x = self._hint(height - 1, x, key, what)
        if ui.help:
            self._help(height, width)
        self.win.refresh()

    def _hint(self, y: int, x: int, key: str, what: str) -> int:
        self.put(y, x, f" {key} ", "key")
        self.put(y, x + width_of(key) + 2, f" {what} ", "dim")
        return x + width_of(key) + width_of(what) + 4

    def _help(self, height: int, width: int) -> None:
        w, h = min(78, width - 4), len(HELP) + 4
        y, x = (height - h) // 2, (width - w) // 2
        for row in range(h):
            self.put(y + row, x, " " * w, "normal")
        self.box(y, x, h, w, "Keys")
        for row, (key, what) in enumerate(HELP):
            self.put(y + 2 + row, x + 3, fit(key, 10), "accent")
            self.put(y + 2 + row, x + 14, fit(what, w - 17), "normal")

    def ask(self, prompt: str) -> str | None:
        """A one-line text field at the bottom; None when cancelled with Esc."""
        height, width = self.win.getmaxyx()
        text = ""
        curses.curs_set(1)
        try:
            while True:
                self.put(height - 2, 1, fit(f"{prompt}{text}", width - 2), "accent")
                self.win.move(height - 2, min(width - 2, 1 + width_of(prompt) + width_of(text)))
                self.win.refresh()
                key = self.win.get_wch()
                if key in ("\n", "\r", curses.KEY_ENTER):
                    return text.strip()
                if key == "\x1b":
                    return None
                if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    text = text[:-1]
                elif isinstance(key, str) and key.isprintable():
                    text += key
        finally:
            curses.curs_set(0)


def handle(key, ui: UiState, session: ReviewSession, player: Player, screen: Screen | None,
           page: int = 10) -> bool:
    """Apply one key press. Returns False when the user quits."""
    items = session.items_in(ui.tab, ui.query)
    item = items[ui.cursor[ui.tab]] if items and ui.cursor[ui.tab] < len(items) else None
    ui.message = ""
    if ui.help:
        ui.help = False
        return True
    if key in ("q", "Q"):
        return False
    if key in ("?", "h"):
        ui.help = True
    elif key in (curses.KEY_DOWN, "j"):
        ui.cursor[ui.tab] = min(ui.cursor[ui.tab] + 1, max(0, len(items) - 1))
    elif key in (curses.KEY_UP, "k"):
        ui.cursor[ui.tab] = max(ui.cursor[ui.tab] - 1, 0)
    elif key == curses.KEY_NPAGE:
        ui.cursor[ui.tab] = min(ui.cursor[ui.tab] + page, max(0, len(items) - 1))
    elif key == curses.KEY_PPAGE:
        ui.cursor[ui.tab] = max(ui.cursor[ui.tab] - page, 0)
    elif key in ("g", curses.KEY_HOME):
        ui.cursor[ui.tab] = 0
    elif key in ("G", curses.KEY_END):
        ui.cursor[ui.tab] = max(0, len(items) - 1)
    elif key in ("1", "2", "3"):
        ui.tab = int(key) - 1
    elif key == "\t":
        ui.tab = (ui.tab + 1) % len(TAB_NAMES)
    elif key == "/" and screen:
        query = screen.ask("filter: ")
        if query is not None:
            ui.query = query
            ui.cursor[ui.tab] = 0
    elif key == "\x1b":
        ui.query = ""
    elif key == " ":
        player.pause()
    elif item is None:
        return True
    elif key == "y":
        ui.message = "✓ saved: take the Spotify track" if session.accept(item) else "nothing on Spotify to take; paste a link with l"
        _advance(ui, items)
    elif key == "n":
        session.reject(item)
        ui.message = "✗ saved: not on Spotify"
        _advance(ui, items)
    elif key == "x":
        session.leave_out(item)
        ui.message = "⊘ saved: left out of Spotify"
        _advance(ui, items)
    elif key == "u":
        ui.message = "decision removed" if session.undo(item) else "nothing to undo"
    elif key == "l" and screen:
        text = screen.ask("Spotify track link: ")
        if text:
            if session.link(item, text):
                linked = session.linked_track(spotify_track_id(text))
                ui.message = f"↗ saved: {linked.label}" if linked else "↗ saved: your link (title shown after make apply)"
            else:
                ui.message = "that is not a Spotify track link"
    elif key == "p":
        target = session.spotify_to_play(item)
        if target:
            player.play_spotify(target.id, target.label if target.artists else "your link")
        else:
            ui.message = "no Spotify track to play; o searches Spotify for it"
    elif key == "m":
        player.play_music(item.track.id, item.label)
    elif key == "o":
        if item.result.candidate:
            player.open_track(item.result.candidate.id)
        else:
            player.open_search(f"{item.track.artist} {item.track.name}")
    return True


def _advance(ui: UiState, items: list[Item]) -> None:
    ui.cursor[ui.tab] = min(ui.cursor[ui.tab] + 1, max(0, len(items) - 1))


def run(session: ReviewSession, player: Player | None = None) -> None:
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")  # Esc should not lag a second behind
    player = player or Player()

    def main(window) -> None:
        curses.curs_set(0)
        window.keypad(True)
        screen = Screen(window, session, player)
        ui = UiState()
        while True:
            screen.draw(ui)
            try:
                key = window.get_wch()
            except curses.error:
                continue
            if key == curses.KEY_RESIZE:
                continue
            height = window.getmaxyx()[0]
            if not handle(key, ui, session, player, screen, page=max(1, height - 8)):
                break

    try:
        curses.wrapper(main)
    finally:
        player.pause()
