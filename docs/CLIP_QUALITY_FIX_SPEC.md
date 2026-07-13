# CLIP_QUALITY_FIX_SPEC.md — Face-Centered Reframing, Blur Rejection, True Idea Boundaries, Campaign Stance

Operator brief 2026-07-12 (verbatim requirements distilled). This is the build
spec for the fix pass. Binding alongside docs/PIPELINE_QUEUE_CONTRACTS.md and
docs/REVAMP_CONTRACTS.md. Research findings are appended in §R.

## The operator's manual workflow we are automating (ground truth)

> "I screen record the podcast, put it in CapCut at 9:16. When the crop isn't
> centred on the face, I SPLIT the video at that point and DRAG the frame
> across so their full face is inside. When the camera angle changes, I split
> again and drag back. I do split-drag through the whole video so the face is
> centred AT ALL TIMES."

Programmatic equivalent: scene-cut detection = the SPLIT; per-scene crop
placement anchored on the active speaker's face = the DRAG. We already do a
version of this (render/reframe.py) — it demonstrably fails in specific ways.

## MEASURED EVIDENCE (frame analysis of the 2026-07-12 demo renders, YuNet + Laplacian)

1. **Edge-pinned face (operator complaint #1).** clip46: from t=6s the face
   box sits at edgeL≈0.00 (cx≈0.16-0.20) for ~30 of 42 seconds — face half
   out of frame. Cause: per-scene crop anchored wrong after an angle change /
   wrong face chosen in a 2-face shot.
2. **Out-of-focus segments shipped (complaint #2).** clip52 t=10-20s has
   center-frame Laplacian variance 2.7-7.5; clip55 t=10-20s ≈7-10. Sharp
   footage in the same clips measures 2400-2700 (with hook box) / 60+ (plain).
   Nothing detects or rejects this today.
3. **Boundary failures dominate gate rejections (complaint #3).** 6 of 9
   didnt_pass cite SELF-CONTAINED: starts mid-thought ("of that. We are the
   same..."), ends trailing into the next topic's setup sentence, unfinished
   metaphors at the tail. Several clips scored >0.6 otherwise (0.730, 0.675) —
   boundaries are the #1 yield killer. Each bad boundary wastes a full render.
4. **Stance hole (complaint #4).** Clip 53 reached READY while being
   ANTI-peptide ("neither really WOWED him, a nicotine patch beat them all").
   The campaign is pro-peptide only. Ranker + gate both missed stance.
   (Operator rejected it manually 2026-07-12 with reason off_brand.)

## REQUIREMENTS (definition of done)

R1. **Face fully in frame at all times when a face exists in the source.**
    Post-reframe validation: sampled frames must have the primary face bbox
    fully inside the 9:16 crop with margin (no edge contact); a scene whose
    chosen crop would cut the active face is WRONG by definition and must be
    re-anchored. Walking/standing speakers: crop follows smoothly within the
    scene (no static crop that lets the subject walk out of frame).
R2. **Blur rejection.** Defocused footage (Laplacian variance under threshold,
    calibrated against §EVIDENCE numbers, measured on the face region or
    center band, sampled ≥2 fps-equivalent) must never ship: prefer trimming
    the clip window to sharp footage; if the defect spans the clip, fail the
    clip BEFORE Modal render spend (probe at ranking time is impossible — the
    check runs at render time pre-overlay, and the design gate gets a
    deterministic blur check as backstop).
R3. **Idea boundaries fixed at a deeper level, pre-render.** A clip must start
    at the true beginning of the idea and end where that idea resolves. The
    current single-call segmentation + regex sentence snap is insufficient on
    noisy YouTube captions. Layered fix per §R2-research; non-negotiable
    element: a cheap boundary VERIFICATION step per candidate clip BEFORE
    rendering (render only clips that pass), so gate self_contained failures
    trend to zero instead of burning GPU.
R4. **Campaign stance enforcement.** Per-campaign `stance` (e.g. peptides =
    pro-peptide): ranker must score 0 / drop moments whose framing contradicts
    it, and the review gate gets an explicit campaign_alignment auto-fail
    check derived from campaign config (NOT relaxable via
    relaxed_safety_checks; independent of the safety list).
R5. All of it config-driven — nothing niche-specific hardcoded (fitness.yaml
    untouched; new keys optional with safe defaults).
R6. Full test coverage; suite stays green; a NO-CONTEXT reviewer agent audits
    the diff before deploy; Railway + Modal (`make deploy-modal`) both
    deployed; verified by a fresh demo run with frame extraction.

## Hard constraints (unchanged)

Human review gate stays; demo label never burned in; real podcast footage
only; hardwired hook/caption/watermark layout; secrets from env; learning
loop can never relax safety or these design rules.

## §R. RESEARCH FINDINGS (appended after the research agents reported)

### R1 — Reframing / active speaker / blur (research verified 2026-07-12; sources: Junhua-Liao/LR-ASD IJCV 2025, Light-ASD CVPR 2023, Google AutoFlip, KazKozDev/auto-vertical-reframe)

1. **Margin guard (fixes the measured edge-pinning directly, deterministic).**
   Per SAMPLED FRAME (not once per scene): the active face bbox must sit
   within the central 80% of the crop width (10% margin each side; 15-20%
   headroom above). Violation → shift the crop rect to re-center the face
   (clamped to source bounds). This alone fixes clip46-style half-face.
2. **Virtual camera per scene (AutoFlip model): three modes.** stationary
   (face-center stddev <5% of source width → constant crop), panning/tracking
   (2nd-degree numpy.polyfit over sampled face centers → smooth path, clamped,
   max crop shift ~1px/frame at 30fps). Fixes walking/standing speakers.
   EMA fallback α=0.05-0.08; never α>0.15.
3. **Blur rejection: Laplacian variance on the FACE CROP** (not full frame —
   background bokeh must not penalize a sharp face). Threshold ~200 start
   (sharp studio >500; our measured defocus segments: 3-10). Blurry sampled
   frames are excluded from anchoring/speaker selection; a fully-blurry scene
   is flagged; the design gate gains a deterministic blur check as backstop so
   defocused clips can never reach READY.
4. **Active speaker: LR-ASD** (Junhua-Liao/LR-ASD, MIT, 0.84M params,
   ~free on L4) replacing the mouth-variance heuristic: consumes YuNet face
   tracks + audio mel windows → per-track speaking probability; pick highest
   mean per scene. Fallback chain when weights/deps unavailable: improved
   heuristic (largest+most-central face) + margin guard — pipeline must never
   break for lack of the model. Do NOT use TalkNet (27x compute), ClipsAI
   resize (pyannote token, low maintenance), AutoFlip binary (legacy C++).

### R2 — Boundary quality (research verified 2026-07-12; sources: ClipsAI, arxiv 2505.23908 Spotify preview paper, oliverguhr FullStop, arxiv 2506.03793)

Root cause confirmed: EVERY production tool (ClipsAI, FunClip, the Spotify
LLM-preview system) requires PUNCTUATED, sentence-segmented input before any
clip logic. We regex-split raw unpunctuated caption fragments — that is why
snaps land mid-thought. Layered fix, by leverage:

1. **Punctuation restoration pre-pass (highest ROI).** Restore punctuation on
   the concatenated fragment text before sentence splitting
   (`deepmultilingualpunctuation` / FullStop, BERT-base; ~60-90s CPU for a
   90-min podcast, one-time per source → CACHE the sentence list on the
   transcript row). Prefer an ONNX-runtime variant if practical to avoid a
   full torch install in the Railway image; MUST degrade gracefully to the
   current regex path when the model is unavailable. Existing char→time
   interpolation is correct once input is sentence-delimited.
2. **Sentence-INDEX selection (Spotify pattern, 54.2% preference win).** Give
   the ranking LLM the numbered sentence list (with timestamps) and require
   sentence_start_index/sentence_end_index instead of raw float seconds —
   the model can no longer invent mid-sentence timestamps.
3. **Speaker-turn regex pre-filters** (cheap, deterministic): never START a
   clip on a sentence ending "?" (interviewer question) or starting with
   So/And/But/Well/Yeah/Right/Exactly/I mean (continuations); extend END 1-2
   sentences when the tail ends in like/than/as if (unfinished comparison).
4. **Pre-render LLM boundary verification (the render-money gate).** Per
   candidate clip: prompt with 2-3 sentences BEFORE (context only), the clip
   sentences, 2-3 sentences AFTER; JSON verdict pass/fail +
   adjusted_start_sentences/adjusted_end_sentences deltas; one adjustment
   round max; only verdict=pass clips reach Modal. Model: cheap tier
   (anthropic/claude-haiku-4.5 via OpenRouter), <$0.001/clip.
   Env override BOUNDARY_CHECK_MODEL; fall back to LLM_MODEL.
5. NOT building: diarization (pyannote), custom completeness models,
   render-time semantic scoring (too late by design).

---

## PART 2 — Topical relevance + hook/body match + aggressive end-trim (operator review 2026-07-12, post-verification)

Operator reviewed the READY clips and gave three findings. I (orchestrator) read
the raw transcripts and pinned the exact correct boundaries. This section is
BINDING for the follow-up fix.

### Finding A — off-topic / hook-body mismatch reaches READY (clip 76)
Clip 76 hook: "GH secretagogues like CJC-1295 and ipamorelin are permissive
anabolics." ACTUAL clip body at its timestamps (497.6-556.6s), from the
transcript:
```
497  ...allodynia where their skin felt like it had been sunburned...
505  ...glucagon receptors on sensory neurons...
521  ...just like semaglutide and tirzepatide, the risk for pancreatitis...
531  ...and gallstones. rapid weight loss is the top trigger for gallstones...
540  Now again, a lot of people are getting their hands on retatrutide...
552  For retatrutide, in the trials, the doses were 2mg, 4, 6, 9, 12...
```
The body is entirely RETATRUTIDE side-effects + dosing — it never mentions
CJC-1295 or secretagogues. The hook is about a different moment. The gate passed
it at 0.75 because the content is internally coherent and on-brand-ish; nothing
checked that the BODY delivers the HOOK's specific claim, nor that the clip stays
on ONE topic (it spans allodynia → pancreatitis → gallstones → retatrutide
dosing = 4 subjects).

REQUIREMENT A1 (hook/body match, hard gate): the review gate MUST fail a clip
when the clip body does not substantively deliver the specific claim in the hook.
New Phase-2 verdict field `hook_body_match: {matches: bool, reason}`; matches==false
→ hard didnt_pass (like campaign_alignment). Prompt must instruct: "The hook names
a specific subject/claim. Does the transcript actually deliver THAT subject? If the
hook says CJC-1295 but the clip is about retatrutide side effects, matches=false."

REQUIREMENT A2 (single-topic / no multi-subject bleed): a clip that traverses
more than one distinct subject (a different named peptide, a list transition, a
new question) is not self-contained. Fold this into the existing self_contained
check AND the pre-render boundary verifier: reject/ retrim when the clip contains
a mid-clip subject change.

REQUIREMENT A3 (topical relevance, not just stance): stance catches anti-peptide;
it does NOT catch generic/off-topic. Add to the gate: the clip must substantively
discuss a campaign topic (named peptide + its mechanism/experience/effect), not
merely mention one in passing while the substance is generic advice. (Clip 87's
body is generic heart-rate/hydration/magnesium/medical-disclaimer with one passing
"retatrutide" — that is the failure mode to catch.)

### Finding B — end bleeds past the idea's resolution (clip 80 Selank)
Transcript:
```
232.7  ...some people report having a lot less daily anxiety when they use it,
       but there's really mixed results. Some people have worse anxiety.
236.9  I'm not sure that I would try this one again.   <-- IDEA RESOLVES ~238.4
238.4  Number 16, CAX. This is kind of like taking Adderall...  <-- NEXT LIST ITEM
```
Clip ends at 238.9 → bleeds ~0.5-1s into "Number 16, CAX" (the next peptide).
CORRECT END: 238.4 (end of "...try this one again."). The clip must end BEFORE
the next list item is named.

REQUIREMENT B1 (list/transition end-signals): the end must be pulled back to the
last sentence of the current point when the following sentence begins a new list
item or subject. Deterministic end-signal markers to detect at the tail and trim
before: sentence STARTS with any of — "Number \d+", "Next up", "The next one",
"Number", "Now again", "And just like", "Also,", "Oh, and", "So the next",
"Moving on", "Alright, next". If a clip's last sentence is (or immediately
precedes) such a transition, trim the end to the prior sentence boundary. Add
these to producer/boundary_check.py and to the verifier + ranker prompt few-shot.

REQUIREMENT B2 (verifier strictness on END): the pre-render boundary verifier is
currently too lenient on the tail (these clips passed it). Strengthen its prompt:
"Be STRICT about the END. The clip must end the MOMENT the specific idea in the
hook resolves. If the last 1-2 sentences introduce a new list item, a new peptide,
a new question, a tangent (e.g. a generic medical disclaimer), or start a
different subject, set verdict=fail and return adjusted_end_sentences negative to
trim them off." Include the clip-80 and clip-76 cases as few-shot examples.

### Correct-boundary few-shot examples (bake into prompts verbatim)
1. Selank clip: hook about Selank anxiety. GOOD end = "...I'm not sure that I
   would try this one again." BAD end = bleeding into "Number 16, CAX" (next
   peptide). Rule shown: end before the next list item.
2. CJC clip: hook about CJC-1295 secretagogues, body about retatrutide gallstones
   = hook/body MISMATCH → reject entirely (not a trim; the whole span is wrong).
3. Generic-advice clip: hook "racing heart on peptides", body is generic
   hydration/magnesium/"I'm a doctor on YouTube" disclaimer with one passing
   peptide mention = topical-relevance FAIL.

### Also observed (fix if cheap): punctuation-restored sentence spans show
alignment artifacts (stray ",," tokens, timestamps that don't cover all content)
in the cached `transcripts.sentences`. If the aligner is dropping/duplicating on
YT-caption noise, harden `_align_sentences_to_times` — but never at the cost of
the graceful-None fallback. Low priority vs A/B above.
