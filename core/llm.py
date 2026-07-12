"""
core/llm.py — Anthropic Messages API client for clip ranking.

Public interface (per ARCHITECTURE §4):
    rank_moments(transcript, rules, comment_summary, clip_len, max_clips) -> list[dict]

The anthropic SDK is imported lazily so that importing this module never
fails in test environments where it is not installed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from core.sentences import build_sentence_spans, snap_to_sentences
from core.settings import get_settings
from core.topics import (
    FEWSHOT_BOUNDARY_EXAMPLES,
    _validate_topic_segments,
    snap_end_off_next_topic,
)

log = logging.getLogger(__name__)

# ~12s LLM-prompt chunks: the discovery actor emits 2-4s transcript fragments;
# merging them strips thousands of redundant timestamp markers per long podcast
# with no word loss. Boundary snapping still uses the full-resolution transcript.
_PROMPT_CHUNK_SECONDS = 12.0


def create_completion(client: Any, model: str, max_tokens: int, messages: list) -> Any:
    """
    Call messages.create with extended thinking DISABLED.

    Ranking, segmentation and the review gate are structured-extraction tasks
    that don't benefit from extended thinking — and on thinking-on-by-default
    models (Sonnet 5, Opus 4.8, Fable 5) leaving it on burns the whole
    max_tokens budget on reasoning (empty/no JSON, higher cost). Disabling it
    keeps those models fast and cheap. Models that reject the `thinking`
    parameter fall back to a plain call.
    """
    try:
        return client.messages.create(
            model=model,
            max_tokens=max_tokens,
            thinking={"type": "disabled"},
            messages=messages,
        )
    except Exception as exc:  # noqa: BLE001 - only retry the thinking-param case
        if "thinking" in str(exc).lower():
            return client.messages.create(
                model=model, max_tokens=max_tokens, messages=messages
            )
        raise


def extract_text(message: Any) -> str:
    """
    Return the first text block's text from an Anthropic Messages response.

    Thinking-on models (Sonnet 5, Opus 4.8, Fable 5, …) put a ThinkingBlock at
    content[0], so `content[0].text` raises AttributeError. Scan for the first
    block that actually carries text instead of assuming position 0.
    """
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return block.text
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    return ""


def _extract_json_array(text: str) -> list[dict] | None:
    """
    Extract the first JSON array from a string, even if surrounded by prose.
    Returns None if no valid array is found.
    """
    # Try to find a JSON array (greedy match of [...])
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return None
    try:
        result = json.loads(match.group())
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass
    return None


def _compress_transcript(
    transcript: list[dict], target_s: float = _PROMPT_CHUNK_SECONDS
) -> list[dict]:
    """
    Merge consecutive transcript segments into ~target_s chunks for the prompt.

    Reduces input-token cost on long transcripts by collapsing the per-fragment
    ``[x.xs-y.ys]`` markers. Text is preserved verbatim (space-joined); only the
    marker granularity is coarsened. Returns [{start, end, text}].
    """
    out: list[dict] = []
    cur: dict | None = None
    for seg in transcript:
        try:
            s = float(seg["start"])
            e = float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if cur is None:
            cur = {"start": s, "end": e, "text": text}
        else:
            cur["text"] += " " + text
            cur["end"] = e
        if cur["end"] - cur["start"] >= target_s:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return out


def _parse_ranking_response(text: str) -> tuple[list, list]:
    """
    Parse the combined ranking response into (clips, topics).

    Preferred shape is a JSON object ``{"topics": [...], "clips": [...]}``.
    Falls back to a bare JSON array (legacy / model slip) treated as clips with
    no topics. Returns ([], []) when nothing parseable is found.
    """
    obj_match = re.search(r"\{.*\}", text, re.DOTALL)
    if obj_match:
        try:
            obj = json.loads(obj_match.group())
            if isinstance(obj, dict) and ("clips" in obj or "topics" in obj):
                clips = obj.get("clips") or []
                topics = obj.get("topics") or []
                if isinstance(clips, list):
                    return clips, (topics if isinstance(topics, list) else [])
        except json.JSONDecodeError:
            pass
    # Fallback: a bare array of clips.
    arr = _extract_json_array(text)
    if arr is not None:
        return arr, []
    return [], []


def _validate_moments(
    raw: list[Any],
    clip_len: tuple[int, int],
    sentence_spans: list[dict] | None = None,
) -> list[dict]:
    """
    Validate and normalise raw LLM output items.

    Supports two output shapes from the model:

    1. Sentence-index mode (new, when sentence_spans is provided):
       The LLM returns ``{"start_sentence": int, "end_sentence": int, ...}``.
       Indices are clamped to the spans list, then converted to float
       start/end timestamps before returning.

    2. Float-time mode (legacy / backwards-compat fallback):
       The LLM returns ``{"start": float, "end": float, ...}``.
       Handled exactly as before — no behaviour change.

    Returns only items with required fields and plausible values.
    """
    validated = []
    min_len, max_len = clip_len
    n_spans = len(sentence_spans) if sentence_spans else 0

    for item in raw:
        if not isinstance(item, dict):
            log.warning("Skipping non-dict moment item", extra={"item": item})
            continue
        try:
            # ── Resolve start / end from sentence indices or float times ───
            if (
                sentence_spans
                and "start_sentence" in item
                and "end_sentence" in item
            ):
                si = int(item["start_sentence"])
                ei = int(item["end_sentence"])
                # Clamp to valid span range
                si = max(0, min(si, n_spans - 1))
                ei = max(si, min(ei, n_spans - 1))
                start = float(sentence_spans[si]["start"])
                end = float(sentence_spans[ei]["end"])
            else:
                start = float(item["start"])
                end = float(item["end"])

            score = float(item["score"])
            hook = str(item.get("hook") or "")
            reason = str(item.get("reason") or "")

            # Enforce HOOK_CAPITALISATION.md mechanically: strip em dashes,
            # cap the strategic-caps budget, demote connectives/all-caps lines.
            if hook:
                from core.hook_style import enforce_hook_style
                styled = enforce_hook_style(hook)
                if styled != hook:
                    log.info(
                        "Hook style repaired",
                        extra={"before": hook, "after": styled},
                    )
                    hook = styled
        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "Skipping malformed moment item",
                extra={"error": str(exc), "item": item},
            )
            continue

        duration = end - start
        if duration < min_len:
            log.debug(
                "Skipping moment shorter than clip_len min",
                extra={"start": start, "end": end, "duration": duration, "min": min_len},
            )
            continue
        if duration > max_len:
            log.debug(
                "Skipping moment longer than clip_len max",
                extra={"start": start, "end": end, "duration": duration, "max": max_len},
            )
            continue
        if not (0.0 <= score <= 1.0):
            log.warning(
                "Score out of [0,1] range; clamping",
                extra={"score": score},
            )
            score = max(0.0, min(1.0, score))

        validated.append(
            {"start": start, "end": end, "score": score, "hook": hook, "reason": reason}
        )

    return validated


def _build_prompt(
    transcript: list[dict],
    rules: str,
    comment_summary: str | None,
    clip_len: tuple[int, int],
    max_clips: int,
    preference_context: str = "",
    sentence_spans: list[dict] | None = None,
    stance: str = "",
) -> str:
    """Build the combined segmentation + ranking prompt.

    ONE call does both jobs: it first splits the transcript into self-contained
    topic segments, then selects clip candidates whose boundaries align to those
    segments. This avoids a second full-transcript LLM call per source.

    preference_context: optional learned-preference block injected AFTER the
    campaign ranking rules and BEFORE the sentence-boundary rules.

    sentence_spans: when provided (punctuation-restored spans), the prompt
    presents NUMBERED sentences and asks for sentence-index selection instead of
    raw float times.  When absent the prompt and output format are unchanged
    (regression-critical).

    stance: campaign stance string (R4).  When non-empty, a MANDATORY STANCE RULE
    block is injected after the campaign rules instructing the model to score 0 /
    exclude moments that contradict the stance.
    """
    comment_section = ""
    if comment_summary:
        comment_section = f"\n\nCOMMENT SIGNAL (top recurring themes from audience):\n{comment_summary}"

    # Preference block: injected after campaign rules, before sentence/topic rules.
    pref_section = ""
    if preference_context and preference_context.strip():
        pref_section = f"\n\n{preference_context.strip()}"

    # Stance block (R4): injected after campaign rules and before preferences.
    stance_section = ""
    if stance and stance.strip():
        stance_section = (
            f"\n\nMANDATORY STANCE RULE (R4 — enforce before all other criteria): "
            f"{stance.strip()} "
            "Any moment whose framing contradicts this stance MUST be scored 0 and "
            "excluded from clips — do NOT include such moments even if they are "
            "otherwise high-quality."
        )

    if sentence_spans:
        # ── Sentence-INDEX mode (Spotify pattern, §R2.2) ─────────────────────
        # Present numbered sentences with timestamps; ask for index-based selection.
        span_lines = "\n".join(
            f"[{i}] [{span['start']:.1f}s-{span['end']:.1f}s] {span['text']}"
            for i, span in enumerate(sentence_spans)
        )
        n_spans = len(sentence_spans)

        return f"""You are a clip ranking assistant. Analyse the numbered sentence list below and identify the best moments to cut as short-form clips.

