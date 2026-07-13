# CLIP ENGINE — REFERENCE TEMPLATE
**Source:** `/root/clip-engine/style_refs/Clip Engine Full Video reference.MOV`
**Derived:** 2026-07-13 via ffmpeg frame extraction + faster-whisper base (CPU, int8) transcription

> This file is the canonical spec derived from a published VICI PEPTIDES TikTok.
> All measurements come from pixel analysis of 1080×1920 frames and word-level transcript timing.
> "MATCH" = the current render code already produces this; "DIFF" = the code diverges from the reference.

---

## VISUAL TEMPLATE

### Hook

| Property | Reference (measured) | Code constant | Status |
|---|---|---|---|
| Text box background | White rounded rectangle | `box_color="#FFFFFF"` | MATCH |
| Text color | Black | `text_color="#000000"` | MATCH |
| Box width | ~83–84% of frame (≈900px of 1080px) | `boxborderw=30` (implicit ≈80–90%) | MATCH |
| **Box center Y** | **58.8–60.9% of H (≈1130–1169 px at H=1920)** | **HOOK_BOX_CENTER_Y_FRAC = 0.52 → 998 px** | **DIFF: reference is ≈130–171 px LOWER** |
| Hook visible | **t=0–6s** (gone by t=6.5s) | HOOK_SHOW_SECONDS=(0.0, 7.5); peptides.yaml [0, 8] | **DIFF: reference shows hook for ≈6 s, not 7.5–8 s** |
| Lines | 3 lines of bold text | fit_fontsize wraps to ≤4 lines | MATCH |
| Font | Montserrat ExtraBold | `assets/peptides/Montserrat-ExtraBold.ttf` | MATCH |

**Hook text pattern in reference:**
```
"Dr Dubrow REVEALS The SECRET To Effective RETA Dosing" 👀
```
Format: `"[Expert Name] REVEALS [Key Claim]" [emoji]` — a HEADLINE TEASE framing, with quotation marks
and an emoji. This differs from what the ranker currently generates (shorter direct claim sentences).
The hook text does NOT repeat the opening spoken words; it frames what the viewer is about to learn.

---

### Captions

