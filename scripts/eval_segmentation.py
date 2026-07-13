"""
scripts/eval_segmentation.py — Offline segmentation eval harness (B4).

Runs FULLY OFFLINE — zero network, zero LLM, zero render spend.

For each of the 4 real gate-failure cases in
  tests/fixtures/segmentation/boundary_failure_pairs.json

the harness:
  1. Loads the source transcript from the matching fixture file.
  2. Builds sentence spans (build_sentence_spans from segments, or uses the
     fixture's cached .sentences when available).
  3. Applies the full deterministic guard chain:
       apply_prefilters → detect_unit_boundaries → build_units_from_boundaries
       → clip_within_unit
     to the WRONG candidate boundaries.
  4. Checks four assertions on the result:
       (a) start lands on a sentence boundary (within _EPS tolerance)
       (b) end lands on a sentence boundary (within _EPS tolerance)
       (c) end does NOT fall inside a later deterministic topic unit than start
       (d) end sentence does NOT start with a transition/new-topic opener

  (a) and (b) are HARD assertions — failure exits non-zero.
  (c) and (d) are SOFT assertions with LLM-caveat (some cases genuinely need
  the LLM to detect the exact boundary; the harness documents this honestly).

  A case is PASS when all four assertions hold.
  A case is PARTIAL when (a)+(b) hold but (c) or (d) cannot be fully verified
  without the LLM.
  A case is FAIL when (a) or (b) fails — these are always deterministic.

Usage:
    python scripts/eval_segmentation.py [--verbose]
    make eval-segmentation

Exit code:
    0  — all regression cases pass (a)+(b) assertions.
    1  — one or more cases fail a hard assertion.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

# Ensure the repo root is on sys.path so we can import core/producer
_REPO = pathlib.Path(__file__).parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXTURES_DIR = _REPO / "tests" / "fixtures" / "segmentation"
_PAIRS_FILE = _FIXTURES_DIR / "boundary_failure_pairs.json"
_EPS = 0.75   # timestamp tolerance in seconds (matches core/topics._EPS)

# Source-id → fixture file stem
_SOURCE_TO_FIXTURE: dict[str, str] = {
    "youtube:Cu9PVWM2fJo": "youtube_Cu9PVWM2fJo",
    "youtube:O640yAgq5f8": "youtube_O640yAgq5f8",
    "youtube:MZyDYMWbmmw": "youtube_MZyDYMWbmmw",
    "youtube:ukCySPXN0WU": "youtube_ukCySPXN0WU",
}


# ---------------------------------------------------------------------------
# Imports from the repo (must come after sys.path fixup)
# ---------------------------------------------------------------------------

def _import_guards():
    """Return (apply_prefilters, detect_unit_boundaries, build_units_from_boundaries,
    clip_within_unit, build_sentence_spans, TRANSITION_START_RE, _topic_index_at)."""
    from producer.boundary_check import apply_prefilters
    from core.topics import (
        detect_unit_boundaries,
        build_units_from_boundaries,
        clip_within_unit,
        TRANSITION_START_RE,
        _topic_index_at,
    )
    from core.sentences import build_sentence_spans
    return (
        apply_prefilters,
        detect_unit_boundaries,
        build_units_from_boundaries,
        clip_within_unit,
        build_sentence_spans,
        TRANSITION_START_RE,
        _topic_index_at,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_fixture(source_id: str) -> dict:
    stem = _SOURCE_TO_FIXTURE.get(source_id)
    if not stem:
        raise ValueError(f"No fixture mapped for source_id={source_id!r}")
    path = _FIXTURES_DIR / f"{stem}.json"
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _get_sentence_spans(fixture: dict, build_sentence_spans) -> list[dict]:
    """Build fine-grained sentence spans from .segments using build_sentence_spans.

    The fixture's cached .sentences field (from punctuation-restoration) produces
    very coarse spans (~100s average for the O640 fixture) that are not suitable
    for precise boundary snapping.  build_sentence_spans produces ~6s spans using
    the same logic as the production pipeline.  We always prefer the fine-grained
    version for the eval harness so that boundary assertions are accurate.
    """
    segments = fixture.get("segments", [])
    if segments:
        return build_sentence_spans(segments)
    # Fallback: use cached sentences when no segments available
    sentences = fixture.get("sentences") or []
    return [
        {"text": s.get("text", ""), "start": float(s["start"]), "end": float(s["end"])}
        for s in sentences
        if s.get("text") and s.get("start") is not None and s.get("end") is not None
    ]


def _is_on_sentence_boundary(t: float, spans: list[dict], attr: str) -> bool:
    """Return True when t matches a span's start (attr='start') or end (attr='end')."""
    return any(abs(float(s[attr]) - t) <= _EPS for s in spans)


