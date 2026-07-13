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
below into self-contained TOPIC segments. A topic segment is one coherent thread \
of thought — a single question-and-answer, a single explanation, or a single \
story — that begins when a subject is introduced and ends when that subject \
resolves and a DIFFERENT subject begins.

A new segment starts when ANY of these occurs:
- the host asks a new question or changes the subject ("let me ask you...",
  "what about...", "so we've got X on the table...");
- a discourse cue signals a move ("so anyway", "moving on", "the next thing",
  "another thing", "but here's the point");
- the speaker clearly shifts to a different subject (semantic change).

For each topic segment return its start second, its end second (the LAST word of
the resolving thought — NOT the first word of the next subject), a one-line
summary, and a short note on what marks the boundary at its end.

TRANSCRIPT (timestamps in seconds):
{seg_lines}

Return ONLY a JSON array (no prose, no code fences), chronological, covering the
whole transcript, in this exact shape:
[
  {{
    "start": <float seconds — first word of this topic>,
    "end": <float seconds — last word of the resolving thought>,
    "summary": "<one line: what this topic is about>",
    "ends_because": "<what marks the boundary: 'host asks new question' / 'subject change to X' / 'wrap-up cue' / 'story resolves'>"
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
    """Keep only well-formed {start, end, ...} items with end > start; sort."""
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
        out.append(
            {
                "start": start,
                "end": end,
                "summary": str(item.get("summary") or ""),
                "ends_because": str(item.get("ends_because") or ""),
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