| Property | Reference (measured) | Code constant | Status |
|---|---|---|---|
| Style | Word-by-word, **1 word per caption event** | CAPTION_MAX_WORDS_PER_EVENT = 3 | **DIFF: reference uses 1-word events** |
| **Text color** | **White (#FFFFFF) — single color, all words** | `base_color="#FFFFFF"`, `highlight_color="#00E5FF"` | **DIFF: no cyan visible in reference; all words white** |
| Outline | Thick black, ≈5–6 px | `outline_color="#000000"`, `outline_px=6` | MATCH |
| Font | Montserrat ExtraBold | `assets/peptides/Montserrat-ExtraBold.ttf` | MATCH |
| **Caption top Y** | **≈66–69% of H (≈1267–1325 px)** | CAPTION_ZONE_Y_FRAC=0.65 → MarginV=1248 px | **DIFF: reference is ≈19–77 px lower (1–4%)** |
| Position relative to hook | Below the hook box during hook phase; same Y after hook goes | Same by construction | MATCH |
| Case | ALL CAPS display (words appear in uppercase) | Raw transcript text (mixed case from ASS) | Note: reference may apply CSS/uppercase transform |

**Caption observation:** The reference clip shows single words flashing one at a time (BEST, WAY, WEIGHT,
QUICKLY, TO, ON, GUYS, IS, 2.5). All appear in bright white with thick black outline and no secondary
color. Our engine's 3-word groups with cyan (#00E5FF) highlight produce a different look.

---

### Watermark

| Property | Reference (measured) | Code constant | Status |
|---|---|---|---|
| Logo | VICI PEPTIDES wordmark | `assets/peptides/logo.png` | MATCH |
| Alignment | Bottom-center | `position: "bottom"` | MATCH |
| Width | ≈28–30% of frame (≈302–324 px) | WATERMARK_WIDTH_FRAC = 0.30 | MATCH |
| **Bottom margin** | **≈11–13% of H from bottom (≈211–249 px)** | **WATERMARK_BOTTOM_MARGIN_FRAC = 0.06 → 115 px** | **DIFF: reference has ≈2× larger bottom margin; watermark sits ≈100 px higher** |
| **Opacity** | **Visually ≈65–75%** | WATERMARK_MIN_OPACITY = 0.85 | **DIFF: reference appears more transparent than code's floor** |
| On-screen duration | Entire clip (0 s to end) | Always-on overlay | MATCH |

---

### "Via @source" Credit

Not visually detectable in any frame. The speaker's dark jacket background may obscure it.
The reference may have been produced without the credit line. Code adds it just above the watermark.
**UNKNOWN**: Cannot confirm match or diff without cleaner reference frame. Treat as TO VERIFY.

---

### Speaker Framing

- **Framing type**: Tight bust shot — single speaker, head and shoulders
- **Face position**: Eyes at ≈30–37% from top; chin at ≈40–48% from top; speaker centered horizontally
- **Background**: Neutral dark charcoal acoustic-panel backdrop; no distractions
- **Crop**: Light horizontal crop only — source appears to be already near-9:16 (podcast B-roll camera).
  No heavy active-tracking motion visible; speaker remains close to center throughout.
- **Code behavior**: `render/reframe.py` applies YuNet face detection + Gaussian-smooth virtual camera.
  For a relatively static podcast speaker this should produce a near-static center crop — **MATCH expected**.
- **Tightness**: The reference shows the speaker filling roughly the upper 55% of the frame.
  The crop is tight enough that the speaker's hand gestures extend to the frame edges — not zoomed out.

---

### Resolution / Technical

| Property | Reference | Code output |
|---|---|---|
| Resolution | 1080×1920 | 1080×1920 | MATCH |
| Aspect | 9:16 | 9:16 | MATCH |
| Frame rate | ≈60 fps native (HEVC/hvc1) | 24000/1001 ≈ 23.976 fps H.264 output | Acceptable diff |
| Encode | HEVC Main, ~8.9 Mbps | h264_nvenc -cq 23 / libx264 fallback | Acceptable diff |
| Audio | AAC stereo 44.1 kHz | Normalised audio in pipeline | MATCH |

---

### Outro Card

- **Duration**: ≈7.3 s (speech ends at t=45.04 s; total clip 52.38 s)
- **Style**: Cream/off-white static card (no video, no speaker)
- **Content** (text reveals as time progresses):
  - "VICI" — large luxury serif, centered, ≈48% from top
  - "If you want to go deeper into this," — italic serif, light weight
  - "we have a free research guide" — bold serif
  - "feel free to grab it, link in bio" — small regular
  - "→" arrow below
- **DIFF vs code**: The code's `_concat_outro` appends whatever is in `assets/peptides/outro.mov`.
  If the `outro.mov` for peptides is a different branded card, this outro style needs to match the
  reference card design. The reference card is a CTA (call-to-action), not a follow-prompt.
  **The concept (branded card at end) MATCHES; the specific content/style is campaign-asset-dependent.**

---

## STRUCTURAL TEMPLATE

### Full Transcript (from faster-whisper base, word-level)

**Language:** English | **Total audio duration:** 52.38 s | **Word count:** ≈158 words | **Pace:** ≈3.5 words/s = ≈210 wpm

```
[0.00–1.20]  "the best way to do it."
[1.32–2.52]  "If you're doing it for weight loss,"
[2.64–3.76]  "when you get to your weight,"
[3.84–5.82]  "which you will get to because they work very quickly"
[5.82–7.44]  "to control your weight,"
[7.86–9.38]  "rather than go off it,"
[9.62–11.52] "I think you just go on it less."
[12.14–12.80] "So let's see,"
[13.44–16.24] "let's see, you started out at 2.5 milligrams of zap bound."
[16.76–17.08] "And you,"
[17.22–18.36] "that didn't really control my,"
[18.38–19.20] "and you went up to five,"
[19.34–20.74] "you go, wow, five I'm not hungry"
[20.74–21.76] "and I'm really losing weight."
[21.88–22.88] "Okay, so you stand five,"
[23.12–24.06] "you lose all the weight"
[24.78–25.84] "and you feel like,"
[25.90–26.80] "okay, what do I do now?"
[26.96–30.26] "Maybe you go back down to 2.5 every other week."
[30.88–36.04] "And you kind of figure out your body's reaction to these things, okay?"
[36.60–38.24] "But I promise you,"
[38.40–39.26] "both of you guys,"
[39.74–41.22] "no matter how fit you get,"
[41.64–42.10] "in the future,"
[42.30–43.40] "you're gonna be on these trucks."
[43.92–44.38] "You're gonna be on the,"
[44.46–45.04] "everybody is,"
[45.04–52.38] [OUTRO CARD — no speech]
```

---

### Structural Pattern

**Start behavior — "Opens on the substance, not the setup"**

The clip begins at t=0 with "the best way to do it." — lowercase "the" indicates the source video
was cut mid-paragraph, entering at the moment the speaker gives their direct answer. The HOOK TEXT
("Dr Dubrow REVEALS The SECRET To Effective RETA Dosing") provides the framing context the viewer
needs; the spoken content does NOT repeat that context — it dives straight into the explanation.

Rule: **start exactly at the first word of the speaker's substantive answer/explanation, even if
that word is a lowercase connector.** Do NOT waste the first seconds on a question, a name drop, or
a "so as I was saying." The hook on-screen text compensates for the missing context.

**Point development — 3-beat arc**

| Beat | Time | Content |
|---|---|---|
| 1. Principle | 0–11.5 s | The core insight: "you just go on it less" (don't quit; titrate down) |
| 2. Concrete example | 12–36 s | A specific patient scenario: 2.5 mg → 5 mg → back to 2.5 mg EOW — listeners follow a real decision path |
| 3. Bold prediction / payoff | 36–45 s | "no matter how fit you get… you're gonna be on these drugs — everybody is" |

**End behavior — "Ends on the landing, not the fade"**

The clip cuts at t=45.04 s on "everybody is," — this is the moment the speaker's main thesis lands
as a universal, slightly provocative prediction. It is a complete thought. The trailing comma ("is,")
gives a natural pause after a bold statement — it reads as confidence, not incompleteness.

The clip does NOT run into the next sentence, into a laugh, or into a backtrack. It stops the
instant the payoff has been delivered. No outro audio — the branded card takes over silently.

---

## TIMING

| Metric | Value |
|---|---|
| Total clip duration | 52.38 s |
| Speech content | 0–45.04 s = **45.0 s** |
| Outro card | 45.04–52.38 s = **7.3 s** |
| Hook on-screen | 0–6.0 s ≈ **6 s** |
| Word count (speech) | ≈158 words |
| Pace | ≈210 wpm (comfortable podcast cadence) |
| Hook text length | 9 words + 2 emojis (in quotation marks) |

Clip length (45 s speech) falls within the `clip_length: [18, 60]` range in peptides.yaml. MATCH.

---

## STRUCTURAL EXEMPLAR (for ranker/gate prompts)

> Copy-paste this block verbatim into LLM prompts as the positive reference example.

```
GOLD-STANDARD CLIP EXAMPLE

Content: A clip of Dr. Terry Dubrow (cosmetic surgeon, podcast guest) explaining GLP-1 drug
maintenance strategy for weight loss — specifically how to titrate down instead of quitting.

STRUCTURE:
- Starts at: The exact first word of the doctor's direct answer — "the best way to do it" —
  even though this is mid-sentence in the source. The clip does NOT include the interviewer's
  question or any preamble.
- Opening hook: A bold, actionable claim implied by the opening words ("the best way to do
  it"). The on-screen hook text frames it as a "reveal," establishing stakes in the first
  1–2 seconds so a viewer who just scrolled past will stop.
- Development: The speaker walks through one concrete patient scenario with specific numbers
  (2.5 mg, 5 mg, every other week). Abstract principles are avoided; instead, the listener
  follows a real decision path they could imagine applying to themselves.
- Resolution: The clip ends on the speaker's closing prediction — "no matter how fit you get,
  you're gonna be on these drugs — everybody is." This is the PAYOFF: a confident,
  slightly provocative universal statement that crystallises the whole argument. It ends exactly
  here, mid-thought-comma, the moment the idea has fully landed.
- Total content: 45 seconds of speech. No runway, no outro voiceover. The argument is self-
  contained: a first-time viewer with no context understands the full point.

WHAT MAKES THIS CLIP PASS:
1. Starts at the OPENING of the substantive answer (t=0), not at a mid-sentence clarification.
2. Ends at the natural RESOLUTION of the argument (the universal prediction), not before it
   and not after it runs into the next topic.
3. The arc has a clear 3-beat shape: principle → concrete example → bold payoff.
4. Every sentence advances the point. There is no filler.

A clip that does NOT start at a claim/answer or does NOT end after the payoff lands is NOT
a structural match for this exemplar.
```

---

## MATCH / DIFF SUMMARY vs CURRENT RENDER LAYOUT

| Element | Code (`render/modal_app.py` constants + `campaigns/peptides.yaml`) | Reference | Status |
|---|---|---|---|
| Hook box color | `#FFFFFF` box, `#000000` text | White box, black text | **MATCH** |
| Hook box width | `boxborderw=30`, ~80–90% of W | ~83–84% of W | **MATCH** |
| **Hook center Y** | `HOOK_BOX_CENTER_Y_FRAC=0.52` → y=998 px | **y≈1130–1169 px (58.8–60.9%)** | **DIFF ≈130–171 px lower in reference** |
| **Hook duration** | `HOOK_SHOW_SECONDS=(0.0, 7.5)` / peptides.yaml `[0, 8]` | **≈6 s** | **DIFF: reference shorter by ≈1.5–2 s** |
| Caption words/event | `CAPTION_MAX_WORDS_PER_EVENT=3` | **1 word at a time** | **DIFF** |
| **Caption color** | base `#FFFFFF` + highlight `#00E5FF` (cyan) | **All white, no cyan** | **DIFF: no cyan in reference** |
| Caption top Y | `CAPTION_ZONE_Y_FRAC=0.65` → 1248 px | **≈1267–1325 px (66–69%)** | **DIFF ≈19–77 px lower; minor** |
| Watermark width | `WATERMARK_WIDTH_FRAC=0.30` → 324 px | **≈302–324 px** | **MATCH** |
| **Watermark bottom margin** | `WATERMARK_BOTTOM_MARGIN_FRAC=0.06` → 115 px from bottom | **≈211–249 px from bottom (11–13%)** | **DIFF: reference watermark ≈100 px higher** |
| **Watermark opacity** | `WATERMARK_MIN_OPACITY=0.85` | **Visually ≈65–75%** | **DIFF: reference more transparent** |
| Resolution | 1080×1920 | 1080×1920 | **MATCH** |
| Outro card present | Yes (via `outro.mov`) | Yes (branded CTA card ≈7.3 s) | **MATCH concept; content is campaign-asset-specific** |
| Speaker framing | YuNet face track → center crop 1080×1920 | Tight bust, speaker centered | **MATCH expected** |
| "Via @source" credit | Rendered above watermark | Not confirmed visible | **UNVERIFIED** |

### Priority fixes if aligning render to reference:
1. **HOOK_BOX_CENTER_Y_FRAC**: raise from 0.52 to ≈0.60 (or tune per campaign)
2. **Hook duration**: expose as campaign config; peptides reference suggests ≈6 s, not 8 s
3. **Caption words per event**: consider adding `1-word` style option (vs current 3-word + cyan)
4. **Watermark bottom margin**: consider raising `WATERMARK_BOTTOM_MARGIN_FRAC` to ≈0.11–0.13
5. **Watermark opacity**: consider lowering target/floor closer to 0.70 for peptides campaign
