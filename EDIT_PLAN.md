# John / Nani Edit Plan

Status: **PLAN ONLY — NOT APPROVED FOR EXECUTION**  
Prepared: 2026-07-20  
Working directory: `/root/projects/clip-engine`  
Dropbox scope inspected: `dropbox:John/`  

No Dropbox/source media has been edited, rendered, moved, renamed, deleted, or uploaded in this pass. Read-only copies and still review frames were created only under `/tmp` to identify Nani's visual references; they are not deliverables. The only project artifact changed is this plan.

## Approval checklist

- [ ] Approve Nani's extracted rules below.
- [ ] Answer the open questions at the end.
- [ ] Make the Bosi executable toolkit available at its documented path before an approved execution batch.
- [ ] Approve one batch at a time before any production work begins.

## Source and method record

- Source of truth read in full twice: `/root/Nani- John Meeting.docx` (all 1,321 paragraphs; meeting length 121 minutes). The second pass specifically re-checked the May 9 sequence, cover pairings, audio, captions, and delivery rules.
- Visual-reference recording: `/root/Impromptu Google Meet Meeting - Jul 19 2026.mp4` (2:00:49.933, 1280x720 H.264/AAC). Only transcript-directed moments were sampled: the finals/still discussion, SuitSupply selections, podium/event selections, exact cover-pairing interval, office/rooftop selections, friend-group/team selections, and loafer selection.
- Dropbox tree walked in full: 93 files total.
  - `dropbox:John/John Finals/`: 17 files — 3 videos and 14 stills.
  - `dropbox:John/John Raws/`: 76 files — 57 videos and 19 stills.
- Bosi skill package read in full: `/root/bosi-editor-skills/VIDEO_EDITOR.md`, `/root/bosi-editor-skills/PLANNING_GUIDELINES.md`, and `/root/bosi-editor-skills/BROLL_KNOWLEDGE.md` (903 lines total).
- Execution preflight finding: the skill documents point to `/root/bosi-editor/edit.py` and supporting scripts, but `/root/bosi-editor/` is not currently present. The plan is mapped to the documented Bosi capabilities, but execution remains blocked until those scripts are installed or their real path is supplied.
- Exact visual checks now locked:
  - `Video May 09 2026, 4 49 28 PM.mov` is the same depicted clip retained locally as `/root/video-engine/imports/john-content-campaign/source/IMG_2330.MOV` (5.085 seconds): John in the light suit during the fitting-room adjustment.
  - `IMG_2451.MOV` uses `59B54D2C-48AF-4363-831F-FDAB5D965827.jpg` as its cover.
  - `c12b67e441434051a4fed48671a988e7.MP4` uses `3D47B365-E6FE-46F3-9689-02C4F81D88D4.JPG` as its cover.
- The previously supplied Emmanuel Bar skill informed the verification posture used here: instructions were checked against the transcript, meeting visuals, and current Dropbox state; contradictions are raised rather than guessed through.

## Deliverable count

### Confirmed work

- **15 content packages**.
- **19 delivery files**:
  - 2 ordered static carousel slides.
  - 2 non-destructive cover-still delivery copies.
  - 15 video files.
- **5 self-contained production batches** for the confirmed files.

This is the media-deliverable count. If companion `.txt` files are chosen instead of Dropbox comments for posting captions, those sidecars will be additional delivery files and will be counted after the caption-storage convention is approved.

| Confirmed output type | Count |
|---|---:|
| Ordered still/carousel slides | 2 |
| Cover-still delivery copies | 2 |
| Clean or packaging-only video variants | 2 |
| Text-led short reels (not speech captions) | 10 |
| Cinematic montage/recap reels | 2 |
| Award reel with isolated source-name/applause audio | 1 |
| **Confirmed total** | **19** |

The required clean/raw office source and Nani's five-second rooftop select are already present in `John Raws`; no new raw-select export is counted. The working-on-laptop packaging copy and office clean client variant are counted because Nani assigned the former an exact cover and asked that reusable clean versions remain available without replacing the raws.

### Contingent speaking work

- **1 additional blocked batch** for the speaking reels.
- Nani's rule is two topics per outfit and two versions per topic, so the count is **4 video files per outfit**.
- Per outfit, that is 2 clean/no-caption speaking variants and 2 captioned/filler-enhanced speaking variants.
- Let `N` be the number of outfits. The speaking batch adds `4N` files, and the campaign total becomes **`19 + 4N` delivery files**.
- `N` cannot be determined because the promised speaking-outfit files are not identifiable in the current Dropbox tree.

## Technical capability map

Every executable edit below points to a capability explicitly documented in the installed Bosi skill package. `BVE-READY` means documented; it does not mean the missing `/root/bosi-editor/` scripts have passed an execution preflight.

| ID | Bosi capability | Skill evidence |
|---|---|---|
| BVE-01 | Planning-first `plan` then approved `execute`; machine-readable cut list; no improvisation after approval | `VIDEO_EDITOR.md` Planning-First Workflow; `PLANNING_GUIDELINES.md` pipeline and JSON schema |
| BVE-02 | Timestamped silence/filler/source cuts and assembly through the FFmpeg-backed plan executor | `PLANNING_GUIDELINES.md` §§B, G; `VIDEO_EDITOR.md` toolkit/workflow |
| BVE-03 | Faster-whisper transcription with word-level timestamps | `VIDEO_EDITOR.md` transcription workflow |
| BVE-04 | Burned TikTok/Instagram speech captions after cuts: white bold, 55 pt, black outline 3, `MarginV=338`, one-word cue preferred | `VIDEO_EDITOR.md` Caption style rules |
| BVE-05 | KineticText/on-screen text rendered in Remotion and composited over video | `VIDEO_EDITOR.md` Motion graphics and composite pattern |
| BVE-06 | 1080x1920 9:16 TikTok/Instagram export, H.264 CRF 18, `+faststart` | `VIDEO_EDITOR.md` Export format rules |
| BVE-07 | Contextual B-roll/filler plan from a speech transcript and FFmpeg composition, with clips spaced at least 8 seconds apart | `PLANNING_GUIDELINES.md` §§C-D; `BROLL_KNOWLEDGE.md` |
| GAP-S1 | Static JPEG quote-card/postcard rendering is not documented | Separate approved still-design method required |
| GAP-H1 | Generative hair reshaping/removal is not documented | Use Nani's cut-around fallback |
| GAP-D1 | Writing Dropbox file comments is not a video-editor capability | Delivery mechanism must be confirmed |
| GAP-T1 | `/root/bosi-editor/` executable toolkit is absent | Install it or provide its real path before execution |

