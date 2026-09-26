"""Text normalisation and title parsing for comparing Music tracks with Spotify tracks.

Music titles in a DJ-heavy library carry a lot besides the song name: featured
artists, remixers, "(Original Mix)", Camelot keys, "Artist - Title" written into
the title field. Spotify writes the same things differently ("Song - Remastered
2011", "Song (feat. X)"). Everything here turns both sides into comparable parts.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

# Letters NFKD does not decompose into a base letter plus a combining mark.
_LETTERS = str.maketrans({
    "ł": "l", "Ł": "l", "ø": "o", "Ø": "o", "đ": "d", "Đ": "d", "ß": "ss",
    "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe", "þ": "th", "ı": "i",
    "$": "s", "€": "e", "@": "a",
})
_AND = re.compile(r"\s*&\s*|\s+\+\s+")
_NON_WORD = re.compile(r"[^\w\s]|_")
_SPACES = re.compile(r"\s+")


def fold(text: str) -> str:
    """Lower-case ASCII-ish form: no accents, no punctuation, '&' spelled 'and'."""
    text = unicodedata.normalize("NFKD", text.translate(_LETTERS))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _AND.sub(" and ", text.casefold())
    text = text.replace("'", "").replace("’", "")
    text = _NON_WORD.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def token_set_ratio(a: str, b: str) -> float:
    """Similarity that ignores word order and tolerates extra words on one side."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    common = " ".join(sorted(ta & tb))
    rest_a = " ".join(sorted(ta - tb))
    rest_b = " ".join(sorted(tb - ta))
    joined_a = f"{common} {rest_a}".strip()
    joined_b = f"{common} {rest_b}".strip()
    scores = [SequenceMatcher(None, joined_a, joined_b).ratio()]
    if common:
        scores += [SequenceMatcher(None, common, joined_a).ratio(),
                   SequenceMatcher(None, common, joined_b).ratio()]
    return max(scores)


