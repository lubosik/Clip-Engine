"""
core/hook_style.py — strategic-capitalisation enforcement for hooks.

Implements the mechanical rules of docs/HOOK_CAPITALISATION.md ("this file is
law"): every LLM-generated hook is audited and deterministically repaired
before it reaches a Clip row or the render.

The semantic side (WHICH word deserves the cap — action / outcome / pivot) is
the generation prompt's job (core/llm.py). This module guarantees the hard
constraints hold no matter what the model returns:

  - no em dashes or en dashes anywhere (rule 4.9)
  - never a fully capitalised line (rule 4.6)
  - max 2 strategic caps (3 only when the hook exceeds 10 words) (rule 4.1)
  - cap ratio <= 20% when 2+ caps are used (rule 4.3)
  - never two adjacent capitalised words (rule 4.4)
  - connectives never hold a cap (rule 3)
  - acronyms / technical tokens (FDA, TRT, GLP-1, BPC-157) keep house casing
    and are invisible to the cap budget (rule 3)
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Word classes
# ---------------------------------------------------------------------------

# Connective tissue — words that NEVER earn a cap (HOOK_CAPITALISATION.md §3).
CONNECTIVES = {
    # articles
    "a", "an", "the",
    # prepositions
    "to", "for", "in", "on", "with", "from", "at", "of", "by", "into", "over",
    # conjunctions
    "and", "but", "or", "so", "if", "as",
    # auxiliaries
    "is", "are", "was", "were", "will", "can", "do", "does", "did", "be",
    "been", "has", "have", "had", "would", "should", "could",
    # pronouns
    "i", "you", "my", "your", "this", "that", "it", "we", "our", "his", "her",
    "they", "their", "he", "she", "me", "us", "its",
    # filler adjectives with no emotional charge
    "good", "nice", "great", "useful", "better", "very", "really", "just",
}

# Acronyms / technical tokens that legitimately appear in ALL CAPS and must
# never be demoted or counted against the cap budget. Tokens containing a
# digit (GLP-1, BPC-157, CJC-1295) are auto-detected; this set covers the
# letter-only ones common in our niches. Campaigns can extend via the
# `protected_tokens` argument.
ACRONYMS = {
    "fda", "trt", "hrt", "hgh", "glp", "rfk", "ceo", "cia", "fbi", "usa",
    "uk", "us", "eu", "un", "ai", "dna", "rna", "iq", "bmi", "ped", "sarm",
    "nad", "atp", "gh", "igf", "hcg", "mots", "nih", "who", "tv", "diy",
    "ufc", "nfl", "nba", "mlb", "gym", "mpmd", "tb", "ghk", "cu", "asap",
    "og", "aka", "faq", "gpt", "llm", "pr", "roi", "cta",
}

_EM_DASH_RE = re.compile(r"[—–―]")  # em dash, en dash, horizontal bar
_WORD_ALPHA_RE = re.compile(r"[A-Za-z]")
_SENTENCE_END_RE = re.compile(r"[.!?]$")


def _strip_punct(word: str) -> str:
    """Return the word without leading/trailing punctuation."""
    return word.strip(".,!?;:()[]\"'`")


def _has_digit(word: str) -> bool:
    return any(ch.isdigit() for ch in word)


def _is_protected(word: str, protected: set[str]) -> bool:
    """Acronym / brand / numeric-technical token — invisible to cap budget."""
    core = _strip_punct(word)
    if not core:
        return True
    if _has_digit(core):  # GLP-1, BPC-157, 10x, 2023
        return True
    return core.lower() in protected


def _is_cap_word(word: str) -> bool:
    """A word rendered as a strategic cap: 2+ letters, all letters uppercase."""
    core = _strip_punct(word)
    letters = [ch for ch in core if ch.isalpha()]
    if len(letters) < 2:
        return False
    return all(ch.isupper() for ch in letters)


def _demote(word: str, sentence_start: bool) -> str:
    """Lowercase a capitalised word, keeping sentence-initial capitalisation."""

    def transform(core: str) -> str:
        low = core.lower()
        return (low[:1].upper() + low[1:]) if sentence_start else low

    core = _strip_punct(word)
    if not core:
        return word
    start = word.find(core)
    return word[:start] + transform(core) + word[start + len(core):]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def audit_hook(hook: str, protected_tokens: tuple[str, ...] = ()) -> dict:
    """Audit a hook against the mechanical HOOK_CAPITALISATION.md constraints.

    Returns a dict with cap metrics and a `violations` list (empty = clean).
    Semantic checks (strip test, word roles) are not automatable and live in
    the generation prompt.
    """
    protected = ACRONYMS | {t.lower() for t in protected_tokens}
    words = hook.split()
    word_count = len(words)

    cap_indices = [
        i for i, w in enumerate(words)
        if _is_cap_word(w) and not _is_protected(w, protected)
    ]
    cap_words = [_strip_punct(words[i]) for i in cap_indices]
    cap_count = len(cap_indices)
    cap_ratio = (cap_count / word_count) if word_count else 0.0

    adjacent = any(b - a == 1 for a, b in zip(cap_indices, cap_indices[1:]))
    em_dash = bool(_EM_DASH_RE.search(hook))

    # A fully capitalised line: every countable (non-protected, 2+ letter)
    # word is upper.
    countable = [
        w for w in words
        if len([c for c in _strip_punct(w) if c.isalpha()]) >= 2
        and not _is_protected(w, protected)
    ]
    all_caps_line = bool(countable) and all(_is_cap_word(w) for w in countable)

    max_caps = 3 if word_count > 10 else 2

    violations: list[str] = []
    if em_dash:
        violations.append("em_dash_present")
    if all_caps_line:
        violations.append("all_caps_line")
    if cap_count > max_caps:
        violations.append("too_many_caps")
    if cap_count >= 2 and cap_ratio > 0.20:
        violations.append("cap_ratio_exceeded")
    if adjacent:
        violations.append("adjacent_caps")
    for i in cap_indices:
        if _strip_punct(words[i]).lower() in CONNECTIVES:
            violations.append("connective_capitalised")
            break

    return {
        "hook": hook,
        "word_count": word_count,
        "cap_count": cap_count,
        "cap_ratio": round(cap_ratio, 4),
        "caps": cap_words,
        "adjacent_caps": adjacent,
        "em_dash_present": em_dash,
        "all_caps_line": all_caps_line,
        "violations": violations,
    }


def sanitize_hook(hook: str, protected_tokens: tuple[str, ...] = ()) -> str:
    """Deterministically repair a hook so it passes audit_hook().

    Repairs, in order:
      1. em/en dashes -> full stop (spaced) or comma (tight)  [rule 4.9]
      2. fully capitalised line -> sentence case, zero caps    [rule 4.6]
      3. capitalised connectives -> lowercase                  [rule 3]
      4. adjacent caps -> keep the first of each run           [rule 4.4]
      5. too many caps -> keep first + last                    [rule 4.1]
      6. ratio > 20% with 2 caps -> keep the stronger position [rule 4.3]

    Never touches acronyms, digit-bearing tokens (GLP-1, BPC-157) or
    campaign-protected brand tokens.
    """
    if not hook or not hook.strip():
        return hook

    protected = ACRONYMS | {t.lower() for t in protected_tokens}

    # 1. Em/en dashes: spaced dash reads as a hard break -> ". "; tight dash
    #    (word—word) reads as a soft join -> ", ". Capitalise after a stop.
    text = re.sub(r"\s+[—–―]+\s+", ". ", hook)
    text = _EM_DASH_RE.sub(", ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    # Capitalise the letter following a full stop we may have introduced.
    text = re.sub(
        r"([.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text
    )

    words = text.split()
    if not words:
        return text

    def sentence_start(idx: int) -> bool:
        return idx == 0 or bool(_SENTENCE_END_RE.search(_strip_punct(words[idx - 1]) or words[idx - 1]))

    # 2. Fully capitalised line -> sentence case (zero strategic caps is the
    #    honest fallback; rule 4.2 allows deliberate zero, and one wrong cap
    #    is worse than none).
    countable = [
        w for w in words
        if len([c for c in _strip_punct(w) if c.isalpha()]) >= 2
        and not _is_protected(w, protected)
    ]
    if countable and all(_is_cap_word(w) for w in countable):
        words = [
            w if _is_protected(w, protected) else _demote(w, sentence_start(i))
            for i, w in enumerate(words)
        ]

    # 3. Capitalised connectives -> demote unconditionally.
    for i, w in enumerate(words):
        if (
            _is_cap_word(w)
            and not _is_protected(w, protected)
            and _strip_punct(w).lower() in CONNECTIVES
        ):
            words[i] = _demote(w, sentence_start(i))

    # Recompute strategic cap positions.
    def cap_positions() -> list[int]:
        return [
            i for i, w in enumerate(words)
            if _is_cap_word(w) and not _is_protected(w, protected)
        ]

    # 4. Adjacent caps: keep the first of each adjacent run.
    caps = cap_positions()
    for a, b in zip(caps, caps[1:]):
        if b - a == 1 and _is_cap_word(words[b]):
            words[b] = _demote(words[b], sentence_start(b))

    # 5. Cap budget: keep the first and the last cap (front + exit fixation
    #    points per §6); demote everything between.
    caps = cap_positions()
    max_caps = 3 if len(words) > 10 else 2
    if len(caps) > max_caps:
        keep = {caps[0], caps[-1]} if max_caps >= 2 else {caps[0]}
        for i in caps:
            if i not in keep:
                words[i] = _demote(words[i], sentence_start(i))

    # 6. Ratio guard (only when 2+ caps): drop down to one cap, preferring to
    #    keep the cap that is NOT on the first word (rule 6.1 — the first word
    #    is already high-salience).
    caps = cap_positions()
    if len(caps) >= 2 and len(caps) / len(words) > 0.20:
        keep_idx = caps[-1] if caps[0] == 0 else caps[0]
        for i in caps:
            if i != keep_idx:
                words[i] = _demote(words[i], sentence_start(i))

    return " ".join(words)


def enforce_hook_style(hook: str, protected_tokens: tuple[str, ...] = ()) -> str:
    """Sanitize + return the hook. Convenience wrapper used by core/llm.py."""
    return sanitize_hook(hook, protected_tokens)