### Binding export rules for all videos

- [ ] Deliver vertical 9:16 at 1080x1920 unless Nani approves another format.
- [ ] Encode H.264 at CRF 18 with `+faststart` using the Bosi TikTok/Instagram preset (BVE-06).
- [ ] Record every cut as an exact timestamp in the approved Bosi plan before execution (BVE-01/BVE-02).
- [ ] Preserve the source image character. Do **not** add filters, dehaze, denoise, sharpen, or attempt to “fix” existing fogginess.
- [ ] Do not add a digital zoom unless the executable Bosi toolkit is first shown to support that planned move; no generic zoom capability is claimed by the installed documents.
- [ ] Do not use an out-of-focus range merely because it is present in a source.
- [ ] Do not delete or overwrite any raw or existing final. New deliverables are non-destructive new files.

## Nani's extracted rules

These are the operating rules extracted from the transcript. Timestamps identify where Nani stated or demonstrated them.

### Folder, RAW, naming, and caption rules

- [ ] Finished work goes to the existing plural path `dropbox:John/John Finals/`. The user locked this convention on 2026-07-20; do not create a near-duplicate singular folder.
- [ ] Selected/raw material goes to John's raw area (Nani at 4:07 and 25:41). The current real path is `dropbox:John/John Raws/`.
- [ ] Names must be client-readable and describe what the post/video is or what its caption should be; do not leave opaque camera names on delivered files (7:55-9:34).
- [ ] Save the posting caption with the file so the client can copy/paste it (8:43-9:34).
- [ ] When a specific Instagram caption applies, attach that exact caption (9:34).
- [ ] Nani later asks for the caption to be copied into the destination-side Dropbox comments (50:19). Whether comments or sidecars are the final mechanism is an open question.
- [ ] Use ordered prefixes for paired versions: `1A`, `1B`, then `2A`, `2B`, etc., so Dropbox sorts them together (1:49:46-1:50:29).
- [ ] Never wipe, delete, replace, or broaden access beyond the shared folders (1:51 onward). Existing duplicates remain untouched.

### Still and cover rules

- [ ] Convert selected still concepts into an image-then-quote sequence: photo first, quote/postcard second (1:04-2:01, 11:26-14:05).
- [ ] “Discover your confidence” needs a companion quote about discovering inner confidence (11:26-14:05, 24:38).
- [ ] Some photos are straight standalone posts rather than quote cards; do not automatically put copy over every still (22:21-23:03).
- [ ] The three-second “It starts with you” reel uses `Cover photo for “it starts with” reel .jpg` as its exact cover (16:48-18:27).
- [ ] `Cover photo for inspire video .jpg` is the selected cover for `Inspire speaker vid .mov` (23:35-24:21).
- [ ] The meeting's later “this video with this cover photo” pairings are now identified as:
  - `dropbox:John/John Raws/IMG_2451.MOV` → `dropbox:John/John Raws/59B54D2C-48AF-4363-831F-FDAB5D965827.jpg`.
  - `dropbox:John/John Raws/c12b67e441434051a4fed48671a988e7.MP4` → `dropbox:John/John Raws/3D47B365-E6FE-46F3-9689-02C4F81D88D4.JPG`.
- [ ] The meeting did **not** visibly or verbally bind a specific still to the funny-podium or award reel; those covers must not be guessed from the May 14 still pool.
- [ ] Several later stills/carousels were declared already final; no new image editing is authorized for them (1:19:31-1:23:00).

### Creative direction

- [ ] Overall presentation is luxurious, masculine, polished, and personal-speaker oriented; Nani cites Afnan as the style reference (28:08-29:55).
- [ ] Non-speaking reels carry commanding masculine energy; speaking reels reveal John's kindness (29:24-29:55).
- [ ] Hard bass was Nani's original direction, but the user has deferred all background music. Current planned exports contain no added music; the user will add/select audio later.
- [ ] A preparation sequence can move from John in the suit, to the suit being fixed, to John speaking on stage (28:48).
- [ ] Event recaps should feel elegant/luxurious, change frames quickly, remain captivating, and not run long (57:43 onward).
- [ ] Funny content must stay cute and must not insult short people (54:32-57:07).

### Hard “do not touch” and exclusion rules

- [ ] **Hard constraint:** do not change fogginess, source material/texture, or add filters to any of these videos (53:24).
- [ ] Zoom in/out is allowed for action, but it is not permission to grade or materially alter the footage (53:24).
- [ ] Do not use unfocused portions; a Vivid filter made blur worse and was removed during the meeting (1:15:00-1:16:52).
- [ ] Exclude the independent funny podium clip from the black-suit event recap (1:06:41-1:08:27).
- [ ] Ignore/exclude the footage focused on the woman giving dirty looks (1:08:44 onward).
- [ ] Text on the friend-group clip must not cover John's suit; test the side area versus the area around the shoes (1:42:49).
- [ ] Do not alter already-final carousels/stills unless an item below explicitly names a new derivative.

### Two-version rule

- [ ] Every speaking topic needs:
  - `A`: clean edited video with **no captions and no filler pictures**.
  - `B`: the full final with captions and relevant filler pictures.
- [ ] Each outfit contains two separate speaking topics, so each outfit produces four files: Topic 1 A/B and Topic 2 A/B (1:47:53-1:50:29, 1:55:00-1:55:15).
- [ ] The office/Pollyanna clip separately needs a clean reusable copy and a text-overlay final (1:23:43-1:27:00).

### Audio and caption rules for this execution plan

- [ ] Do not add background music in any current batch. Music is not an execution blocker; produce silent/music-ready non-speaking exports unless a source sound is explicitly essential.
- [ ] JN-08 is the exception: retain only the verified announcer phrase containing `John Victoria` and the applause around it.
- [ ] Full speech captions apply mainly to the forthcoming speaking reels. Use the user's consistent normal white-caption direction via BVE-04; do not add flashy caption styling.
- [ ] Non-speaking reels receive only the specific hook/title text Nani requested, not full speech-style captions.

## Batch 1 — Studio foundation: quote carousel and micro reel

Batch output: **2 content packages / 3 files**.  
Batch done condition: the ordered two-slide confidence post and the three-second silent reel exist as new, client-readable files in the approved final folder; existing source/final files remain unchanged; approved posting copy is attached using the approved caption-storage method.

### JN-01 — Discover Your Confidence, image then quote

- [ ] **Source:** `dropbox:John/John Finals/Discover your confidence   AM.jpg`
- [ ] **Type:** two-slide static quote post/carousel.
- [ ] **Output count:** two still files.
- [ ] **Version/order:**
  - Slide 1: the source photograph unchanged.
  - Slide 2: a matching postcard-style quote card.
