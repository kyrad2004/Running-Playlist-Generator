"""Sourcing BPM for a track, and knowing how much to trust it.

Spotify removed `external_ids` in February 2026, taking ISRC with it — the clean
join key to any music database. Every lookup now goes through artist and title
strings, which are messy in ways that break naive matching:

    "Blinding Lights - Remastered 2021"   vs   "Blinding Lights"
    "Levitating (feat. DaBaby)"           vs   "Levitating"
    "Mr. Brightside" by a covers band     vs   The Killers' original

The last is the dangerous one. A confident match to the wrong recording yields a
wrong BPM, and a wrong BPM is worse than a missing one: it puts a track in the
playlist at the wrong tempo and nothing surfaces the error until you're running
to it. So "no confident match" is a first-class result here, never an exception.

No concrete provider ships yet — see docs/PHASE0_FINDINGS.md for the candidates.
Everything downstream depends on the protocol, so adding one is a single file.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Protocol

# Suffixes Spotify appends after a dash. Order matters only for readability;
# each is tried against the whole trailing segment.
_DASH_SUFFIXES = (
    r"remaster(ed)?(\s+\d{4})?",
    r"\d{4}\s+remaster(ed)?",
    r"radio\s+edit",
    r"single\s+version",
    r"album\s+version",
    r"extended(\s+(mix|version))?",
    r"bonus\s+track",
    r"deluxe(\s+edition)?",
    r"mono|stereo",
    r"live(\s+.*)?",
    r".*\s+remix",
    r"acoustic(\s+version)?",
    r"instrumental",
    r"sped\s+up|slowed(\s+\+\s+reverb)?",
)

_PAREN_PATTERNS = (
    r"\(\s*(feat|ft|featuring)\.?\s+[^)]*\)",
    r"\[\s*(feat|ft|featuring)\.?\s+[^\]]*\]",
    r"\(\s*(remaster(ed)?|radio\s+edit|single\s+version|album\s+version|"
    r"bonus\s+track|deluxe|mono|stereo|instrumental|acoustic)[^)]*\)",
    r"\(\s*live[^)]*\)",
    r"\(\s*[^)]*remix\s*\)",
)

# Markers that mean the recording genuinely differs from the studio original —
# often at a different tempo. Stripping them helps matching but must lower
# confidence, or a live cut silently inherits the studio BPM.
_VARIANT_MARKERS = (
    ("live", r"\blive\b"),
    ("remix", r"\bremix\b"),
    ("acoustic", r"\bacoustic\b"),
    ("instrumental", r"\binstrumental\b"),
    ("sped_up", r"\bsped\s+up\b"),
    ("slowed", r"\bslowed\b"),
)

CONFIDENT = 0.80
PLAUSIBLE = 0.60


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def normalize(text: str) -> str:
    """Casefold, drop accents and punctuation, collapse whitespace."""
    text = _strip_accents(text).lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def variant_markers(title: str) -> list[str]:
    """Which 'this is a different recording' markers appear in the raw title."""
    lowered = title.lower()
    return [name for name, pattern in _VARIANT_MARKERS if re.search(pattern, lowered)]


def clean_title(title: str) -> str:
    """Strip Spotify's decorations, leaving the underlying song title."""
    cleaned = title
    for pattern in _PAREN_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    # Trailing " - Something" segments, applied repeatedly for stacked suffixes
    # like "Song - Live - Remastered 2011".
    changed = True
    while changed:
        changed = False
        for suffix in _DASH_SUFFIXES:
            new = re.sub(rf"\s+-\s+{suffix}\s*$", "", cleaned, flags=re.IGNORECASE)
            if new != cleaned:
                cleaned, changed = new, True
    return normalize(cleaned)


def primary_artist(artist: str) -> str:
    """The lead artist only — features and collaborators are dropped.

    Databases index under the lead artist, and a track credited to three people
    won't match a record credited to one unless the extras are removed.
    """
    # The trailing \b must sit before the optional period, not after it: in
    # "feat. DaBaby" the period is followed by a space, and two non-word
    # characters in a row give no boundary to anchor to.
    split = re.split(
        r"\s*(?:,|&|\b(?:featuring|feat|ft)\b\.?|\bwith\b|\bx\b)\s+",
        artist,
        maxsplit=1,
        flags=re.IGNORECASE,
    )
    return normalize(split[0])