CAMPAIGN RANKING RULES:
{rules.strip()}{stance_section}

CLIP LENGTH CONSTRAINTS: minimum {clip_len[0]}s, maximum {clip_len[1]}s
MAXIMUM CLIPS TO RETURN: {max_clips}{comment_section}{pref_section}

SENTENCE-BOUNDARY RULE (mandatory): Every clip must start at the FIRST word of a \
sentence and end at the LAST word of a sentence — use the sentence INDEX numbers \
shown below. The clip must be a complete, coherent thought. The hook must describe \
the point made in the opening sentence.

HOOK STYLE RULES (mandatory — the hook is burned on screen over the first \
seconds of the clip; a viewer decides to stay or scroll off these words):
- Sentence case baseline. Then put EXACTLY ONE (at most two, if the hook is \
long) high-impact word in FULL UPPERCASE so the eye stops on it: the action \
verb, the outcome word, or the emotional pivot (NEVER, STOP, WRONG, BANNED, \
NOBODY, ILLEGAL). Example: "The FDA quietly BANNED the most effective peptides".
- The capitalised word(s) must survive the strip test: reading ONLY the \
uppercase words must still communicate the promise of the clip.
- NEVER capitalise two adjacent words. NEVER capitalise the first word unless \
it is a contrarian opener (STOP / NEVER). NEVER capitalise connective words \
(the, a, you, is, to...), brand names, or numbers. Acronyms (FDA, TRT, GLP-1, \
BPC-157) keep their normal casing and do not count as your capitalised word.
- Keep capitalised words under 20 percent of the hook. One strong cap beats \
two weak ones.
- ABSOLUTE RULE: never use an em dash or en dash (— or –) anywhere in the \
hook. Use a full stop or a comma instead.
- The hook must read like a person talking, not a billboard.