- [ ] **Edit sequence:**
  1. Make a non-destructive ordered delivery copy of the photograph; no retouching or filter.
  2. Build the quote card in the same visual family, with the approved inner-confidence quote.
  3. Confirm the pair reads photo first, quote second.
- [ ] **Capability:** slide 1 is a non-destructive delivery copy; slide 2 remains **GAP-S1** because the Bosi package does not document static JPEG quote-card rendering.
- [ ] **Hard constraints:** no edit to John's photograph; do not invent the quote.
- [ ] **Caption source:** Nani specified only the theme: “discovering your inner confidence.” Exact quote and Instagram caption require approval.
- [ ] **Exact outputs:**
  - `01A - Discover Your Confidence - Photo.jpg`
  - `01B - Discover Your Confidence - Quote.jpg`
- [ ] **Destination:** `dropbox:John/John Finals/`.
- [ ] **Definition of done:** two correctly ordered stills form a photo-then-quote post, with approved quote/caption available to copy and no alteration to the source photo.

### JN-02 — It Starts With You

- [ ] **Source video:** `dropbox:John/John Finals/It starts with you .mov` (verified 3.063 seconds and currently has no text).
- [ ] **Cover:** `dropbox:John/John Finals/Cover photo for “it starts with” reel .jpg`.
- [ ] **Type:** silent text-led micro reel.
- [ ] **Output count:** one video.
- [ ] **Edit sequence:**
  1. Preserve the full approximately three-second source as a zero-cut Bosi plan (BVE-01/BVE-02).
  2. Mute/remove source sound, as Nani explicitly requested no sound (BVE-02/FFmpeg audio timeline).
  3. Place exact on-screen text `It starts with you.` in the open upper area without covering John's face (BVE-05).
  4. Export and verify the vertical social preset (BVE-06).
- [ ] **Hard constraints:** no filter, fogginess/material change, or beauty retouch; exact cover only.
- [ ] **Version rule:** one version; no captioned/uncaptioned pair requested.
- [ ] **Posting caption:** not specified; on-screen text is specified.
- [ ] **Exact output:** `02 - It Starts With You - Final.mp4`
- [ ] **Destination:** `dropbox:John/John Finals/`.
- [ ] **Definition of done:** a 3.063-second silent reel displays the exact phrase cleanly and is paired with Nani's exact cover.

## Batch 2 — SuitSupply preparation series

Batch output: **3 video files**.  
Batch done condition: all three distinct SuitSupply concepts are exported with their locked source order/cuts, approved copy, no added music, and no image-material changes; the remaining ambiguous timecodes must be resolved before this batch starts.

### JN-03 — A Day With SuitSupply in Preparation for the APAICS Panel

- [ ] **Type:** non-speaking luxurious BTS montage.
- [ ] **Confirmed source order and cuts:**
  1. `dropbox:John/John Raws/Video May 09 2026, 4 17 13 PM.mov` — exterior storefront, `00:00.00-00:01.00`.
  2. `dropbox:John/John Raws/Video May 09 2026, 4 29 40 PM.mov` — coffee being made, `00:00.00-00:01.00`.
  3. `dropbox:John/John Raws/Video May 09 2026, 4 31 27 PM.mov` — white-suit/red-mannequin frame, transcript says `00:00-00:00.2`; confirm whether Nani meant 0.2 seconds or “through second 2.”
  4. `dropbox:John/John Raws/Video May 09 2026, 4 32 21 PM.mov` — framed menswear picture, full 3.208-second clip.
  5. `dropbox:John/John Raws/Video May 09 2026, 4 29 54 PM.mov` — curtain/fitting-room action, transcript points to approximately `00:04.00` through the very beginning of `00:06.00`.
  6. Meeting/Dropbox name: `dropbox:John/John Raws/Video May 09 2026, 4 49 28 PM.mov` — John in the light suit during the fitting-room adjustment; absent under this renamed Dropbox path, but visually matched to the retained local original `/root/video-engine/imports/john-content-campaign/source/IMG_2330.MOV` (5.085 seconds). Use the local original for this beat only after the batch is approved; do not substitute another May 9 clip.
- [ ] **Available supporting inserts, not replacements:**
  - `dropbox:John/John Raws/Video May 09 2026, 4 44 14 PM.mov` — fit adjustment.
  - `dropbox:John/John Raws/Video May 09 2026, 4 53 41 PM.mov` — light-suit finishing/mirror moment.
  - Do not add these to the locked six-shot sequence unless Nani separately approves them.
- [ ] **Edit sequence:** record the locked exterior → coffee → mannequin → picture → fitting-room/John trims in the Bosi JSON plan, then execute the timestamped cuts and concat (BVE-01/BVE-02); export Bosi vertical (BVE-06). Do not add an undocumented zoom.
- [ ] **Hard constraints:** mute room audio; no background music; no filters/fogginess changes; do not substitute a different clip for the verified local `IMG_2330.MOV` beat.
- [ ] **Version rule:** one final version.
- [ ] **Specified title/caption source:** `A Day With SuitSupply in Preparation for the APAICS Panel`. Nani also identifies the event as the APAICS Legislative Leadership Summit and mentions “Celebrating 250 Years, Building America's Future Together”; exact public wording must be confirmed.
- [ ] **Audio:** silent/music-ready export; the user will add/select music later.
- [ ] **Exact output:** `03 - A Day With SuitSupply - APAICS Panel Preparation - Final.mp4`
- [ ] **Destination:** `dropbox:John/John Finals/`.
- [ ] **Definition of done:** the approved ordered montage moves cleanly from storefront to preparation to John, uses the correct event wording, and contains no unauthorized grading or substitute footage.

### JN-04 — Custom Suit Sets the Stage loop

- [ ] **Source:** `dropbox:John/John Raws/Video May 09 2026, 4 53 41 PM.mov` (3.750 seconds; the clip on screen when Nani introduced the loop concept).
- [ ] **Type:** fast loop/text reel.
- [ ] **Edit sequence:** preserve the strongest complete action; record only matching-motion loop trims in the approved Bosi plan (BVE-01/BVE-02); add the approved statement as restrained on-screen text (BVE-05); export Bosi vertical (BVE-06).
- [ ] **Hard constraints:** keep it fast; no fade that exposes the loop; no image filter or texture change.
- [ ] **Version rule:** one final version.
- [ ] **Transcript copy draft:** `To all of my men and future speakers: one of your biggest assets is a custom suit. It sets the stage before you open your mouth.` Nani explicitly asked for a stronger viral rendition, so this is not locked copy.
- [ ] **Audio:** silent/music-ready export; the user will add/select music later.
- [ ] **Exact output:** `04 - A Custom Suit Sets the Stage - Loop - Final.mp4`
- [ ] **Destination:** `dropbox:John/John Finals/`.
- [ ] **Definition of done:** a seamless short loop carries the approved suit statement, reads at phone size, and preserves the source look.

