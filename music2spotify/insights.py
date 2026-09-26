"""Extra playlists drawn from the Music listening history.

The rules copy the ones in Kuba's own smart playlists where there are any:
"rock" means the genre contains "rock" (Rock, Punkrock, Rockabilly, Surf Rock,
Electro-Rock), and ratings are the ones Music shows, including those derived
from an album rating.

Ranked playlists are filled from the top of the ranking with tracks that are on
Spotify, so "Top 100" holds 100 songs; the description says how many
higher-ranked songs were passed over because Spotify does not have them.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from .library import Library, Track
from .matching import MATCHED
from .normalize import fold
from .pipeline import MatchResult
from .plan import PlannedPlaylist

ROOT = "Insights"
GENRE_MIN_TRACKS = 50
YEAR_MIN_TRACKS = 50
DECADE_MIN_TRACKS = 30
FORGOTTEN_BEFORE = datetime(2017, 1, 1, tzinfo=timezone.utc)
BPM_RANGES = ((85, 100), (120, 128), (138, 145), (170, 176))


def is_rock(track: Track) -> bool:
    return "rock" in track.genre.casefold()


def by_plays(track: Track) -> tuple:
    return (-track.plays, -track.rating, track.artist.casefold(), track.name.casefold())


@dataclass
class Context:
    library: Library
    results: dict[str, MatchResult]

    @property
    def music(self) -> list[Track]:
        return [t for t in self.library.tracks.values() if t.is_music]

    def uri(self, track: Track) -> str | None:
        result = self.results.get(track.id)
        return result.uri if result and result.status == MATCHED else None

    def playlist(self, group: str, slug: str, name: str, origin: str, ranked: Iterable[Track],
                 limit: int | None = None) -> PlannedPlaylist:
        """Take tracks in ranking order, keeping those on Spotify, up to the limit."""
        uris: list[str] = []
        considered = 0
        for track in ranked:
            if limit is not None and len(uris) >= limit:
                break
            considered += 1
            uri = self.uri(track)
            if uri and uri not in uris:
                uris.append(uri)
        return PlannedPlaylist(f"insight:{fold(group).replace(' ', '-')}:{slug}", [ROOT, group],
                               name, uris, considered, origin)


def canonical_genres(tracks: list[Track]) -> dict[str, str]:
    """Spelling variants ("Hip-hop", "Hip-Hop") under their most common spelling."""
    spellings: dict[str, Counter] = defaultdict(Counter)
    for track in tracks:
        if track.genre.strip():
            spellings[fold(track.genre)][track.genre.strip()] += 1
    return {key: counts.most_common(1)[0][0] for key, counts in spellings.items()}


def rankings(ctx: Context) -> list[PlannedPlaylist]:
    music = sorted(ctx.music, key=by_plays)
    group = "Rankings"
    playlists = [
        ctx.playlist(group, "top-100", "Top 100", "Most played in Music", music, 100),
        ctx.playlist(group, "top-100-rock", "Top 100 · Rock",
                     "Most played rock in Music (genre contains \"rock\")",
                     [t for t in music if is_rock(t)], 100),
        ctx.playlist(group, "top-100-not-rock", "Top 100 · Not rock",
                     "Most played outside rock in Music", [t for t in music if not is_rock(t)], 100),
    ]
    plays_by_artist: Counter = Counter()
    for track in music:
        if track.artist:
            plays_by_artist[fold(track.artist)] += track.plays
    top_artists = [artist for artist, _ in plays_by_artist.most_common(10)]
    picks = []
    for artist in top_artists:
        own = [t for t in music if fold(t.artist) == artist]
        on_spotify = [t for t in own if ctx.uri(t)]
        picks += on_spotify[:5]
    playlists.append(ctx.playlist(group, "top-artists", "Top 10 artists · 5 each",
                                  "The 10 most played artists in Music, their 5 most played songs",
                                  picks))
    return playlists


def genres(ctx: Context) -> list[PlannedPlaylist]:
    music = ctx.music
    names = canonical_genres(music)
    counts = Counter(fold(t.genre) for t in music if t.genre.strip())
    playlists = []
    for key, count in counts.most_common():
        if count < GENRE_MIN_TRACKS:
            continue
        genre = names[key]
        ranked = sorted((t for t in music if fold(t.genre) == key), key=by_plays)
        playlists.append(ctx.playlist("Genres", key.replace(" ", "-"), f"Top 25 · {genre}",
                                      f"Most played {genre} in Music", ranked, 25))
    return playlists


def ratings(ctx: Context) -> list[PlannedPlaylist]:
    music = ctx.music
    group = "Ratings"

    def rated(test: Callable[[Track], bool], key=by_plays) -> list[Track]:
        return sorted((t for t in music if test(t)), key=key)

    return [
        ctx.playlist(group, "5-stars", "5 stars", "Rated 5 stars in Music, most played first",
                     rated(lambda t: t.stars == 5)),
        ctx.playlist(group, "4-stars-and-up", "4 stars and up",
                     "Rated 4 or 5 stars in Music, best rated and most played first",
                     rated(lambda t: t.stars >= 4, key=lambda t: (-t.stars, *by_plays(t)))),
        ctx.playlist(group, "forgotten-gems", "Forgotten gems",
                     "4+ stars, played 20+ times, not played since 2016",
                     rated(lambda t: t.stars >= 4 and t.plays >= 20 and t.last_played is not None
                           and t.last_played < FORGOTTEN_BEFORE)),
        ctx.playlist(group, "undiscovered", "Undiscovered",
                     "4+ stars but played fewer than 5 times",
                     rated(lambda t: t.stars >= 4 and t.plays < 5,
                           key=lambda t: (-t.stars, t.plays, t.artist.casefold(), t.name.casefold()))),
    ]


def timeline(ctx: Context) -> list[PlannedPlaylist]:
    music = ctx.music
    group = "Timeline"
    playlists = []
    last_years = Counter(t.last_played.year for t in music if t.last_played)
    for year in sorted(y for y, n in last_years.items() if n >= YEAR_MIN_TRACKS):
        ranked = sorted((t for t in music if t.last_played and t.last_played.year == year), key=by_plays)
        playlists.append(ctx.playlist(group, f"last-played-{year}", f"Last played in {year}",
                                      f"Songs last played in {year}, most played first", ranked, 100))
    this_year = datetime.now(timezone.utc).year
    decades = Counter(t.year // 10 * 10 for t in music if t.year and t.year <= this_year)
    for decade in sorted(d for d, n in decades.items() if n >= DECADE_MIN_TRACKS):
        ranked = sorted((t for t in music if t.year and t.year // 10 * 10 == decade
                         and t.year <= this_year), key=by_plays)
        playlists.append(ctx.playlist(group, f"released-{decade}s", f"Released in the {decade}s",
                                      f"Released {decade}–{decade + 9}, most played first",
                                      ranked, 100))
    return playlists


def bpm(ctx: Context) -> list[PlannedPlaylist]:
    playlists = []
    for low, high in BPM_RANGES:
        ranked = sorted((t for t in ctx.music if t.bpm and low <= t.bpm <= high),
                        key=lambda t: (t.bpm, *by_plays(t)))
        playlists.append(ctx.playlist("BPM", f"{low}-{high}", f"{low}–{high} BPM",
                                      f"Tempo {low}–{high} BPM from the Music tags, slowest first",
                                      ranked))
    return playlists


GROUPS = (rankings, genres, ratings, timeline, bpm)


def build_insights(library: Library, results: dict[str, MatchResult]) -> list[PlannedPlaylist]:
    """Every insight that has something in it. Unlike the Music mirrors, which are
    kept even when empty to complete the structure, an empty insight says nothing."""
    ctx = Context(library, results)
    return [playlist for group in GROUPS for playlist in group(ctx) if playlist.uris]