TOPIC-BOUNDARY RULE (mandatory): One clip = one complete idea. Start where a \
self-contained thought begins and END where that thought RESOLVES — right before \
a topic change, a new question from the host, or the speaker moving to a different \
subject. NEVER end a clip on the first sentence of a new topic. Topic completeness \
wins over hitting a target length.

{FEWSHOT_BOUNDARY_EXAMPLES}

NUMBERED SENTENCES (index [i] with timestamps):
{span_lines}

FIRST split the sentences into self-contained topic segments. THEN select the best \
clips by referencing sentence indices only — do NOT invent float timestamps.

Return ONLY this JSON object (no prose, no code fences), best clips first:
{{
  "topics": [
    {{
      "start": <float seconds — first word of the topic>,
      "end": <float seconds — last word of the resolving thought>,
      "summary": "<one line: what this topic is about>",
      "ends_because": "<host asks new question / subject change / wrap-up cue / story resolves>"
    }}
  ],
  "clips": [
    {{
      "start_sentence": <int — index of the first sentence of this clip (0 to {n_spans - 1})>,
      "end_sentence": <int — index of the last sentence of this clip (0 to {n_spans - 1})>,
      "score": <float 0.0-1.0>,
      "hook": "<one compelling sentence summarising the opening sentence of this clip>",
      "reason": "<brief explanation why this moment is strong>"
    }}
  ]
}}

If no moments meet the criteria, return {{"topics": [], "clips": []}}.
"""

    # ── Legacy float-time mode (no sentence spans — unchanged behaviour) ──────
    seg_lines = "\n".join(
        f"[{seg['start']:.1f}s-{seg['end']:.1f}s] {seg['text']}" for seg in transcript
    )

    return f"""You are a clip ranking assistant. Analyse the transcript below and identify the best moments to cut as short-form clips.

CAMPAIGN RANKING RULES:
{rules.strip()}{stance_section}

CLIP LENGTH CONSTRAINTS: minimum {clip_len[0]}s, maximum {clip_len[1]}s
MAXIMUM CLIPS TO RETURN: {max_clips}{comment_section}{pref_section}

SENTENCE-BOUNDARY RULE (mandatory): Choose start at the FIRST word of a sentence \
and end at the LAST word of a sentence — the clip must be a complete, coherent \
thought that does not start or end mid-sentence. The hook must describe the point \
made in the opening sentence.

