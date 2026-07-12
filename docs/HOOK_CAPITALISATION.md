# HOOK_CAPITALISATION.md
# Strategic Capitalisation Engine for the Clipping System
# Owner: Bosi. Applies to every hook, on-screen overlay, caption, title and CTA the clipping system produces.
# This file is law. If a generated hook violates any rule below, regenerate it. Do not ship it.
#
# Enforcement in code: `core/hook_style.py` (audit_hook / sanitize_hook) — every
# LLM-generated hook is deterministically audited and repaired before it is
# stored on a Clip row. The ranking prompt in `core/llm.py` carries a distilled
# version of these rules so the model generates compliant hooks first time.

## 0. WHAT THIS IS

Strategic capitalisation is the deliberate uppercasing of one to three high-weight words inside an otherwise
lowercase or sentence-case hook, in order to force the eye to stop on the words that do the selling.

It is NOT title case.
It is NOT ALL CAPS.
It is NOT random emphasis.

Every capital letter is a currency. Spend it, and it must buy attention. If a cap does not earn its place,
it is stolen attention with nothing bought, and it makes the hook read as spam.

## 1. WHY IT WORKS (the research grounding, so the model never drifts)

1.1 PATTERN INTERRUPT
A viewer scrolls dozens of near-identical frames per minute. The brain is running a low-effort prediction
loop. Anything that breaks the expected visual flow triggers a pause response. A capitalised word inside a
lowercase sentence is a break in the expected flow. It is a speed bump for the eye.

1.2 THE VON RESTORFF / ISOLATION EFFECT
Items that are perceptually distinct from their surroundings are attended to more, encoded more strongly,
and recalled better than items that blend in. This is why ONE capitalised word inside a lowercase line pops.
It is also why FIVE capitalised words inside the same line do not pop. If everything is isolated, nothing is
isolated. The effect collapses to zero. This is the single most important mechanism in this file.

1.3 THE GLANCEABLE READING EXCEPTION
Classic typography research (Tinker, and modern replications) shows all-caps slows reading of continuous
text by roughly 10 to 20 percent, because uppercase words form uniform rectangular blocks and lose the
ascender and descender shape cues the eye uses for fast word recognition.
BUT: MIT AgeLab / NN-g research on "glanceable" reading found that for single-word, sub-second identification,
uppercase actually OUTPERFORMS lowercase.
This is the whole theoretical basis for the technique. A hook is a hybrid. It is a short line that must be
READ as a sentence but GLANCED at as a set of keywords. So:
  - Capitalise the words you want GLANCED (the payload words).
  - Leave lowercase the words you want READ (the connective tissue).
The caps carry the meaning at 0.4 seconds. The lowercase carries the meaning at 1.5 seconds. Both work.

1.4 THE CREDIBILITY COST
Excessive capitalisation is a known spam and clickbait signal. Meta's ad standards actively discourage caps
used to grab attention. Audiences pattern-match ALL CAPS hooks to low-trust content. So caps buy attention
at the cost of trust. One or two caps: attention gained, trust intact. Four-plus caps: attention gained,
trust burned. The whole game is staying on the right side of that line.

1.5 THE DECISION WINDOW
Roughly 71 percent of short-form viewers decide to watch or scroll within 1 to 3 seconds. The capitalised
words must therefore be readable and meaningful in isolation, because in the worst case the capitalised words
are the ONLY thing the viewer processes before deciding.
TEST: strip every lowercase word from the hook. Do the remaining capitalised words still communicate a
promise? If not, you capitalised the wrong words.

## 2. THE CORE RULE

CAPITALISE THE ACTION. CAPITALISE THE OUTCOME. CAPITALISE THE CALL TO ACTION. EVERYTHING ELSE STAYS LOWERCASE.

That is the entire rule. Everything below is enforcement detail.

## 3. WORD CLASS TABLE

WORDS THAT EARN A CAP (payload words):
  - The ACTION VERB, the thing being done. ENGINEER, AUTOMATE, HIJACK, DELETE, BREAK, STEAL, RIP, WEAPONISE,
    EXPOSE, KILL, REPLACE, BUILD.
  - The OUTCOME NOUN, the thing the viewer wants or fears. VIRALITY, REVENUE, LEADS, PURITY, MONEY, REACH,
    BANNED, FIRED, BROKE.
  - The CTA VERB, the instruction. STEAL, COPY, TAKE, SAVE, WATCH, TRY.
  - The EMOTIONAL PIVOT, the single word the entire hook turns on. NEVER, STOP, WRONG, NOBODY, FREE, ILLEGAL.
  - The CONTRARIAN NEGATOR when the hook is a myth-buster. "This is NOT random." "You DON'T need X."