### JN-05 — Preparation Is Key

- [ ] **Primary source:** `dropbox:John/John Raws/Video May 09 2026, 5 28 53 PM.mov` (35.723 seconds; John in a black suit at the mirror beside red mannequins).
- [ ] **Possible payoff source shown during the discussion:** `dropbox:John/John Raws/Video May 12 2026, 9 16 50 AM.mov` (APAICS panel footage). Whether it belongs at the end is an open question.
- [ ] **Related alternate:** `dropbox:John/John Raws/Video May 09 2026, 5 25 27 PM.mov` (45.685 seconds); do not substitute it unless approved.
- [ ] **Type:** non-speaking preparation/investing-in-yourself reel.
- [ ] **Transcript-directed provisional keep ranges:**
  - `00:03.00-00:05.00`
  - `00:08.00` through the end of second 9
  - the end of second 11 through second 15
  - `00:22.00-00:33.00`
  - Exclude seconds 5-7, 10 through most of 11, and 16-22.
- [ ] **Edit sequence:** resolve exact boundary frames; record and execute the timestamped trim/concat plan (BVE-01/BVE-02); create momentum with the approved hard cuts; render approved on-screen copy (BVE-05); export (BVE-06). Omit zooms unless the executable Bosi toolkit later proves a supported zoom operation and the approved plan is amended.
- [ ] **Hard constraints:** no filter, dehaze, denoise, sharpening, or source-material change; do not include blurry opening frames.
- [ ] **Version rule:** one final version.
- [ ] **Transcript caption draft:** `I am a firm believer in the idea that preparation is key. Investing in resources such as a stylist and brand specialist helps make sure your presence captivates and your message is delivered with a lasting impression. Today I partnered with luxury brand specialist Nani Rosen of Rosen Relations to get the job done.` Nani composed this live and asked for polish; final wording needs approval.
- [ ] **Audio:** silent/music-ready export; the user will add/select music later.
- [ ] **Exact output:** `05 - Preparation Is Key - SuitSupply - Final.mp4`
- [ ] **Destination:** `dropbox:John/John Finals/`.
- [ ] **Definition of done:** only the approved ranges remain, pacing feels energetic, copy is approved, and the original visual material/fogginess is untouched.

## Batch 3 — Black-suit leadership event

Batch output: **3 video files**.  
Batch done condition: one concise black-suit recap, one independent funny podium reel, and one 5-7-second award announcement/walk-up reel are delivered; all blurry/dirty-look footage is excluded; the funny clip is absent from the recap.

### JN-06 — Black-Suit Leadership Event Recap

- [ ] **Source pool:** all in-focus John moments in the May 14 event set listed in the inventory appendix, including `IMG_2400.MOV` and the award moment from `Video May 14 2026, 1 58 20 PM.mov`; May 14 stills remain cover candidates only, not assumed mappings.
- [ ] **Type:** luxurious, elegant event recap.
- [ ] **Target duration:** 12-18 seconds, satisfying Nani's “not too long” instruction.
- [ ] **Edit sequence:** shortlist only in-focus John/black-suit actions; arrange a clear arrival/presence → podium/event → award/group payoff; record and execute exact cuts in the approved Bosi plan (BVE-01/BVE-02); export (BVE-06). Use fast shot changes for energy; do not claim an undocumented zoom.
- [ ] **Hard constraints:** exclude `Independance funny vidAM.mov`; exclude any section centered on the woman giving dirty looks; exclude every out-of-focus interval; no Vivid or other filter; no fogginess/material change.
- [ ] **Version rule:** one recap version.
- [ ] **Caption:** no exact Instagram caption supplied; source is Nani's event-recap/luxury direction.
- [ ] **Audio:** silent/music-ready export for this pass; the user will add the luxury track later.
- [ ] **Exact output:** `06 - John Black Suit Leadership Event Recap - Final.mp4`
- [ ] **Destination:** `dropbox:John/John Finals/`.
- [ ] **Definition of done:** a short polished recap contains only sharp, flattering John/event moments, includes the award beat, and respects both explicit exclusions.

### JN-07 — Independent Funny Podium Reel

- [ ] **Primary source:** `dropbox:John/John Raws/Independance funny vidAM.mov`.
- [ ] **Cover:** none locked. The recording did not bind a May 14 still to this funny reel; do not guess one.
- [ ] **Type:** independent funny podium reel.
- [ ] **Edit sequence:** isolate the concise podium beat in the approved timestamped plan (BVE-01/BVE-02); add one approved joke as simple readable on-screen text (BVE-05); export (BVE-06).
- [ ] **Hard constraints:** do not include this footage in JN-06; joke must be cute and not insulting; no filter/fogginess change.
- [ ] **Version rule:** one final version.
- [ ] **Copy options stated by Nani:** `Anyone have a chair I could borrow?` or `Comment what I said if you can read my lips.` Choose one only after approval.
- [ ] **Audio:** silent/music-ready export for now. The failed song reference is no longer a blocker because the user will add/select audio later.
- [ ] **Exact output:** `07 - Anyone Have a Chair I Could Borrow - Funny Podium - Final.mp4` (rename if the second copy option wins).
- [ ] **Destination:** `dropbox:John/John Finals/`.
- [ ] **Definition of done:** the standalone podium joke lands quickly, remains kind, and is never duplicated into the event recap.

### JN-08 — John Victoria Award Announcement and Walk-Up