@dataclass(frozen=True)
class TrackQuery:
    """A normalized lookup key, plus what normalizing had to throw away."""

    artist: str
    title: str
    raw_artist: str = ""
    raw_title: str = ""
    variants: tuple[str, ...] = ()

    @property
    def cache_key(self) -> str:
        return f"{self.artist}|{self.title}"

    @property
    def is_variant(self) -> bool:
        """True when the recording differs from the studio original."""
        return bool(self.variants)


def build_query(artist: str, title: str) -> TrackQuery:
    return TrackQuery(
        artist=primary_artist(artist),
        title=clean_title(title),
        raw_artist=artist,
        raw_title=title,
        variants=tuple(variant_markers(title)),
    )


def query_from_spotify_track(track: dict) -> TrackQuery:
    """Build a query from a Spotify track object."""
    artists = track.get("artists") or []
    artist = artists[0].get("name", "") if artists else ""
    return build_query(artist, track.get("name", ""))


@dataclass
class BpmResult:
    """A BPM with its provenance and how much it should be trusted."""

    bpm: float
    source: str
    confidence: float = 1.0
    matched_artist: str = ""
    matched_title: str = ""
    notes: tuple[str, ...] = ()

    @property
    def is_confident(self) -> bool:
        return self.confidence >= CONFIDENT

    @property
    def is_usable(self) -> bool:
        return self.confidence >= PLAUSIBLE

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BpmResult":
        data = dict(data)
        data["notes"] = tuple(data.get("notes", ()))
        return cls(**data)


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def score_match(query: TrackQuery, matched_artist: str, matched_title: str) -> tuple[float, list[str]]:
    """How well a provider's answer matches what was asked for.

    Artist agreement is weighted hardest: the same title by a different artist
    is a cover, and covers are routinely at a different tempo.
    """
    notes: list[str] = []
    artist_score = similarity(query.artist, normalize(matched_artist))
    title_score = similarity(query.title, normalize(matched_title))

    if artist_score < 0.6:
        notes.append(f"artist mismatch ({query.artist!r} vs {normalize(matched_artist)!r})")
    if title_score < 0.8:
        notes.append(f"title differs ({query.title!r} vs {normalize(matched_title)!r})")

    confidence = 0.65 * artist_score + 0.35 * title_score

    # A live or remixed cut matched against a studio record is a real tempo risk
    # even when the strings agree perfectly.
    if query.is_variant:
        confidence *= 0.75
        notes.append(f"variant recording ({', '.join(query.variants)})")

    return min(confidence, 1.0), notes


class BpmProvider(Protocol):
    """Anything that can look up a track's BPM.

    Return None rather than guessing when there's no confident match.
    """

    name: str

    def bpm_for(self, query: TrackQuery) -> BpmResult | None: ...


@dataclass
class StubProvider:
    """An in-memory provider for tests and for exercising the pipeline before a
    real source is chosen. Seeded with `{(artist, title): bpm}`."""

    catalogue: dict[tuple[str, str], float] = field(default_factory=dict)
    name: str = "stub"

    def bpm_for(self, query: TrackQuery) -> BpmResult | None:
        for (artist, title), bpm in self.catalogue.items():
            confidence, notes = score_match(query, artist, title)
            if confidence >= PLAUSIBLE:
                return BpmResult(
                    bpm=bpm,
                    source=self.name,
                    confidence=confidence,
                    matched_artist=artist,
                    matched_title=title,
                    notes=tuple(notes),
                )
        return None


class CachingProvider:
    """Wraps a provider so each track is looked up at most once, ever.

    Misses are cached too — a track no database knows about stays unknown, and
    re-asking wastes rate limit on every run.

    JSON is deliberate for now: this is derived data, and Phase 1 owns the real
    storage decision.
    """

    def __init__(self, provider: BpmProvider, path: Path) -> None:
        self.provider = provider
        self.path = path
        self.name = f"cached:{provider.name}"
        self._cache: dict[str, dict | None] = {}
        self.hits = 0
        self.misses = 0
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._cache = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self._cache = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._cache, indent=2, sort_keys=True))

    def bpm_for(self, query: TrackQuery) -> BpmResult | None:
        key = query.cache_key
        if key in self._cache:
            self.hits += 1
            payload = self._cache[key]
            return BpmResult.from_dict(payload) if payload else None

        self.misses += 1
        result = self.provider.bpm_for(query)
        self._cache[key] = result.to_dict() if result else None
        return result


def lookup_all(
    provider: BpmProvider, queries: Iterable[TrackQuery]
) -> list[tuple[TrackQuery, BpmResult | None]]:
    return [(q, provider.bpm_for(q)) for q in queries]
