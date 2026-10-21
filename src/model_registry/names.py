"""Model-name normalization shared by catalog grouping and request parsing.

Spelling is only ever folded for comparison; a model's listed id is chosen by
the pipeline and is never rewritten by these helpers beyond lower-casing.
"""
from __future__ import annotations

import re

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_EFFORT_WORDS = {**{name: name for name in EFFORTS}, "extra-high": "xhigh"}
_THINKING_WORDS = {"thinking": "on", "nothinking": "off", "non-thinking": "off", "disabled": "off"}
_SPEED_WORDS = {"fast": "fast"}

_SEPARATORS = re.compile(r"[\s_]+")
_POINT_P = re.compile(r"(?<=\d)p(?=\d)")
_VERSION_SEP = re.compile(r"(?<=\d)[.-](?=\d)")
_TRAILING_TAG = re.compile(r"\(([^()]*)\)\s*$")
_SLUG_JUNK = re.compile(r"[^a-z0-9.-]+")


def fold(name: str) -> str:
    """Lower-case spelling with uniform separators: ``GLM 5p3_Flash`` -> ``glm-5.3-flash``."""
    text = _SEPARATORS.sub("-", str(name or "").strip().casefold())
    return _POINT_P.sub(".", text).strip("-")


def compare_key(name: str) -> str:
    """Identity used only for matching: ``claude-opus-4-6`` == ``Claude Opus 4.6``."""
    return _VERSION_SEP.sub(".", fold(name))


def slug(name: str) -> str:
    return re.sub(r"-{2,}", "-", _SLUG_JUNK.sub("-", fold(name))).strip("-.")


def display_traits(name: str) -> tuple[str, dict[str, str]]:
    """Split a display name's trailing ``(High)`` / ``(Thinking)`` into traits.

    An unknown tag stays part of the name, so ``(Preview)`` never merges a
    preview into its release.
    """
    text = str(name or "").strip()
    match = _TRAILING_TAG.search(text)
    if not match:
        return fold(text), {}
    traits = _trait_of(fold(match.group(1)))
    if not traits:
        return fold(text[:match.start()] + " " + match.group(1)), {}
    return fold(text[:match.start()]), traits


def plain_display(name: str) -> str:
    """The display name as written, minus a trailing trait tag: ``Gemini 3.8 Flash (High)`` -> ``Gemini 3.8 Flash``."""
    text = str(name or "").strip()
    match = _TRAILING_TAG.search(text)
    if match and _trait_of(fold(match.group(1))):
        return text[:match.start()].strip()
    return text


def suffix_traits(name: str) -> tuple[str, dict[str, str]]:
    """Strip trailing effort / thinking / speed words: ``gemini-4-argon-high`` -> (``gemini-4-argon``, effort=high).

    Each kind is taken at most once, outermost first; callers decide whether
    the stripped base is a real model before trusting the split.
    """
    base = fold(name)
    traits: dict[str, str] = {}
    while True:
        for word in sorted({**_EFFORT_WORDS, **_THINKING_WORDS, **_SPEED_WORDS}, key=len, reverse=True):
            if not base.endswith("-" + word):
                continue
            trait = _trait_of(word)
            kind = next(iter(trait))
            if kind in traits:
                continue
            candidate = base[:-len(word) - 1]
            if not candidate:
                continue
            traits.update(trait)
            base = candidate
            break
        else:
            return base, traits


def _trait_of(word: str) -> dict[str, str]:
    if word in _EFFORT_WORDS:
        return {"effort": _EFFORT_WORDS[word]}
    if word in _THINKING_WORDS:
        return {"thinking": _THINKING_WORDS[word]}
    if word in _SPEED_WORDS:
        return {"speed": _SPEED_WORDS[word]}
    return {}


def effort_rank(effort: str) -> int:
    return EFFORTS.index(effort) if effort in EFFORTS else -1


def nearest_effort(wanted: str, available) -> str | None:
    """The supported level closest to ``wanted``, preferring lower on a tie."""
    levels = [level for level in available if level in EFFORTS]
    if not levels:
        return None
    if wanted in levels:
        return wanted
    target = effort_rank(wanted)
    lower = [level for level in levels if effort_rank(level) < target]
    if lower:
        return max(lower, key=effort_rank)
    return min(levels, key=effort_rank)