def similarity(a: str, b: str) -> float:
    """Best of a plain sequence ratio and the order-insensitive token ratio."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return max(SequenceMatcher(None, a, b).ratio(), token_set_ratio(a, b))


# --- versions -----------------------------------------------------------------

# Markers that make a different recording or arrangement. The value says whether
# the marker names someone (the remixer, the bootlegger), which then has to match.
DISTINCT = {
    "remix": True, "rmx": True, "bootleg": True, "flip": True, "rework": True,
    "refix": True, "vip": False, "live": False, "acoustic": False,
    "unplugged": False, "instrumental": False, "acapella": False, "cover": False,
    "demo": False, "karaoke": False, "slowed": False, "sped up": False,
    "mashup": True, "dub": False, "re edit": True, "reedit": True, "remake": True,
}
_ACAPELLA = re.compile(r"\ba\s*capp?ella\b|\bacc?app?ella\b")
# The same recording in another cut or packaging. Kuba counts a remaster, a radio
# edit or an explicit/clean release as the same version, so these may differ in
# length from the Music file without making the match doubtful.
CUT = re.compile(
    r"\b(remaster(ed)?|radio (edit|mix|version)|album version|single version|"
    r"original (mix|version)|extended (mix|version)|club mix|explicit|clean)\b"
)
# Words that name nothing distinct: a bare "edit" or "version" says too little to
# excuse a different length, so these do not count as a cut.
NEUTRAL = re.compile(
    r"\b(mono|stereo|bonus( track)?|deluxe|edit|version|mix|prod(uced)? by .*|"
    r"from .*|soundtrack|ost|\d{4})\b"
)
# DJ annotations some files carry: Camelot keys ("10B") and BPM ("78.17").
_ANNOTATION = re.compile(r"^(\d{1,2}[ab]|\d{2,3}([.,]\d+)?( ?bpm)?)$")
_FEAT = re.compile(r"^(feat|featuring|ft|with)\b\.?\s*", re.I)
_INLINE_FEAT = re.compile(r"\s+(?:\(|\[)?(?:feat\.?|featuring|ft\.)\s+", re.I)
_BRACKETS = re.compile(r"\s*[\(\[\{]([^\)\]\}]*)[\)\]\}]")


@dataclass(frozen=True)
class Version:
    """A distinct-version marker, with who made it when that matters."""

    marker: str
    by: str = ""


@dataclass(frozen=True)
class ParsedTitle:
    core: str  # folded, without featured artists, versions or annotations
    featured: tuple[str, ...]
    versions: frozenset[Version]
    cut: bool  # mentions a radio edit, extended mix, remaster and the like
    search_words: str  # core plus the words that identify the version, for queries


_ARTIST_SEPARATORS = re.compile(r"\s*(?:,|&|\band\b|\bx\b|\bvs\b\.?|/|;|\+)\s*", re.I)


def _split_artists(text: str) -> list[str]:
    # Split before folding: folding removes the commas that separate names.
    return [f for f in (fold(p) for p in _ARTIST_SEPARATORS.split(text)) if f]


def _classify_segment(segment: str) -> tuple[list[Version], bool] | None:
    """Versions named in a bracket or dash segment; None when it names nothing."""
    if _ANNOTATION.match(segment.strip().lower()):
        return [], False
    folded = fold(segment)
    if not folded:
        return [], False
    folded = _ACAPELLA.sub("acapella", folded)
    versions = []
    for marker, named in DISTINCT.items():
        match = re.search(rf"\b{marker}\b", folded)
        if match:
            by = folded[: match.start()].strip() if named else ""
            by = re.sub(r"\b(s|official|unofficial|trap|dubstep|house|club)\b", " ", by)
            versions.append(Version(marker.replace("rmx", "remix"), _SPACES.sub(" ", by).strip()))
    if versions:
        return versions, False
    if CUT.search(folded):
        return [], True
    if NEUTRAL.search(folded):
        return [], False
    return None


def is_version_segment(segment: str) -> bool:
    """Whether a bracket or dash segment describes a version, a cut, featured
    artists or a DJ annotation rather than being part of the name."""
    return bool(_FEAT.match(segment.strip())) or _classify_segment(segment) is not None


def parse_title(title: str) -> ParsedTitle:
    featured: list[str] = []
    versions: list[Version] = []
    cut = False
    extra_words: list[str] = []

    def take(segment: str) -> bool:
        nonlocal cut
        if _FEAT.match(segment.strip()):
            featured.extend(_split_artists(_FEAT.sub("", segment.strip())))
            return True
        found = _classify_segment(segment)
        if found is None:
            return False
        found_versions, is_cut = found
        versions.extend(found_versions)
        cut = cut or is_cut
        if found_versions:
            extra_words.append(fold(segment))
        return True

    text = title
    # Bracketed segments: "(feat. X)", "[Original Mix]", "(PRVIM Trap Remix)".
    kept = []
    for segment in _BRACKETS.findall(text):
        if not take(segment):
            kept.append(segment)
    text = _BRACKETS.sub(" ", text)
    # Dash suffixes as Spotify writes them: "Song - Remastered 2011", "Song - X Remix".
    parts = [p for p in re.split(r"\s+-\s+", text) if p.strip()]
    while len(parts) > 1 and take(parts[-1]):
        parts.pop()
    text = " - ".join(parts)
    # Inline "Song feat. X" without brackets.
    inline = _INLINE_FEAT.split(text, maxsplit=1)
    if len(inline) == 2:
        text, guests = inline
        featured.extend(_split_artists(guests))
    core = fold(" ".join([text, *kept]))
    return ParsedTitle(
        core=core,
        featured=tuple(featured),
        versions=frozenset(versions),
        cut=cut,
        search_words=" ".join([core, *extra_words]).strip(),
    )


def split_artists(text: str) -> list[str]:
    """Individual artist names, folded, from a Music artist field."""
    base = _INLINE_FEAT.split(text, maxsplit=1)
    parts = _split_artists(base[0])
    if len(base) == 2:
        parts += _split_artists(base[1])
    return parts


def strip_the(name: str) -> str:
    return name[4:] if name.startswith("the ") else name


def versions_compatible(a: frozenset[Version], b: frozenset[Version]) -> bool:
    """Same recording as far as the titles tell: the same distinct markers, made by
    the same people (fuzzy, because remixers get spelled many ways)."""
    if {v.marker for v in a} != {v.marker for v in b}:
        return False
    for version in a:
        if not version.by:
            continue
        others = [o.by for o in b if o.marker == version.marker and o.by]
        if others and max(similarity(version.by, o) for o in others) < 0.6:
            return False
    return True