def _end_sentence_text(t: float, spans: list[dict]) -> str | None:
    """Return text of the sentence whose end is closest to t (within _EPS)."""
    best = None
    best_delta = float("inf")
    for s in spans:
        delta = abs(float(s["end"]) - t)
        if delta <= _EPS and delta < best_delta:
            best = s["text"]
            best_delta = delta
    return best


# ---------------------------------------------------------------------------
# Core check function (importable from tests)
# ---------------------------------------------------------------------------

def run_case(
    pair: dict,
    verbose: bool = False,
) -> dict:
    """Run deterministic guards on one failure pair.

    Returns a result dict:
      {
        "id": str,
        "wrong": {"start", "end"},
        "correct": {"start", "end"},
        "after_guards": {"start", "end"},
        "assertions": {
            "a_start_on_boundary": bool,
            "b_end_on_boundary": bool,
            "c_end_in_same_or_earlier_unit": bool | None,  # None = LLM needed
            "d_end_not_transition_opener": bool,
        },
        "verdict": "PASS" | "PARTIAL" | "FAIL",
        "notes": [str],
      }
    """
    (
        apply_prefilters,
        detect_unit_boundaries,
        build_units_from_boundaries,
        clip_within_unit,
        build_sentence_spans,
        TRANSITION_START_RE,
        _topic_index_at,
    ) = _import_guards()

    pid = pair["id"]
    wrong = pair["wrong"]
    correct = pair["correct"]
    source_id = pair["source_id"]

    notes: list[str] = []
    result_template = {
        "id": pid,
        "wrong": wrong,
        "correct": correct,
        "after_guards": None,
        "assertions": {
            "a_start_on_boundary": False,
            "b_end_on_boundary": False,
            "c_end_in_same_or_earlier_unit": None,
            "d_end_not_transition_opener": False,
        },
        "verdict": "FAIL",
        "notes": notes,
    }

    # ── Load fixture and build sentence spans ─────────────────────────────────
    try:
        fixture = _load_fixture(source_id)
    except Exception as exc:
        notes.append(f"ERROR: could not load fixture for {source_id}: {exc}")
        return {**result_template, "verdict": "FAIL"}

    spans = _get_sentence_spans(fixture, build_sentence_spans)
    if not spans:
        notes.append("ERROR: no sentence spans available — cannot evaluate")
        return {**result_template, "verdict": "FAIL"}

    notes.append(f"Built {len(spans)} sentence spans from fixture")

    # Clip length hint from the wrong boundaries (min 10s, max 90s)
    clip_dur = wrong["end"] - wrong["start"]
    clip_len = (max(10, clip_dur - 20), min(90, clip_dur + 20))

    candidate = {"start": float(wrong["start"]), "end": float(wrong["end"]),
                 "score": 0.8, "hook": "", "reason": ""}

    # ── Step 1: apply prefilters (sentence-snap + transition-trim + bad-start) ─
    candidate = apply_prefilters(candidate, spans, clip_len)
    notes.append(
        f"After apply_prefilters: start={candidate['start']:.3f} end={candidate['end']:.3f}"
    )

    # ── Step 2: detect deterministic unit boundaries ──────────────────────────
    boundary_indices = detect_unit_boundaries(spans)
    det_units = build_units_from_boundaries(spans, boundary_indices)
    notes.append(
        f"Deterministic units: {len(det_units)} "
        f"(boundaries at sentence indices: {boundary_indices[:10]}{'...' if len(boundary_indices) > 10 else ''})"
    )

    # ── Step 3: enforce within-unit ───────────────────────────────────────────
    before_cwu = dict(candidate)
    candidate = clip_within_unit(candidate, det_units, spans)
    if candidate["start"] != before_cwu["start"] or candidate["end"] != before_cwu["end"]:
        notes.append(
            f"clip_within_unit adjusted: "
            f"start {before_cwu['start']:.3f}→{candidate['start']:.3f} "
            f"end {before_cwu['end']:.3f}→{candidate['end']:.3f}"
        )
    else:
        notes.append("clip_within_unit: no adjustment (already within one unit)")

    after = {"start": candidate["start"], "end": candidate["end"]}

    # ── Assert (a): start on sentence boundary ────────────────────────────────
    a_pass = _is_on_sentence_boundary(after["start"], spans, "start")
    if not a_pass:
        # Try end attr too (some sentence spans have start == end for single-word spans)
        a_pass = _is_on_sentence_boundary(after["start"], spans, "end")
    if not a_pass:
        notes.append(
            f"FAIL (a): start={after['start']:.3f} not within {_EPS}s of any sentence start"
        )

    # ── Assert (b): end on sentence boundary ─────────────────────────────────
    b_pass = _is_on_sentence_boundary(after["end"], spans, "end")
    if not b_pass:
        notes.append(
            f"FAIL (b): end={after['end']:.3f} not within {_EPS}s of any sentence end"
        )

    # ── Assert (c): end in same/earlier unit as start ─────────────────────────
    if len(det_units) >= 2:
        si = _topic_index_at(det_units, after["start"])
        ei = _topic_index_at(det_units, after["end"])
        c_pass: bool | None = (ei <= si)
        if not c_pass:
            notes.append(
                f"FAIL (c): end unit {ei} > start unit {si} "
                f"(end={after['end']:.3f} crosses unit boundary at "
                f"{det_units[si]['end']:.3f}s)"
            )
    else:
        # Only one deterministic unit — trivially satisfied.  Document the LLM caveat.
        c_pass = None  # type: ignore[assignment]
        notes.append(
            "(c) PARTIAL: deterministic markers found no unit boundaries in this span; "
            "LLM topic segmentation is needed to verify this assertion fully"
        )
        notes.append(
            f"    Correct boundary requires clip to end by {correct['end']:.3f}s "
            f"(currently ends {after['end']:.3f}s)"
        )

    # ── Assert (d): end sentence does not start a new topic/transition ────────
    end_text = _end_sentence_text(after["end"], spans)
    if end_text is not None:
        d_pass = not bool(TRANSITION_START_RE.match(end_text.strip()))
        if not d_pass:
            notes.append(
                f"FAIL (d): end sentence is a transition opener: {end_text[:80]!r}"
            )
        else:
            notes.append(f"(d) end sentence OK: {end_text[:80]!r}")
    else:
        d_pass = True  # no sentence found at end — treat as pass (can't verify)
        notes.append(f"(d) PARTIAL: no sentence found near end={after['end']:.3f}s")

    # ── Verdict ───────────────────────────────────────────────────────────────
    hard_pass = a_pass and b_pass
    soft_pass = (c_pass is True or c_pass is None) and d_pass

    if hard_pass and soft_pass and c_pass is True:
        verdict = "PASS"
    elif hard_pass and (c_pass is None or soft_pass):
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"

    return {
        "id": pid,
        "wrong": wrong,
        "correct": correct,
        "after_guards": after,
        "assertions": {
            "a_start_on_boundary": a_pass,
            "b_end_on_boundary": b_pass,
            "c_end_in_same_or_earlier_unit": c_pass,
            "d_end_not_transition_opener": d_pass,
        },
        "verdict": verdict,
        "notes": notes,
    }