WORDS THAT NEVER EARN A CAP (connective tissue):
  - Articles: a, an, the
  - Prepositions: to, for, in, on, with, from, at, of
  - Conjunctions: and, but, or, so
  - Auxiliaries: is, are, was, will, can, do, does
  - Pronouns: i, you, my, your, this, that, it
  - Filler adjectives with no emotional charge: good, nice, great, useful, better
  - The brand or product name. It uses its own house casing, always. Claude Code stays Claude Code.
    Vici stays Vici. Never CLAUDE CODE. A brand name in caps reads as a shouty advert, not a hook.
  - Numbers and figures. They are already high-salience by default. Capitalising around them is redundant.
  - Acronyms and technical tokens (FDA, TRT, GLP-1, BPC-157) keep their house casing. They neither earn
    nor spend a cap — they are invisible to the cap budget.

## 4. THE HARD CONSTRAINTS

4.1  MAXIMUM 2 capitalised words per hook. 3 is the absolute ceiling and only if the hook exceeds 10 words.
4.2  MINIMUM 1. A hook with zero caps is allowed only when the hook is deliberately deadpan or lowercase-cool.
     Flag it if so. Otherwise, one cap minimum.
4.3  CAP RATIO must not exceed 20 percent of total words. 2 caps in a 6-word hook is 33 percent. Too hot.
     2 caps in a 10-word hook is 20 percent. Correct.
4.4  NEVER capitalise two ADJACENT words. Adjacent caps merge into one visual block and the isolation effect
     dies. Caps need lowercase on both sides to breathe.
     WRONG: "STEAL THIS system"
     RIGHT: "STEAL this SYSTEM" (still two caps, but separated)
     BETTER: "STEAL this system" (one cap, cleaner)
4.5  NEVER capitalise two words that carry the SAME job. Do not have two outcome words fighting each other,
     or two CTA words fighting each other. One action, one outcome. That is the pair.
4.6  NEVER use a fully capitalised line. Not for a hook, not for an overlay, not for a caption.
4.7  NEVER use alternating case, mocking case, or random mid-word caps.
4.8  NEVER capitalise inside the body of a caption after the first line. Caps are a hook-layer tool only.
     Body copy is sentence case.
4.9  NO EM-DASHES anywhere in any output. Use full stops, commas or line breaks. This is an absolute rule
     across every system Bosi owns.
4.10 The capitalised words must survive the STRIP TEST in section 1.5.

## 5. THE BRACKET RULE

Many hooks end with a parenthetical CTA or payoff.

FORMAT: `[main hook with 1 cap] ([CTA with 1 cap])`

The bracket is a second, separate attention zone. It gets its own single cap.
So the hook body gets ONE cap, and the bracket gets ONE cap. That is the natural 2-cap structure, and the
brackets themselves provide the visual separation that stops the caps merging.

  RIGHT: `Use Claude Code to ENGINEER TikTok virality (STEAL This System)`
  WRONG: `USE CLAUDE CODE TO ENGINEER TIKTOK VIRALITY (STEAL THIS SYSTEM)`
  WRONG: `Use claude code to engineer tiktok VIRALITY (steal this system)`

## 6. PLACEMENT

6.1 Do NOT put the cap on the first word by default. The first word is already high-salience because the eye
    lands there first. Capitalising it wastes the cap. Put the cap at position 3 to 6, where the eye is
    already moving and needs a reason to stop.
    EXCEPTION: contrarian openers. "STOP doing this." "NEVER post this." Here the cap IS the interrupt and it
    belongs at position 1.
6.2 Put the second cap in the last 3 words. That is the exit position, where the viewer decides to keep
    watching or scroll. The CTA verb lives there.
6.3 Never put both caps in the same half of the line. Front-load one, back-load one. You are building two
    fixation points, not one.

## 7. HOOK LENGTH

  - On-screen overlay (first 3 seconds): 4 to 8 words. Hard cap around 30 to 40 characters. 1 cap. Sometimes 2.
  - Video title / TikTok caption line 1: 6 to 12 words. 1 to 2 caps.
  - Instagram caption first line: 6 to 14 words. 1 to 2 caps.
  - Body copy below line 1: sentence case, zero caps, one sentence per line, white space between lines.

## 8. THE SELECTION ALGORITHM (run this every single time)

  STEP 1. Tokenise the hook into words.
  STEP 2. Tag each word with a role: ACTION, OUTCOME, CTA, PIVOT, BRAND, NUMBER, or CONNECTIVE.
  STEP 3. Discard BRAND, NUMBER and CONNECTIVE from cap candidacy immediately.
  STEP 4. Score each remaining word on emotional and commercial weight, 0 to 10.
  STEP 5. Select the single highest-scoring word. That is CAP 1.
  STEP 6. If the hook has a bracket or a CTA clause, select the highest-scoring word inside it. That is CAP 2.
          Otherwise select the highest-scoring word in the OPPOSITE half of the line from CAP 1, but only if
          its score is 7 or above. If nothing scores 7 or above, ship with one cap.
  STEP 7. Check CAP 1 and CAP 2 do not do the same job. If they do, drop CAP 2.
  STEP 8. Check CAP 1 and CAP 2 are not adjacent. If they are, drop CAP 2.
  STEP 9. Check cap ratio is 20 percent or below. If not, drop CAP 2.
  STEP 10. Run the STRIP TEST. If it fails, go back to STEP 5 and pick differently.
  STEP 11. Run the ANTI-SPAM TEST. Read the whole hook out loud. If billboard, drop a cap.
  STEP 12. Ship.