- [ ] **Long source:** `dropbox:John/John Raws/Video May 14 2026, 1 58 20 PM.mov` (meeting visual confirmed the award walk-up/receipt inside this source).
- [ ] **Short award-pose source:** `dropbox:John/John Raws/IMG_2400.MOV` (7.033 seconds; John holding the award with presenters).
- [ ] **Type:** 5-7-second award/name reel; also contributes one beat to JN-06.
- [ ] **Edit sequence:** transcribe the long source with word timestamps (BVE-03); locate the clean announcer phrase containing `John Victoria` plus applause; record the matching audio/video range and 5-7-second walk-up/award cut in the approved plan (BVE-01/BVE-02); silence all other source audio; export (BVE-06).
- [ ] **Hard constraints:** do not use out-of-focus ranges; no Vivid/filter; no invented award title or “recipient of…” wording without a verified source line.
- [ ] **Version rule:** one independent version plus a reused visual beat in the recap; that reuse is not counted as another deliverable.
- [ ] **Caption:** only `John Victoria` is locked from Nani's request; official award title is not provided.
- [ ] **Cover:** none locked. The recording's demonstrated cover pairs were for `IMG_2451.MOV` and the staircase reel, not this award reel.
- [ ] **Audio:** retain only the verified announcer phrase containing `John Victoria` and the associated applause; silence all other source audio and add no music.
- [ ] **Exact output:** `08 - John Victoria Award Announcement and Walk-Up - Final.mp4`
- [ ] **Destination:** `dropbox:John/John Finals/`.
- [ ] **Definition of done:** a 5-7-second sharp clip audibly lands on `John Victoria` and applause, then shows the approved walk-up/award action with no blur or unsupported claim.

## Batch 4 — Work, motion, and rooftop positioning

Batch output: **4 content packages / 7 delivery files** (5 videos and 2 cover-still copies).  
Batch done condition: the laptop reel is packaged with Nani's exact cover, the office clip has clean and text versions, the staircase reel uses its exact cover and approved intentional-motion copy, and the rooftop reel uses approved AI/cubicle copy; every source remains intact and no music is added.

### JN-09 — John Working on Laptop, cover-paired independent reel

- [ ] **Source:** `dropbox:John/John Raws/IMG_2451.MOV` (19.553 seconds; John in black working on a laptop on the gold banquette).
- [ ] **Exact cover:** `dropbox:John/John Raws/59B54D2C-48AF-4363-831F-FDAB5D965827.jpg` (the May 26 9:40 AM still shown when Nani said the picture was “fire”).
- [ ] **Type:** packaging-only independent work/laptop reel.
- [ ] **Edit sequence:** preserve the full source as a zero-cut approved Bosi plan unless Nani later supplies a specific trim (BVE-01/BVE-02); mute source sound; export the vertical delivery copy (BVE-06); pair it with the exact cover.
- [ ] **Hard constraints:** no filter, brightness change, fogginess change, text overlay, or invented Pollyanna claim; this is not the later desk/meeting clip.
- [ ] **Version rule:** one clean version.
- [ ] **Caption:** none specified; captions are being held mainly for speaking content.
- [ ] **Audio:** silent/music-ready export.
- [ ] **Exact outputs:**
  - `09 - John Working - Independent Reel - Final.mp4`
  - `09 - John Working - Cover.jpg` — non-destructive delivery copy of the exact `59B...jpg` source.
- [ ] **Destination:** `dropbox:John/John Finals/`.
- [ ] **Definition of done:** the full clean laptop reel is available under a client-readable name and is paired with Nani's exact `59B...jpg` cover, with both raw files unchanged.

### JN-10A / JN-10B — Pollyanna SEO and AI meeting

- [ ] **Source:** `dropbox:John/John Raws/IMG_2859.mov` (3.135 seconds; John at his desk in a meeting). An identical-size copy currently exists at `dropbox:John/John Finals/IMG_2859.mov`; neither copy is to be replaced or deleted.
- [ ] **Type:** silent office brand reel with clean and text versions.
- [ ] **Output count:** two videos.
- [ ] **A edit sequence:** preserve the current Nani-adjusted visual exactly as a zero-cut Bosi plan; create a client-readable clean delivery copy without captions or filters (BVE-01/BVE-02/BVE-06).
- [ ] **B edit sequence:** use the exact same picture timing; add approved text in the open area above John's head (BVE-05); export (BVE-06).
- [ ] **Hard constraints:** Nani already lowered brightness during the meeting; do not grade it again; no filter/fogginess/material change.
- [ ] **Caption source:** the overlay should connect SEO research, AI, and John's company Pollyanna. Nani supplied the topic but no exact words; company claims must be verified before copy is locked.
- [ ] **Audio:** mute both versions; no music.
- [ ] **Exact outputs:**
  - `10A - John - Pollyanna SEO and AI - Final No Cap.mp4`
  - `10B - John - Pollyanna SEO and AI - Final.mp4`
- [ ] **Destination:** `dropbox:John/John Finals/`; the original reusable clean raw remains in `dropbox:John/John Raws/`.
- [ ] **Definition of done:** A and B match frame-for-frame, A is clean, B has approved readable Pollyanna/SEO/AI copy above John's head, and both preserve Nani's existing adjustment.

### JN-11 — Motion Doesn't Always Mean Progress

- [ ] **Source:** `dropbox:John/John Raws/c12b67e441434051a4fed48671a988e7.MP4` (4.200 seconds; John descending the indoor hotel stairs).
- [ ] **Exact cover:** `dropbox:John/John Raws/3D47B365-E6FE-46F3-9689-02C4F81D88D4.JPG` (John standing at the top of the same staircase on a phone call).
- [ ] **Type:** short text-led thought-leadership reel.
- [ ] **Edit sequence:** retain the full clean action in the approved Bosi plan (BVE-01/BVE-02); add approved text in the open area (BVE-05); export (BVE-06); pair the exact cover.
- [ ] **Hard constraints:** no filter/fogginess/material change; keep copy readable without obscuring John.
- [ ] **Version rule:** one final version.
- [ ] **Transcript-derived draft:** `Motion doesn't always mean progress. Make every move intentional.` Nani described this as “something along the lines of,” so final wording needs approval.
- [ ] **Audio:** silent/music-ready export.
- [ ] **Exact outputs:**
  - `11 - Motion Doesn't Always Mean Progress - Final.mp4`
  - `11 - Motion Doesn't Always Mean Progress - Cover.jpg` — non-destructive delivery copy of the exact `3D47...JPG` source.
- [ ] **Destination:** `dropbox:John/John Finals/`.
- [ ] **Definition of done:** the full stair action carries one approved, legible intentional-motion message, uses Nani's exact cover, and has no visual regrading.

### JN-12 — Rooftop AI / Cubicle reel

