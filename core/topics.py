"""
core/topics.py — Topic-boundary segmentation for clip selection.

A clip must be ONE complete idea: it starts where a self-contained thought
begins and ends where that thought resolves — right before a topic change, a
new question from the host, or the speaker moving to a different subject.  A
clip must NEVER end on the first sentence of a new topic; if a new subject has
only just been introduced at the tail, that subject belongs to the NEXT clip.

Public interface:
    segment_transcript(transcript, clip_len)                  -> list[dict]
    snap_end_off_next_topic(start, end, topics, spans, clip_len) -> (float, float)
    FEWSHOT_BOUNDARY_EXAMPLES                                  (str, for the ranker prompt)

The segmentation pass (segment_transcript) is an LLM call and is best-effort:
it returns [] on any error so the producer run never breaks.  The snap function
is a PURE deterministic guard that pulls a clip's end back off the opening of a
new topic even if the LLM's chosen end bled into it.

The few-shot examples are drawn from REAL campaign transcripts on the VPS
(peptides + fitness podcasts) — not invented — so the model learns the boundary
rule from the operator's own content.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transition / list-item / wrap-up / topic-opener patterns
# ---------------------------------------------------------------------------
# Canonical definition lives here; producer/boundary_check.py imports from here.
# Updated with B1 additions: 'another thing', 'so anyway', 'and just like that'.

TRANSITION_START_RE = re.compile(
    r"""^(?:
        Number\s+\d+            # "Number 16, CAX"
        | Number\s+[A-Z]        # "Number A", "Number B" (lettered list items)
        | Next\s+up             # "Next up"
        | The\s+next\s+one      # "The next one"
        | Now\s+again           # "Now again"
        | And\s+just\s+like\s+that  # "And just like that" (topic-end cue)
        | And\s+just\s+like     # "And just like semaglutide..." (mid-list pivot)
        | Also,                 # "Also,"
        | Oh,\s+and             # "Oh, and"
        | So\s+the\s+next       # "So the next"
        | Moving\s+on           # "Moving on"
        | Alright,?\s*next      # "Alright next" / "Alright, next"
        | Another\s+thing       # "Another thing" (B1 addition)
        | So\s+anyway           # "So anyway" (B1 addition)
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Wrap-up cues: when the CURRENT sentence starts with these, it is ending a unit.
# The NEXT sentence opens a new unit.
_WRAP_UP_RE = re.compile(
    r"^(?:so\s+that'?s\s+why|and\s+that'?s\s+the\s+point)\b",
    re.IGNORECASE,
)

# Topic-opening cues: these sentence starts signal the beginning of a new unit.
_TOPIC_OPENER_RE = re.compile(
    r"^(?:so\s+there'?s\s+this|the\s+other\s+thing\s+is)\b",
    re.IGNORECASE,
)

# Timestamps are linearly interpolated from segment-level transcripts, so exact
# equality never holds; compare boundaries with this slack (seconds).
_EPS = 0.75


# ---------------------------------------------------------------------------
# Few-shot worked examples — REAL transcripts (shown to the operator in the
# report so the boundaries can be confirmed).  Embedded into the ranker prompt.
# ---------------------------------------------------------------------------

FEWSHOT_BOUNDARY_EXAMPLES = """\
WORKED EXAMPLES OF CORRECT TOPIC BOUNDARIES (from real podcast transcripts):

── Example 1 — POSITIVE (a complete thought that resolves before a new question)
Source: "Peptide Expert: What Do Peptides Actually Do?" (Dr Alex Tatem)
Transcript around the moment:
  [1343.9] "...But now we have peptides in the form of GLP-1 drugs like
           semaglutide and tirzepatide."
  [1349.8] "And I just saw a patient last week who increased his sperm count
           10 times over and is now in a normal range because he's lost 100 lb
           due to using tirzepatide, exercising, and improving his diet. And he
           has totally changed his life."
  [1366.2] HOST: "And that started with a peptide?"  GUEST: "Started with a peptide."
  [1369.0] HOST: "So we've got lots of peptides on the table in front of you...
           can you give me a high-level view of the types of areas these peptides
           can help with?"
  [1391.8] GUEST: "The best way to think about it is this. Peptides are almost like
           an app on your phone. So imagine before we had apps..."
CORRECT CLIP: start = 1343.9, end = 1370.4 ("Started with a peptide.")
WHY IT ENDS THERE: the patient sperm-count / weight-loss story is one complete
idea and it lands on "Started with a peptide." The very next line is the host
asking a NEW question that opens a DIFFERENT topic (a high-level overview / the
"app on your phone" analogy). That overview is its own separate clip.

── Example 2 — NEGATIVE (the wrong cut) vs the corrected cut, same source
WRONG:   start = 1343.9, end = 1398.0
  The end bleeds past the resolution, through the host's new question, and into
  "Peptides are almost like an app on your phone. So imagine before we had apps..."
  — a NEW topic that has only JUST started. Ending here cuts off the new thought
  mid-introduction and dilutes the finished story. THIS IS THE FAILURE MODE.
CORRECTED: start = 1343.9, end = 1370.4 ("Started with a peptide.")
  Trim back to where the prior thought completed. The "app on your phone" overview
  becomes the START of a separate candidate clip.

── Example 3 — POSITIVE (a self-contained explainer that resolves before a sub-topic)
Source: "Benefits & Risks of Peptide Therapeutics" (Huberman Lab)
Transcript around the moment:
  [464.0] "So, what is a peptide? A peptide is a small protein made up of little
          chains of amino acids..."
  [497.4] "The basic way we define a peptide is that it tends to be a small
          protein — chains of anywhere from two to 50 amino acids..."
  [523.2] "...a peptide basically looks like beads on a string... the order of
          each amino acid along that string determines what the peptide is and
          what the peptide does."
  [546.7] "The other thing that's important to understand about peptides is that
          some peptides are hormones, others are neuromodulators..."
CORRECT CLIP: start = 464.0 ("So, what is a peptide?"), end = 546.7
  ("...determines what the peptide is and what the peptide does.")
WHY IT ENDS THERE: this is the complete "what is a peptide / beads on a string"
definition, and it resolves cleanly. The next sentence ("some peptides are
hormones...") opens a new sub-topic (peptide categories), which is a separate clip.

RULE TO FOLLOW: choose start on the strong opening line of a self-contained
thought; choose end where that thought RESOLVES — right before a host question,
a discourse cue ("so anyway", "moving on", "the next thing", "another thing"),
or a clear subject change. NEVER end on the first sentence of a new topic. Topic
completeness beats hitting a target length: a clean 35s thought is better than a
60s clip that starts a second topic.

── Example 4 — LIST ITEM END BLEED (Selank anxiety, numbered list context)
Transcript around the moment:
  [232.7] "...some people report having a lot less daily anxiety when they use it,
          but there's really mixed results. Some people have worse anxiety."
  [236.9] "I'm not sure that I would try this one again."    ← IDEA RESOLVES ~238.4s
  [238.4] "Number 16, CAX. This is kind of like taking Adderall..."  ← NEXT LIST ITEM
CORRECT CLIP END: 238.4 ("...I'm not sure that I would try this one again.")
WHY: The Selank discussion ends with the speaker's verdict. The very next sentence
is "Number 16, CAX" — it starts a NEW enumerated list item (a different peptide).
The clip must end BEFORE "Number 16" is spoken. Even a 0.5s bleed into the next
list item makes the clip feel incomplete and off-brand.
WRONG END: 238.9 — bleeds into "Number 16, CAX" (the next list item).

── Example 5 — HOOK/BODY MISMATCH (CJC-1295 hook, retatrutide body)
Transcript around the moment (497-556s):
  [497]  "...allodynia where their skin felt like it had been sunburned..."
  [505]  "...glucagon receptors on sensory neurons..."
  [521]  "...the risk for pancreatitis and gallstones..."
  [540]  "Now again, a lot of people are getting their hands on retatrutide..."
  [552]  "For retatrutide, in the trials, the doses were 2mg, 4, 6, 9, 12..."
Hook assigned to this span: "GH secretagogues like CJC-1295 and ipamorelin are
permissive anabolics."
WHY THIS IS WRONG: The hook promises content about CJC-1295 secretagogues. The
entire body at 497-556s is about retatrutide side effects (allodynia, pancreatitis,
gallstones) and dosing. These are unrelated subjects. This is NOT a trim problem —
the whole span is wrong. Do NOT select this span for a CJC-1295 hook. Find the
correct timestamp window where CJC-1295 is actually discussed.

── Example 6 — TOPICAL RELEVANCE FAIL (generic advice with one passing mention)
Hook: "racing heart on peptides"
Clip body (87s): "If you have a racing heart, the first thing to check is your
hydration. Drink more water, add electrolytes. Magnesium is the big one — most
people are deficient. Also make sure you're not over-stimulating your adrenals.
And as a quick disclaimer: I'm a doctor on YouTube, not your doctor. Always
consult your physician before starting anything. Oh, and if you're on retatrutide
just watch your heart rate during the first few weeks."
WHY THIS FAILS TOPICAL RELEVANCE: The hook promises specific information about
peptides causing a racing heart. The body is generic hydration/magnesium/disclaimer
advice with one passing mention of retatrutide at the end. The substance is NOT
campaign-specific content. This clip should be EXCLUDED.
"""


# ---------------------------------------------------------------------------
# Segmentation pass (LLM call — best effort)
# ---------------------------------------------------------------------------

def _extract_json_array(text: str) -> list | None:
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return None
    try:
        result = json.loads(match.group())
        return result if isinstance(result, list) else None
    except json.JSONDecodeError:
        return None


def _build_segmentation_prompt(transcript: list[dict]) -> str:
    seg_lines = "\n".join(
        f"[{float(seg['start']):.1f}s-{float(seg['end']):.1f}s] {seg['text']}"
        for seg in transcript
    )
    return f"""You are a transcript segmentation assistant. Split the transcript \
below into self-contained TOPIC UNITS. A topic unit is one coherent thread \
of thought — a single question-and-answer, a single explanation, or a single \
story — that begins when a subject is introduced and ends when that subject \
resolves and a DIFFERENT subject begins.

A new unit starts when ANY of these occurs:
- the host asks a new question or changes the subject ("let me ask you...",
  "what about...", "so we've got X on the table...");
- a discourse cue signals a move ("so anyway", "moving on", "the next thing",
  "another thing", "but here's the point");
- the speaker clearly shifts to a different subject (semantic change).

For each topic unit return its start second, its end second (the LAST word of
the resolving thought — NOT the first word of the next subject), a one-line
summary, a short note on what marks the boundary, and whether the unit is
COMPLETE (contains setup → development → resolution).

TRANSCRIPT (timestamps in seconds):
{seg_lines}

Return ONLY a JSON array (no prose, no code fences), chronological, covering the
whole transcript, in this exact shape:
[
  {{
    "start": <float seconds — first word of this topic unit>,
    "end": <float seconds — last word of the resolving thought>,
    "summary": "<one line: what this topic is about>",
    "boundary_reason": "<what marks the end boundary: 'host asks new question' / 'subject change to X' / 'wrap-up cue' / 'story resolves'>",
    "ends_because": "<same as boundary_reason — kept for compatibility>",
    "completeness": <true if the unit has setup+development+resolution; false if it starts/ends mid-thought>
  }}
]
"""


def segment_transcript(
    transcript: list[dict],
    clip_len: tuple[int, int] | None = None,
) -> list[dict]:
    """
    Split a transcript into self-contained topic segments via one LLM call.

    Args:
        transcript: [{start, end, text}] segment-level transcript.
        clip_len:   optional (min, max) — currently unused by the call itself,
                    accepted so callers can pass it uniformly.

    Returns:
        [{start, end, summary, ends_because}] sorted by start, validated.
        Returns [] on ANY error (missing SDK, missing keys, transport failure,
        unparseable response) — segmentation is a best-effort enrichment and
        must never break the producer run.
    """
    if not transcript:
        return []

    try:
        import anthropic  # type: ignore[import]
    except ImportError:
        log.warning("anthropic SDK missing; skipping topic segmentation")
        return []

    try:
        from core.settings import get_settings

        settings = get_settings()
        api_key, model = settings.require_llm()

        base_url = settings.llm_base_url
        if base_url is None and api_key.startswith("sk-or-"):
            base_url = "https://openrouter.ai/api"
        client = (
            anthropic.Anthropic(api_key=api_key, base_url=base_url)
            if base_url
            else anthropic.Anthropic(api_key=api_key)
        )

        prompt = _build_segmentation_prompt(transcript)
        message = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text if message.content else ""
        parsed = _extract_json_array(raw)
        if parsed is None:
            log.warning("Topic segmentation returned no JSON array; skipping")
            return []
        return _validate_topic_segments(parsed)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Topic segmentation failed (%s); continuing without it", exc)
        return []


def _validate_topic_segments(raw: list[Any]) -> list[dict]:
    """Keep only well-formed {start, end, ...} items with end > start; sort.

    Adds (B1):
      boundary_reason  — canonical alias for ends_because (both kept for back-compat).
      completeness     — bool: does the topic contain setup→development→resolution?
                         The LLM fills this in when prompted; falls back to True
                         (safe default) when not present in the response.
    """
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        ends_because = str(item.get("ends_because") or "")
        # boundary_reason is the canonical name; ends_because kept for back-compat
        boundary_reason = str(item.get("boundary_reason") or ends_because)
        # completeness: LLM may return bool or string; default True when absent
        raw_complete = item.get("completeness")
        if isinstance(raw_complete, bool):
            completeness = raw_complete
        elif isinstance(raw_complete, str):
            completeness = raw_complete.lower() not in ("false", "0", "no")
        else:
            completeness = True  # safe default
        out.append(
            {
                "start": start,
                "end": end,
                "summary": str(item.get("summary") or ""),
                "ends_because": ends_because,        # backward-compat
                "boundary_reason": boundary_reason,  # canonical (B1)
                "completeness": completeness,         # B1 addition
            }
        )
    out.sort(key=lambda s: s["start"])
    return out


# ---------------------------------------------------------------------------
# Pure snap guard — pull a clip's end back off the opening of a new topic
# ---------------------------------------------------------------------------

def _topic_index_at(topics: list[dict], t: float) -> int:
    """
    Index of the topic segment whose [start, end] contains *t*.
    Clamps: before the first topic → 0; after the last → last index.  When
    topics are contiguous and *t* sits exactly on a boundary, the EARLIER topic
    wins (so a resolving-word timestamp is treated as belonging to the topic it
    resolves, not the one that follows).
    """
    if t <= topics[0]["start"]:
        return 0
    for i, seg in enumerate(topics):
        if seg["start"] - _EPS <= t <= seg["end"] + _EPS:
            return i
        # In a gap between seg[i] and seg[i+1]: attribute to the earlier topic.
        if i + 1 < len(topics) and seg["end"] < t < topics[i + 1]["start"]:
            return i
    return len(topics) - 1


def _last_sentence_end_at_or_before(
    sentence_spans: list[dict],
    boundary: float,
    floor: float,
) -> float:
    """
    Return the largest sentence end that is <= boundary (+slack) and >= floor.
    Falls back to *boundary* itself when no sentence qualifies.
    """
    best: float | None = None
    for span in sentence_spans:
        e = float(span["end"])
        if e <= boundary + _EPS and e >= floor:
            if best is None or e > best:
                best = e
    return best if best is not None else boundary


def snap_end_off_next_topic(
    start: float,
    end: float,
    topics: list[dict],
    sentence_spans: list[dict],
    clip_len: tuple[int, int],
) -> tuple[float, float]:
    """
    If a clip's *end* has bled into a topic that begins AFTER the topic the clip
    started in, trim *end* back to the resolving edge of an earlier topic so the
    clip never ends on the first sentence of a new subject.

    Strategy: prefer the most conservative (single-topic) boundary — the end of
    the topic that contains *start* — and only fall through to a later topic
    boundary if the conservative one would make the clip shorter than clip_len
    min.  If NO topic boundary keeps the clip within its minimum length, the
    original end is returned unchanged (the segmenter was probably over-granular).

    Pure function. No-op (returns the original pair) when there are fewer than
    two topics or when start/end already sit within a single topic.
    """
    if not topics or len(topics) < 2:
        return start, end

    si = _topic_index_at(topics, start)
    ei = _topic_index_at(topics, end)
    if ei <= si:
        # End is within the same topic the clip started in (or earlier) — the
        # clip does not bleed into a new subject. Nothing to do.
        return start, end

    min_len = clip_len[0]

    # Walk topic boundaries from the most conservative (single idea) outward.
    for bi in range(si, ei):
        boundary = topics[bi]["end"]
        snapped = (
            _last_sentence_end_at_or_before(sentence_spans, boundary, floor=start)
            if sentence_spans
            else boundary
        )
        if snapped > start and (snapped - start) >= min_len:
            return start, snapped

    # No boundary respects the minimum length → leave the clip as-is rather than
    # over-trimming a coherent answer the segmenter split too finely.
    return start, end


# ---------------------------------------------------------------------------
# B1 — Deterministic unit-boundary detection from sentence spans
# ---------------------------------------------------------------------------

def detect_unit_boundaries(sentence_spans: list[dict]) -> list[int]:
    """Return sorted list of sentence indices where a NEW topic unit starts (B1).

    Index 0 always begins the first unit — it is NOT included in the return value.
    The caller uses these indices with :func:`build_units_from_boundaries` to
    construct unit dicts suitable for :func:`clip_within_unit`.

    Deterministic signals detected (in addition to the LLM's semantic pass):
    - A sentence starting with a list/transition marker
      (TRANSITION_START_RE from this module) → that sentence starts a new unit.
    - A sentence starting with a topic-opening cue (_TOPIC_OPENER_RE)
      → new unit starts here.
    - A sentence starting with a wrap-up cue (_WRAP_UP_RE)
      → the sentence FOLLOWING it starts a new unit.

    NOTE: a mid-transcript "?" does NOT create a unit boundary. Podcast speakers
    ask rhetorical/self-Q&A questions constantly ("Why does that matter?"), so
    splitting on every "?" shreds the transcript into 5-15s units and makes
    clip_within_unit over-trim good clips. Starting a clip on an interviewer
    question is prevented by is_bad_start_sentence instead; the LLM segmentation
    pass handles genuine Q→A topic shifts semantically.

    Args:
        sentence_spans: list[{"text", "start", "end"}] — from build_sentence_spans
                        or restore_sentences.

    Returns:
        Sorted list of sentence indices (0-based) where a new unit begins.
        Empty list when sentence_spans has fewer than 2 elements.
    """
    if len(sentence_spans) < 2:
        return []

    boundaries: set[int] = set()
    n = len(sentence_spans)

    for i in range(n):
        text = sentence_spans[i]["text"].strip()
        prev_text = sentence_spans[i - 1]["text"].strip() if i > 0 else ""

        # Sentence starts with list/transition marker → new unit here
        if i > 0 and TRANSITION_START_RE.match(text):
            boundaries.add(i)

        # Sentence starts with topic-opening cue → new unit here
        if i > 0 and _TOPIC_OPENER_RE.match(text):
            boundaries.add(i)

        # Previous sentence starts with wrap-up cue → current sentence is new unit
        if i > 0 and _WRAP_UP_RE.match(prev_text):
            boundaries.add(i)

    return sorted(boundaries)


def build_units_from_boundaries(
    sentence_spans: list[dict],
    boundary_indices: list[int],
) -> list[dict]:
    """Convert sentence-index boundary list into topic-unit dicts (B1 / B2).

    Each unit dict has the same shape as :func:`segment_transcript` output:
    {"start", "end"} — minimal shape used by :func:`clip_within_unit`.

    Args:
        sentence_spans:    list[{"text", "start", "end"}]
        boundary_indices:  output of detect_unit_boundaries — sorted indices
                           where new units begin (not including 0).

    Returns:
        list[{"start", "end"}] — one entry per unit, sorted by start time.
        Returns a single unit covering the whole transcript when boundary_indices
        is empty (or sentence_spans is empty).
    """
    if not sentence_spans:
        return []

    n = len(sentence_spans)
    starts = [0] + sorted(set(i for i in boundary_indices if 0 < i < n))

    units: list[dict] = []
    for k, s_idx in enumerate(starts):
        e_idx = starts[k + 1] - 1 if k + 1 < len(starts) else n - 1
        units.append(
            {
                "start": float(sentence_spans[s_idx]["start"]),
                "end": float(sentence_spans[e_idx]["end"]),
                "summary": "",
                "boundary_reason": "deterministic",
                "ends_because": "deterministic",
                "completeness": True,
            }
        )
    return units


# ---------------------------------------------------------------------------
# B2 — Deterministic guard: enforce clip lies within a single topic unit
# ---------------------------------------------------------------------------

def _first_sentence_start_at_or_after(
    sentence_spans: list[dict],
    t: float,
) -> float:
    """Return the start of the first sentence that begins at or after *t*.

    Falls back to t itself when no sentence qualifies.
    """
    for span in sentence_spans:
        s = float(span["start"])
        if s >= t - _EPS:
            return s
    return t


def clip_within_unit(
    candidate: dict,
    units: list[dict],
    sentence_spans: list[dict],
    clip_len: tuple[int, int] | None = None,
) -> dict:
    """Enforce that a clip lies entirely within ONE topic unit (B2).

    Pure deterministic guard:
    - If candidate end crosses into a LATER unit than the one containing start,
      the end is snapped back to the last sentence that ends within start's unit.
    - If candidate start falls before the unit's first sentence, it is moved up
      to the first sentence start of that unit.

    This is a PURE function: no side-effects, no I/O.

    Graceful no-op when:
    - units is empty (returns candidate unchanged).
    - sentence_spans is empty (returns candidate unchanged).
    - The clip already sits within one unit (returns candidate unchanged).
    - Snapping the end back would drop the clip BELOW clip_len[0] — the LLM's
      chosen boundary is kept rather than over-trimming a good clip to a stub
      (mirrors snap_end_off_next_topic; without this, over-fine deterministic
      units shred legitimate 40-60s clips — see reviewer 2026-07-13).

    Args:
        candidate:      Clip dict with "start" and "end" float keys.
        units:          list[{"start", "end"}] — from segment_transcript or
                        build_units_from_boundaries.
        sentence_spans: list[{"text", "start", "end"}].
        clip_len:       (min_seconds, max_seconds) — the end-snap is skipped
                        when it would produce a sub-min clip. None disables the
                        minimum-length guard.

    Returns:
        A new dict (or the original when no adjustment is needed).
    """
    if not units or not sentence_spans:
        return candidate

    start = float(candidate.get("start", 0))
    end = float(candidate.get("end", 0))
    min_len = float(clip_len[0]) if clip_len else 0.0

    si = _topic_index_at(units, start)
    ei = _topic_index_at(units, end)

    # ── Case 1: end bleeds into a later unit ──────────────────────────────────
    if ei > si:
        unit_end = float(units[si]["end"])
        new_end = _last_sentence_end_at_or_before(
            sentence_spans, unit_end, floor=start
        )
        # Only snap when it stays above the minimum clip length — never trim a
        # good clip down to a stub because the deterministic units were fine.
        if new_end > start and (new_end - start) >= min_len:
            log.debug(
                "clip_within_unit: end %.2f→%.2f (crossed from unit %d to %d; "
                "snapped back to unit %d boundary %.2f)",
                end, new_end, si, ei, si, unit_end,
            )
            candidate = {**candidate, "end": new_end}
            end = new_end
            # Recalculate ei in case the snap also changes the unit index
            ei = _topic_index_at(units, end)
        elif new_end > start:
            log.debug(
                "clip_within_unit: skipping end-snap %.2f→%.2f — would drop below "
                "min_len=%.1fs; keeping LLM boundary",
                end, new_end, min_len,
            )

    # ── Case 2: start is before the unit's opening sentence ──────────────────
    unit_start = float(units[si]["start"])
    if start < unit_start - _EPS:
        new_start = _first_sentence_start_at_or_after(sentence_spans, unit_start)
        if new_start < end:
            log.debug(
                "clip_within_unit: start %.2f→%.2f (before unit %d opening at %.2f)",
                start, new_start, si, unit_start,
            )
            candidate = {**candidate, "start": new_start}

    return candidate