## 9. WORKED EXAMPLES

INPUT:  growing on tiktok is not random
OUTPUT: `Growing on TikTok is NOT random.`

INPUT:  i automated my tiktok analytics inside claude code, one prompt, runs weekly by itself
OUTPUT: `I AUTOMATED my TikTok analytics inside Claude Code. One prompt. Runs every week by itself.`

INPUT:  most peptide brands never show you a certificate of analysis
OUTPUT: `Most peptide brands NEVER show you a certificate of analysis.`

INPUT:  here are 5 claude code skills that make money
OUTPUT: `5 Claude Code skills that actually MAKE money.`

FAILURE: `USE CLAUDE CODE To ENGINEER Your TIKTOK VIRALITY (STEAL THIS SYSTEM NOW)` — 6 caps, adjacent caps,
brand shouted, isolation collapsed, reads as a scam.

FAILURE: `use claude code to engineer tiktok virality (steal this system)` — zero caps, no fixation point.

## 10. AGENT OUTPUT CONTRACT

The deterministic validator in `core/hook_style.py` enforces the mechanical constraints (cap count, ratio,
adjacency, em dashes, connectives, all-caps lines). Reject/repair if any of:
  cap_count > 3
  cap_ratio > 0.20 (when 2+ caps)
  adjacent_caps == true
  em_dash_present == true
  any capitalised word is a BRAND, NUMBER or CONNECTIVE
The semantic checks (strip test, role tagging) are enforced in the generation prompt.

## 11. A/B TESTING PROTOCOL

For every hook concept, three variants: V1 one cap on the action verb; V2 two caps action + CTA;
V3 zero caps deadpan. Post across different clips, log 3-second retention / watch-through / profile visits.
After 20 posts, weight future generation toward the winning CAP PATTERN, not the winning hook.

## 12. NON-NEGOTIABLES SUMMARY

  1. Caps are a currency. Every cap must be bought with attention.
  2. One or two caps. Never more. Never zero by accident.
  3. Action. Outcome. CTA. Nothing else.
  4. Never adjacent. Never the brand. Never a number. Never a connective.
  5. Strip the lowercase. If the caps alone do not sell, you capped the wrong words.
  6. No em-dashes. Ever.
  7. If it reads like a billboard, remove a cap. If it disappears in the feed, add one.

---

## APPENDIX — WEB RESEARCH ADDENDUM (verified 2026-07-12)

Live research run against 2024-2026 sources. Findings that confirm, extend, or nuance the rules above:

1. **NN/g glanceable-fonts data confirmed** — uppercase beat lowercase by 26% for isolated sub-second word
   recognition (p<0.01), BUT the mechanism is partly character AREA: uppercase words are visually bigger.
   Implication: the render layer amplifies the cap by weight/size (Montserrat ExtraBold already does this).
2. **2024 eye-tracking (ACM ETRA, arxiv 2404.05572)** — highlighted text reliably attracts longer fixations
   (Von Restorff confirmed), but over-highlighting was rated "distracting" and did not improve comprehension.
   Attention captured ≠ meaning conveyed. Reinforces the 1-2 cap ceiling.
3. **Color highlight beats caps for dwell time** — practitioner consensus (Opus/Captions.ai, the "Hormozi
   style"): 1-2 color-highlighted words per line outperform caps alone. Our karaoke captions already use the
   cyan current-word highlight; hooks use caps. Do not stack both emphasis systems on the same word zone.
4. **Meta enforcement is harder than "discouraged"** — excessive caps in ad creative is an automated
   REJECTION trigger on Meta, not just a style penalty. Treat >2 caps as a compliance risk for any clip that
   may later be boosted.
5. **No algorithmic caps penalty found on TikTok/YouTube** (2025-2026 sources) — the risk is user-side trust,
   not distribution. TikTok culture tolerates full caps only for sub-7-word standalone overlay titles; our
   hooks are full sentences, so sentence-case + selective caps remains correct.
6. **Authenticity trend (2025-2026)** — raw/unpolished styling wins; heavy text treatment reads corporate.
   Selective caps inside sentence case matches the trend; ALL CAPS does not.
7. **Accessibility** — screen readers can spell out all-caps words letter-by-letter. One more reason the cap
   budget stays at 1-2 words.