HOOK STYLE RULES (mandatory — the hook is burned on screen over the first \
seconds of the clip; a viewer decides to stay or scroll off these words):
- Sentence case baseline. Then put EXACTLY ONE (at most two, if the hook is \
long) high-impact word in FULL UPPERCASE so the eye stops on it: the action \
verb, the outcome word, or the emotional pivot (NEVER, STOP, WRONG, BANNED, \
NOBODY, ILLEGAL). Example: "The FDA quietly BANNED the most effective peptides".
- The capitalised word(s) must survive the strip test: reading ONLY the \
uppercase words must still communicate the promise of the clip.
- NEVER capitalise two adjacent words. NEVER capitalise the first word unless \
it is a contrarian opener (STOP / NEVER). NEVER capitalise connective words \
(the, a, you, is, to...), brand names, or numbers. Acronyms (FDA, TRT, GLP-1, \
BPC-157) keep their normal casing and do not count as your capitalised word.
- Keep capitalised words under 20 percent of the hook. One strong cap beats \
two weak ones.
- ABSOLUTE RULE: never use an em dash or en dash (— or –) anywhere in the \
hook. Use a full stop or a comma instead.
- The hook must read like a person talking, not a billboard.

TOPIC-BOUNDARY RULE (mandatory): One clip = one complete idea. Start where a \
self-contained thought begins and END where that thought RESOLVES — right before \
a topic change, a new question from the host, or the speaker moving to a different \
subject. NEVER end a clip on the first sentence of a new topic; if a new subject \
has only just been introduced at the tail, that subject belongs to the NEXT clip, \
so trim the end back to where the prior thought completed. Topic completeness wins \
over hitting a target length.

{FEWSHOT_BOUNDARY_EXAMPLES}

TRANSCRIPT (timestamps in seconds):
{seg_lines}

FIRST split the transcript into self-contained topic segments (each is ONE complete \
idea, ending where its thought resolves — NOT at the first word of the next subject). \
THEN select the best clips, choosing each clip's boundaries to align with those \
segments so no clip's end crosses into the start of the following segment.

Return ONLY this JSON object (no prose, no code fences), best clips first:
{{
  "topics": [
    {{
      "start": <float seconds — first word of the topic>,
      "end": <float seconds — last word of the resolving thought>,
      "summary": "<one line: what this topic is about>",
      "ends_because": "<host asks new question / subject change / wrap-up cue / story resolves>"
    }}
  ],
  "clips": [
    {{
      "start": <float seconds — must be the first word of a sentence>,
      "end": <float seconds — must be the last word of a sentence, on a topic boundary>,
      "score": <float 0.0-1.0>,
      "hook": "<one compelling sentence summarising the opening sentence of this clip>",
      "reason": "<brief explanation why this moment is strong>"
    }}
  ]
}}