- [ ] **Preferred source:** `dropbox:John/John Raws/26e1e46b92bc4adcb473bd8fd3541b14.MP4` (5.250 seconds; Nani's own shortened rooftop select).
- [ ] **Source lineage only:** `dropbox:John/John Raws/IMG_2844.mov` (19.553 seconds); do not re-cut the long source unless Nani rejects her five-second select.
- [ ] **Type:** fast non-speaking rooftop positioning reel.
- [ ] **Edit sequence:** preserve Nani's five-second cut as the approved source timeline (BVE-01/BVE-02); mute accidental/source sound; add concise approved hook text (BVE-05); export (BVE-06).
- [ ] **Hard constraints:** maximum seven seconds; no visual filter/material change.
- [ ] **Version rule:** one final version.
- [ ] **Transcript caption source:** people say AI is not a real job; critics may be speaking from a cubicle; John is grateful to take calls from a rooftop while planes take off; go after your dreams and stay locked in. Exact public wording is not locked.
- [ ] **Audio:** silent/music-ready export; the user will add/select music later.
- [ ] **Exact output:** `12 - AI Critics From a Cubicle - Rooftop - Final.mp4` (rename after copy approval if needed).
- [ ] **Destination:** `dropbox:John/John Finals/`.
- [ ] **Definition of done:** a five-to-seven-second muted rooftop clip uses Nani's select and an approved concise hook without changing the footage's look.

## Batch 5 — Relationships, team, and style

Batch output: **3 video files**.  
Batch done condition: the friend-group message uses only the approved first-four-second moment and does not cover John's suit; the team reel uses a clear cut-around if hair retouch is not approved; the loafer reel is elegant and uses approved copy.

### JN-13 — Your Friend Group Sets the Tone

- [ ] **Source:** `dropbox:John/John Raws/Video Jun 24 2026, 9 37 58 AM.mov`.
- [ ] **Type:** four-second friend-group text reel.
- [ ] **Edit sequence:** record the first four seconds as the only keep range (BVE-01/BVE-02); place the text box either beside the men or near their shoes after a phone-size visual check, never on John's suit (BVE-05); export (BVE-06).
- [ ] **Hard constraints:** first four seconds; do not cover John's suit; no filter/material change.
- [ ] **Version rule:** one final version.
- [ ] **Locked theme / near-final copy:** `One of the most important things as a man is your friend group. The mentality sets the tone for how you live your life.` Supporting Instagram caption theme: if the conversations are gossip rather than uplifting, it is the wrong friend group.
- [ ] **Readability issue:** the near-final copy is long for four seconds; do not shrink it into unreadability. Nani must approve a shorter on-screen version or a longer hold.
- [ ] **Audio:** silent/music-ready export; no added music.
- [ ] **Exact output:** `13 - Your Friend Group Sets the Tone - Final.mp4`
- [ ] **Destination:** `dropbox:John/John Finals/`.
- [ ] **Definition of done:** the approved four-second moment and copy are readable at phone size, and the text never covers John's suit.

### JN-14 — Group Effort / Team Effort

- [ ] **Primary source:** `dropbox:John/John Raws/Video Jun 24 2026, 8 58 04 AM.mov`.
- [ ] **Approved fallback-angle pool:**
  - `dropbox:John/John Raws/IMG_2952.MOV`
  - `dropbox:John/John Raws/IMG_2953.MOV`
- [ ] **Type:** behind-the-scenes team reel.
- [ ] **Edit sequence:** first identify whether a clean primary section avoids the hair issue; if not, use Nani's explicit fallback and record clean snippets from the three sources as Bosi cuts (BVE-01/BVE-02); add only approved minimal team copy (BVE-05); export (BVE-06).
- [ ] **Capability decision:** generative hair modification is **GAP-H1**. The documented Bosi-compatible plan is to cut around the issue, which Nani explicitly allowed.
- [ ] **Hard constraints:** do not attempt unapproved generative body/hair alteration; no filter/material change.
- [ ] **Version rule:** one final version.
- [ ] **Caption source:** `It's a group effort. It's a team effort.` Exact public caption is not locked.
- [ ] **Audio:** silent/music-ready export; no added music.
- [ ] **Exact output:** `14 - It's a Team Effort - Behind the Scenes - Final.mp4`
- [ ] **Destination:** `dropbox:John/John Finals/`.
- [ ] **Definition of done:** the reel communicates collaborative effort using only flattering, clear angles and either avoids the hair issue or uses an expressly approved retouch method.

### JN-15 — Richard / Importance of a Good Loafer

- [ ] **Sources:**
  - `dropbox:John/John Raws/IMG_2921.MOV`
  - `dropbox:John/John Raws/IMG_2922.MOV`
  - `dropbox:John/John Raws/IMG_2923.MOV`
  - `dropbox:John/John Raws/IMG_2924.MOV`
  - `dropbox:John/John Raws/IMG_2925.MOV`
- [ ] **Type:** elegant client/loafer montage.
- [ ] **Target duration:** approximately 7-12 seconds, using only the cleanest shoe-selection/fitting actions.
- [ ] **Edit sequence:** select a clear establishing, loafer-detail/fitting, and finished-reveal progression; record and execute exact cuts in the approved Bosi plan (BVE-01/BVE-02); render approved minimal text (BVE-05); export (BVE-06).
- [ ] **Hard constraints:** elegant, not comedic; no filter/material change; do not invent a shoe origin, maker, or client endorsement.
- [ ] **Version rule:** one final version.
- [ ] **Caption source:** importance of a good loafer. Nani's draft territory includes `I like my shoes Italian and my wine French`, but she asked for alternatives and did not approve a final line.
- [ ] **Audio:** silent/music-ready export; the user will add/select music later.
- [ ] **Exact output:** `15 - The Importance of a Good Loafer - Richard - Final.mp4`
- [ ] **Destination:** `dropbox:John/John Finals/`.
- [ ] **Definition of done:** a concise elegant montage clearly centers the loafer, uses approved copy, and makes no unsupported product/brand claim.

## Batch 6 — Speaking reels by outfit (blocked intake batch)

Batch output: **`4N` videos, where `N` is the number of outfits**.  
Batch done condition: for each outfit, two distinct complete topics are selected and each topic has a frame-matched A/B pair; A has no captions/fillers, B has verified word-timed captions and approved contextual fillers; all files use continuous ordered numbering.

### SPK template — repeat for every outfit and its two topics

- [ ] **Sources:** not present/identifiable in current Dropbox; Nani says she will upload the remaining speaking footage and second layer.
- [ ] **Type:** speaking video edit with clean and enhanced versions.
- [ ] **Per-topic edit sequence:**
  1. Identify a complete single-topic thought with a strong natural opening; do not join unrelated topics.
  2. Transcribe with faster-whisper word timestamps (BVE-03).
  3. Detect gaps over 0.3 seconds and fillers, but place every proposed removal in the review plan before cutting; preserve 0.15-second breaths and any intentional dramatic pause (BVE-01/BVE-02).
  4. Make the clean edited master from the approved cuts, preserving complete sentences (BVE-02).
  5. Export `A` from that clean master with no captions and no filler pictures (BVE-06).
  6. Build `B` from exactly the same master; burn consistent white captions using BVE-04 and add contextual fillers only where they clarify the spoken concept (BVE-07). Nani's example is a forked-road image when John discusses fear or a crossroads.
  7. Export and verify B (BVE-06).
- [ ] **Caption style:** TikTok/Instagram white bold 55 pt, black outline 3, `MarginV=338`, one-word cues preferred; keep this consistent across all speaking reels. No kinetic-text effects in the speech caption layer.
- [ ] **Audio:** retain and clean John's dialogue; no background music.
- [ ] **Hard constraints:** every outfit is a separate group; two separate topics per outfit; speaking tone should reveal kindness; caption text must match the verified audio; filler must not cover or misrepresent John; no filter/fogginess/material change.
- [ ] **Names:**
  - `[sequence]A - John - [Outfit] - [Topic] - Final No Cap.mp4`
  - `[sequence]B - John - [Outfit] - [Topic] - Final.mp4`
- [ ] **Destination:** `dropbox:John/John Finals/`.
- [ ] **Definition of done:** every outfit yields Topic 1 A/B and Topic 2 A/B, the A/B timings match, B captions are human-verified, and Dropbox ordering is unambiguous.

## Existing finals requiring no new edit

These 14 current files are excluded from the deliverable count and must not be modified. They are mapped as existing final/cover/carousel material.

- [ ] `dropbox:John/John Finals/1760-Edit_(2).jpg`
- [ ] `dropbox:John/John Finals/54E2DAA0-4992-4302-8E29-1C3A9C770344.JPG`
- [ ] `dropbox:John/John Finals/Ad cover pic  AM.jpg`
- [ ] `dropbox:John/John Finals/Cover photo for inspire video .jpg`
- [ ] `dropbox:John/John Finals/Cover photo for “it starts with” reel .jpg`
- [ ] `dropbox:John/John Finals/IMG_2581.heic`
- [ ] `dropbox:John/John Finals/IMG_2586.heic`
- [ ] `dropbox:John/John Finals/IMG_2845.heic`
- [ ] `dropbox:John/John Finals/IMG_2847.HEIC`
- [ ] `dropbox:John/John Finals/IMG_2851.HEIC`
- [ ] `dropbox:John/John Finals/IMG_2855.heic`
- [ ] `dropbox:John/John Finals/IMG_2856.heic`
- [ ] `dropbox:John/John Finals/Inspire speaker vid .mov`
- [ ] `dropbox:John/John Finals/Photo Feb 20 2026, 10 51 13 AM.jpg`

The other three files in `John Finals` are inputs to planned work: `Discover your confidence   AM.jpg`, `It starts with you .mov`, and `IMG_2859.mov`.

## Dropbox material not concretely accounted for by the transcript

No edit is invented for these files. They stay untouched until Nani supplies a specific instruction.

### Legacy/studio video footage with no new concrete deliverable instruction

- [ ] `dropbox:John/John Raws/IMG_1270 (1).MOV`
- [ ] `dropbox:John/John Raws/IMG_1301 (1).MOV`
- [ ] `dropbox:John/John Raws/IMG_1301.MOV` — duplicate-size counterpart of `IMG_1301 (1).MOV`; do not delete.
- [ ] `dropbox:John/John Raws/IMG_8963.MOV`
- [ ] `dropbox:John/John Raws/IMG_8965.MOV`
- [ ] `dropbox:John/John Raws/IMG_8969 (1).MOV`
- [ ] `dropbox:John/John Raws/IMG_8974.MOV`
- [ ] `dropbox:John/John Raws/Video Feb 20 2026, 9 39 00 AM.mov`

### Additional still material without a concrete instruction

- [ ] `dropbox:John/John Raws/IMG_1271.HEIC`
- [ ] `dropbox:John/John Raws/IMG_1272.HEIC`
- [ ] `dropbox:John/John Raws/IMG_1273.HEIC`
- [ ] `dropbox:John/John Raws/IMG_1274.HEIC`
- [ ] `dropbox:John/John Raws/IMG_1299.HEIC`
- [ ] `dropbox:John/John Raws/IMG_8968.JPG`

## Requested material missing from Dropbox

- [ ] **Bosi executable toolkit:** the skill documents exist at `/root/bosi-editor-skills/`, but their documented `/root/bosi-editor/edit.py` and supporting scripts are absent.
- [ ] **`Video May 09 2026, 4 49 28 PM.mov`:** absent under this renamed Dropbox path, but the exact depicted original is retained locally as `/root/video-engine/imports/john-content-campaign/source/IMG_2330.MOV`; Batch 2 therefore has a verified local source and does not need a substitute.
- [ ] **Speaking reels grouped by outfit:** Nani said the remaining speaking footage/second layer would be uploaded; the current tree does not contain an identifiable two-topics-per-outfit set.
- [ ] **Funny podium reel/song reference:** Nani attempted to send it during the meeting, but the send failed. This is recorded for provenance only; it is not a blocker because the user has deferred all music.
- [ ] **Approved exact copy/Instagram captions:** several concepts contain themes or live drafts, not final approved wording; these are listed in the open questions.

## Complete Dropbox inventory map

This appendix accounts for every file found under `dropbox:John/` at planning time.

### `John Finals` — 17 files

- `1760-Edit_(2).jpg`
- `54E2DAA0-4992-4302-8E29-1C3A9C770344.JPG`
- `Ad cover pic  AM.jpg`
- `Cover photo for inspire video .jpg`
- `Cover photo for “it starts with” reel .jpg`
- `Discover your confidence   AM.jpg`
- `IMG_2581.heic`
- `IMG_2586.heic`
- `IMG_2845.heic`
- `IMG_2847.HEIC`
- `IMG_2851.HEIC`
- `IMG_2855.heic`
- `IMG_2856.heic`
- `IMG_2859.mov`
- `Inspire speaker vid .mov`
- `It starts with you .mov`
- `Photo Feb 20 2026, 10 51 13 AM.jpg`

### `John Raws` — SuitSupply / May 9, 11 files

- `Photo May 09 2026, 5 32 13 PM.jpg`
- `Photo May 09 2026, 5 53 26 PM.jpg`
- `Video May 09 2026, 4 17 13 PM.mov`
- `Video May 09 2026, 4 29 40 PM.mov`
- `Video May 09 2026, 4 29 54 PM.mov`
- `Video May 09 2026, 4 31 27 PM.mov`
- `Video May 09 2026, 4 32 21 PM.mov`
- `Video May 09 2026, 4 44 14 PM.mov`
- `Video May 09 2026, 4 53 41 PM.mov`
- `Video May 09 2026, 5 25 27 PM.mov`
- `Video May 09 2026, 5 28 53 PM.mov`

### `John Raws` — APAICS / May 12, 3 files

- `Photo May 12 2026, 8 43 42 AM.jpg`
- `Photo May 12 2026, 9 18 01 AM.jpg`
- `Video May 12 2026, 9 16 50 AM.mov`

### `John Raws` — black-suit leadership event / May 14, 32 files

- `IMG_2400.MOV`
- `Independance funny vidAM.mov`
- `Photo May 14 2026, 11 50 20 AM.jpg`
- `Photo May 14 2026, 12 15 36 PM.jpg`
- `Photo May 14 2026, 2 01 25 PM.jpg`
- `Photo May 14 2026, 2 01 27 PM.jpg`
- `Photo May 14 2026, 2 01 31 PM.jpg`
- `Photo May 14 2026, 2 01 32 PM.jpg`
- `Photo May 14 2026, 2 01 33 PM.jpg`
- `Video May 14 2026, 11 47 04 AM.mov`
- `Video May 14 2026, 11 47 20 AM.mov`
- `Video May 14 2026, 12 11 24 PM.mov`
- `Video May 14 2026, 1 44 37 PM.mov`
- `Video May 14 2026, 1 50 50 PM.mov`
- `Video May 14 2026, 1 51 47 PM.mov`
- `Video May 14 2026, 1 52 22 PM.mov`
- `Video May 14 2026, 1 53 05 PM.mov`
- `Video May 14 2026, 1 53 38 PM.mov`
- `Video May 14 2026, 1 54 03 PM.mov`
- `Video May 14 2026, 1 54 22 PM.mov`
- `Video May 14 2026, 1 54 45 PM.mov`
- `Video May 14 2026, 1 55 31 PM.mov`
- `Video May 14 2026, 1 55 55 PM.mov`
- `Video May 14 2026, 1 56 44 PM.mov`
- `Video May 14 2026, 1 57 57 PM.mov`
- `Video May 14 2026, 1 58 20 PM.mov`
- `Video May 14 2026, 2 01 46 PM.mov`
- `Video May 14 2026, 2 06 02 PM.mov`
- `Video May 14 2026, 2 06 27 PM.mov`
- `Video May 14 2026, 2 07 03 PM.mov`
- `Video May 14 2026, 2 11 24 PM.mov`
- `Video May 14 2026, 2 12 01 PM.mov`

### `John Raws` — contemporary office, rooftop, relationship, and loafer material, 16 files

- `26e1e46b92bc4adcb473bd8fd3541b14.MP4`
- `3D47B365-E6FE-46F3-9689-02C4F81D88D4.JPG`
- `59B54D2C-48AF-4363-831F-FDAB5D965827.jpg`
- `IMG_2451.MOV`
- `IMG_2844.mov`
- `IMG_2859.mov`
- `IMG_2921.MOV`
- `IMG_2922.MOV`
- `IMG_2923.MOV`
- `IMG_2924.MOV`
- `IMG_2925.MOV`
- `IMG_2952.MOV`
- `IMG_2953.MOV`
- `Video Jun 24 2026, 8 58 04 AM.mov`
- `Video Jun 24 2026, 9 37 58 AM.mov`
- `c12b67e441434051a4fed48671a988e7.MP4`

### `John Raws` — legacy/studio material, 14 files

- `IMG_1270 (1).MOV`
- `IMG_1271.HEIC`
- `IMG_1272.HEIC`
- `IMG_1273.HEIC`
- `IMG_1274.HEIC`
- `IMG_1299.HEIC`
- `IMG_1301 (1).MOV`
- `IMG_1301.MOV`
- `IMG_8963.MOV`
- `IMG_8965.MOV`
- `IMG_8968.JPG`
- `IMG_8969 (1).MOV`
- `IMG_8974.MOV`
- `Video Feb 20 2026, 9 39 00 AM.mov`

Inventory cross-check: 17 finals + 11 SuitSupply + 3 APAICS + 32 May 14 event + 16 contemporary + 14 legacy/studio = **93 files**.

## Open questions — answers required only for the affected batch

### Technical and delivery

1. Where is the executable Bosi toolkit referenced by the installed skill documents? Expected path: `/root/bosi-editor/`.
2. Should posting captions be saved in Dropbox file comments, companion `.txt` sidecars, or left for the user to paste manually? Nani asked for Dropbox comments, but the available connector has no verified comment-write operation.
3. The phrase “blackout with silence” in the user's latest note cannot be mapped safely to a specific transcript item. If it means an intentional black frame/silent gap, identify the item and position; otherwise none will be added.

### Batch 1

4. Approve the exact inner-confidence quote and Instagram caption for `Discover Your Confidence`; Nani supplied only the theme.

### Batch 2

5. Approve or rewrite the custom-suit viral statement in JN-04.
6. Approve the final `Preparation Is Key` caption and whether it should say `Nani Rosen`, `Rosen Relations`, and/or `luxury brand specialist`.
7. Confirm the official public wording: `APAICS Panel`, `APAICS Legislative Leadership Summit`, and whether `Celebrating 250 Years, Building America's Future Together` should appear.
8. For JN-05, confirm the exact intended edges for “through second 9” and “the end of 11,” and whether `Video May 12 2026, 9 16 50 AM.mov` is the final payoff or unused.

### Batch 3

9. Choose the funny-podium line: `Anyone have a chair I could borrow?` or `Comment what I said if you can read my lips.`
10. Does the funny-podium reel or award reel require a separate cover? The transcript/recording did not assign one to either; the exact demonstrated covers belong to the laptop and staircase reels.

### Batches 4-5

11. Approve the exact Pollyanna/SEO/AI overlay; may current Pollyanna wording be researched, or must copy remain transcript-only?
12. Approve the exact public copy for the staircase, rooftop AI/cubicle, friend-group, team-effort, and loafer items.
13. For the four-second friend-group clip, should the on-screen line be shortened, or should the visual hold be longer so the full sentence is readable?

### Speaking intake

14. When the speaking footage arrives, how many outfits are present?
15. Should speaking-pair numbering continue globally from `16A/16B`, `17A/17B`, etc., or restart inside per-outfit folders? No outfit subfolders currently exist.

## Execution gate

Do not run a cut, caption, render, copy-to-final, rename, Dropbox comment write, or upload until:

- [ ] the relevant batch is explicitly approved;
- [ ] all open questions affecting that batch are answered;
- [ ] exact source files are present;
- [ ] the executable Bosi toolkit passes preflight;
- [ ] required on-screen/posting copy is approved; and
- [ ] the output folder and caption-storage convention are confirmed.