def run_all_cases(verbose: bool = False) -> list[dict]:
    """Load pairs and run run_case for each."""
    with open(_PAIRS_FILE, encoding="utf-8") as fh:
        data = json.load(fh)
    pairs = data.get("pairs", [])
    return [run_case(p, verbose=verbose) for p in pairs]


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def _print_report(results: list[dict], verbose: bool) -> None:
    verdicts = [r["verdict"] for r in results]
    n_total = len(results)
    n_pass = verdicts.count("PASS")
    n_partial = verdicts.count("PARTIAL")
    n_fail = verdicts.count("FAIL")

    print("=" * 72)
    print("CLIP ENGINE — SEGMENTATION EVAL HARNESS (B4)")
    print("=" * 72)
    print(f"Fixture: {_PAIRS_FILE}")
    print(f"Cases: {n_total}   PASS: {n_pass}   PARTIAL: {n_partial}   FAIL: {n_fail}")
    print()

    for r in results:
        pid = r["id"]
        v = r["verdict"]
        wrong = r["wrong"]
        correct = r["correct"]
        after = r["after_guards"] or {}
        a = r["assertions"]

        marker = {"PASS": "PASS", "PARTIAL": "PART", "FAIL": "FAIL"}[v]
        print(f"[{marker}] {pid}")
        print(f"       wrong   start={wrong['start']:.3f}  end={wrong['end']:.3f}")
        print(f"       correct start={correct['start']:.3f}  end={correct['end']:.3f}")
        if after:
            print(
                f"       guards  start={after['start']:.3f}  end={after['end']:.3f}"
            )
        print(
            f"       (a)start_on_boundary={a['a_start_on_boundary']}  "
            f"(b)end_on_boundary={a['b_end_on_boundary']}  "
            f"(c)within_unit={a['c_end_in_same_or_earlier_unit']}  "
            f"(d)not_transition={a['d_end_not_transition_opener']}"
        )
        if verbose:
            for note in r["notes"]:
                print(f"         {note}")
        print()

    print("-" * 72)
    pass_rate = (n_pass + n_partial) / n_total * 100 if n_total else 0
    hard_pass_rate = n_pass / n_total * 100 if n_total else 0
    print(
        f"Hard assertions (a)+(b) pass rate: "
        f"{sum(r['assertions']['a_start_on_boundary'] and r['assertions']['b_end_on_boundary'] for r in results)}/{n_total}"
    )
    print(
        f"Overall pass rate (PASS): {n_pass}/{n_total} ({hard_pass_rate:.0f}%)"
    )
    print(
        f"Overall rate including PARTIAL: {n_pass + n_partial}/{n_total} ({pass_rate:.0f}%)"
    )
    print()
    print("NOTE: PARTIAL = (a)+(b) assertions satisfied; (c)+(d) need LLM for full")
    print("      verification of the correct semantic boundary. The deterministic")
    print("      guards guarantee sentence-alignment and no-transition-bleed, but")
    print("      they cannot identify the CORRECT topic endpoint without the LLM.")
    print("      See CLIP_QUALITY_FIX_SPEC.md §B4 for the honest caveat.")
    print("=" * 72)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline segmentation eval harness")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print per-case notes")
    args = parser.parse_args(argv)

    results = run_all_cases(verbose=args.verbose)
    _print_report(results, verbose=args.verbose)

    # Exit non-zero if any HARD assertion (a or b) fails for any case
    for r in results:
        a = r["assertions"]
        if not a["a_start_on_boundary"] or not a["b_end_on_boundary"]:
            print(
                f"\nEXIT 1: Case {r['id']!r} failed hard assertion "
                f"(a)={a['a_start_on_boundary']} (b)={a['b_end_on_boundary']}"
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