If no moments meet the criteria, return {{"topics": [], "clips": []}}.
"""


def rank_moments(
    transcript: list[dict],
    rules: str,
    comment_summary: str | None,
    clip_len: tuple[int, int],
    max_clips: int,
    preference_context: str = "",
    sentence_spans: list[dict] | None = None,
    stance: str = "",
) -> list[dict]:
    """
    Call the LLM to rank transcript moments.

    Args:
        transcript:          [{start: float, end: float, text: str}]
        rules:               campaign.ranking.ranking_rules text
        comment_summary:     optional per-post comment aggregation summary
        clip_len:            (min_seconds, max_seconds)
        max_clips:           max clips to request (LLM hint, enforced again by select_clips)
        preference_context:  optional learned-preference block (contract §6).
                             Injected into the prompt after ranking rules and before
                             sentence-boundary rules.  Pass "" to omit (default).
        sentence_spans:      optional punctuation-restored sentence spans from
                             core.punctuate.restore_sentences().  When provided,
                             the prompt uses NUMBERED sentences and requires
                             start_sentence/end_sentence indices from the model
                             (§R2.2 Spotify pattern).  When absent the prompt and
                             parsing behave exactly as today (regression-critical).
        stance:              campaign stance string (R4).  When non-empty, a
                             MANDATORY STANCE RULE block is injected instructing
                             the model to score 0 / exclude contradicting moments.

    Returns:
        [{start, end, score, hook, reason}] — validated, length-filtered.
        May be empty if the LLM finds nothing suitable.

    Raises:
        RuntimeError if LLM_API_KEY or LLM_MODEL are not set.
        Exception on unrecoverable API errors (after one retry).
    """
    try:
        import anthropic  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "anthropic SDK is required for LLM ranking. "
            "Install it with: pip install anthropic"
        ) from exc

    settings = get_settings()
    api_key, model = settings.require_llm()

    # Route by provider: explicit LLM_BASE_URL wins; otherwise OpenRouter keys
    # go to OpenRouter's Anthropic-compatible endpoint, Anthropic keys go direct.
    base_url = settings.llm_base_url
    if base_url is None and api_key.startswith("sk-or-"):
        base_url = "https://openrouter.ai/api"
    if base_url:
        client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
        log.info("LLM client using base_url=%s model=%s", base_url, model)
    else:
        client = anthropic.Anthropic(api_key=api_key)

    # Compress the transcript for the prompt only (merge 2-4s fragments into
    # ~12s chunks) — big input-token saving on long podcasts. Boundary snapping
    # below still uses the full-resolution `transcript`, so precision is intact.
    # When sentence_spans are provided, the prompt uses those instead (already
    # compressed to sentence granularity), so we compress but do not use the
    # compressed transcript in the sentence-index branch.
    prompt_transcript = _compress_transcript(transcript)

    # ONE combined call does segmentation + ranking (topics + clips together),
    # instead of a second full-transcript segmentation call.
    prompt = _build_prompt(
        prompt_transcript, rules, comment_summary, clip_len, max_clips,
        preference_context=preference_context,
        sentence_spans=sentence_spans,
        stance=stance,
    )

    def _call() -> str:
        message = create_completion(
            client, model, 4096, [{"role": "user", "content": prompt}]
        )
        return extract_text(message)

    # First attempt
    response_text = _call()
    log.debug("LLM raw response", extra={"length": len(response_text)})

    moments_raw, topics_raw = _parse_ranking_response(response_text)

    if not moments_raw and not topics_raw:
        log.warning(
            "LLM response did not contain parseable JSON; retrying once",
            extra={"response_preview": response_text[:300]},
        )
        response_text = _call()
        moments_raw, topics_raw = _parse_ranking_response(response_text)

    if not moments_raw:
        log.error(
            "LLM returned no clips after retry; returning empty",
            extra={"response_preview": response_text[:300]},
        )
        return []

    topic_segments = _validate_topic_segments(topics_raw)
    if topic_segments:
        log.info("Ranking response included %d topic segments", len(topic_segments))

    validated = _validate_moments(moments_raw, clip_len, sentence_spans=sentence_spans)

    # ------------------------------------------------------------------
    # Snap every moment's start/end to whole-sentence boundaries so that
    # clips never begin or end mid-word / mid-thought.
    # This is a best-effort post-process: if it fails for any reason the
    # raw validated timestamps are used unchanged.
    # ------------------------------------------------------------------
    try:
        # In sentence-index mode the moments are ALREADY aligned to the
        # punctuation-restored spans — snap with those SAME spans (a safe
        # no-op guard), never the regex-derived ones, or the two different
        # segmentations fight and shift correct boundaries by 0.5-2s.
        snap_spans = sentence_spans if sentence_spans else build_sentence_spans(transcript)
        if snap_spans:
            snapped: list[dict] = []
            for moment in validated:
                new_start, new_end = snap_to_sentences(
                    moment["start"],
                    moment["end"],
                    snap_spans,
                    clip_len,
                )
                # Topic-boundary guard: if the (sentence-snapped) end has bled
                # into the opening of a NEW topic, pull it back to where the
                # prior thought resolved. No-op when no segmentation ran.
                new_start, new_end = snap_end_off_next_topic(
                    new_start,
                    new_end,
                    topic_segments,
                    snap_spans,
                    clip_len,
                )
                snapped.append({**moment, "start": new_start, "end": new_end})
            validated = snapped
            log.debug(
                "Sentence + topic boundary snapping applied",
                extra={
                    "span_count": len(snap_spans),
                    "topic_count": len(topic_segments),
                    "moment_count": len(validated),
                },
            )
        else:
            log.debug("No sentence spans derived from transcript; skipping snap")
    except Exception as exc:  # pragma: no cover
        log.warning(
            "Sentence/topic boundary snapping failed; using raw LLM timestamps",
            extra={"error": str(exc)},
        )

    log.info(
        "LLM ranking complete",
        extra={"raw_count": len(moments_raw), "validated_count": len(validated)},
    )
    return validated
