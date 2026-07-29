/**
 * fixtures.js — static mock data for demo / offline mode.
 * Activated when: localStorage.mock === "1" AND a real /api fetch fails
 * with a network error (i.e. the server is not running).
 *
 * Shape mirrors the live API contract in REVAMP_CONTRACTS.md §6 exactly.
 * Updated for revamp v2: kind/mode/aspect on clips, schedule object on
 * campaigns, sources_summary array, engines, spend payload, hero nulls.
 * Updated v3: fitness campaign removed (§9); peptides added as neutral demo.
 * Updated v4: clips_detail per-clip rows; judge_decision; correction_attempts.
 */

const NOW = Date.now();
const hAgo = (h) => new Date(NOW - h * 3_600_000).toISOString();
const hLater = (h) => new Date(NOW + h * 3_600_000).toISOString();

export const stats = {
  pending: 5,
  approved: 14,
  scheduled: 6,
  posted: 52,
  next_run_at: hLater(2),
};

// Hero media — all nulls in mock mode so the CSS cinematic backdrop
// fallback is shown (intentional, not a broken state).
export const hero = {
  video: null,
  video_vertical: null,
  poster: null,
  poster_mobile: null,
};

export const campaigns = [
  {
    name: 'peptides',
    enabled: true,
    mode: 'production',
    sources_summary: [
      { platform: 'youtube', count: 2, label: 'YouTube · 2 channels' },
    ],
    // schedule is now a formatted object — fixes [object Object] bug
    schedule: {
      posts_per_day: 1,
      times: ['17:00'],
      timezone: 'America/New_York',
      label: '1×/day · 17:00 ET',
    },
    engines: { clips: true, memes: false },
    last_run_at: hAgo(3),
    pending_count: 3,
  },
  {
    name: 'demo_run',
    enabled: false,
    mode: 'demo',
    sources_summary: [
      { platform: 'youtube', count: 1, label: 'YouTube · 1 channel' },
    ],
    schedule: {
      posts_per_day: 2,
      times: ['09:00', '18:00'],
      timezone: 'America/New_York',
      label: '2×/day · 09:00, 18:00 ET',
    },
    engines: { clips: true, memes: true },
    last_run_at: hAgo(12),
    pending_count: 2,
  },
];

// Clips — kind/mode/aspect added per contract §1.
// Mix: 2 peptides clips (pending / didnt_pass with judge), 2 memes.
export const clips = [
  // Peptides — pending review
  {
    id: 'mock_clip_p001',
    campaign: 'peptides',
    kind: 'clip',
    mode: 'production',
    aspect: '9:16',
    hook: 'Most researchers overlook this critical peptide dosing window',
    score: 0.91,
    reason: 'Strong hook, cites mechanism, no unsafe dosing claims',
    caption:
      'Most researchers overlook this critical peptide dosing window\n\nvia @PeptideScience\n#peptides #biohacking #longevity',
    source: {
      handle: 'PeptideScience',
      url: 'https://youtube.com/watch?v=mock_p001',
      title: 'BPC-157 Full Breakdown — Dosing, Timing and Mechanism',
      platform: 'youtube',
    },
    start: 182.5,
    end: 237.0,
    duration: 54.5,
    destination_channels: ['tiktok_peptides', 'instagram_peptides'],
    proposed_slot: hLater(4),
    created_at: hAgo(0.5),
    video_url: null,
    thumb_url: null,
    gate_status: 'ready',
    gate_reasons: [
      { phase: '1', check: 'resolution',            pass: true, reason: '1080x1920 OK' },
      { phase: '1', check: 'hook_present_in_hook_frame', pass: true, reason: 'Hook text visible in hook frame' },
      { phase: '1', check: 'real_humans',           pass: true, reason: 'Real human speaker detected' },
      { phase: '2', check: 'formula_score',         pass: true, reason: 'Score 0.91 >= threshold 0.60' },
    ],
    formula_score: 0.91,
    judge_decision: null,
    correction_attempts: 0,
  },

  // Peptides — didnt_pass with judge_decision and 2 correction attempts
  {
    id: 'mock_clip_p_fail',
    campaign: 'peptides',
    kind: 'clip',
    mode: 'production',
    aspect: '9:16',
    hook: 'This peptide combination has a 3x synergy effect',
    score: 0.63,
    reason: null,
    caption:
      'This peptide combination has a 3x synergy effect\n\nvia @PeptideScience\n#peptides #biohacking',
    source: {
      handle: 'PeptideScience',
      url: 'https://youtube.com/watch?v=mock_p001',
      title: 'BPC-157 Full Breakdown — Dosing, Timing and Mechanism',
      platform: 'youtube',
    },
    start: 310.0,
    end: 362.5,
    duration: 52.5,
    destination_channels: ['tiktok_peptides'],
    proposed_slot: null,
    created_at: hAgo(1),
    video_url: null,
    thumb_url: null,
    gate_status: 'didnt_pass',
    gate_reasons: [
      { phase: '1', check: 'watermark_visible', pass: false, reason: 'No watermark detected in any frame' },
      { phase: '1', check: 'captions_present',  pass: false, reason: 'No word-by-word captions found in mid-clip frame' },
    ],
    formula_score: null,
    judge_decision: {
      clip_id: 'mock_clip_p_fail',
      decision: 'rejected',
      reasons: [
        'Caption burns are illegible at full playback speed',
        'Hook text is absent from the first 3 seconds of the clip',
      ],
      decided_at: hAgo(0.5),
    },
    correction_attempts: 2,
  },

  // Meme fixtures (demo_run — unchanged except fitness refs removed)
  {
    id: 'mock_meme_001',
    campaign: 'demo_run',
    kind: 'meme',
    mode: 'demo',
    aspect: '1:1',
    hook: 'When someone asks if peptides are just "fancy supplements"',
    score: 0.83,
    reason: 'On-brand humor, relatable to niche audience, no unsafe content',
    caption: 'When someone asks if peptides are just "fancy supplements" \u{1F602}\n\n#peptidehumor #biohacking #longevity',
    source: null,
    start: null,
    end: null,
    duration: null,
    destination_channels: ['instagram_peptides'],
    proposed_slot: hLater(7),
    created_at: hAgo(2),
    video_url: null,
    thumb_url: null,
    gate_status: 'pending',
    gate_reasons: null,
    formula_score: null,
    judge_decision: null,
    correction_attempts: 0,
    meme_meta: {
      concept: 'relatable niche humor — skeptic archetype',
      classifier_scores: { on_format: 0.91, on_voice: 0.87, on_brand: 0.85, legibility: 0.94, compliance: 0.99 },
      profile_version: 1,
    },
  },
  {
    id: 'mock_meme_002',
    campaign: 'demo_run',
    kind: 'meme',
    mode: 'demo',
    aspect: '4:5',
    hook: 'Stack your recovery stack but make it aesthetic',
    score: 0.76,
    reason: 'Clean format, on-brand visual style',
    caption: 'Stack your recovery stack but make it aesthetic\n\n#biohacking #peptides #longevity',
    source: null,
    start: null,
    end: null,
    duration: null,
    destination_channels: ['tiktok_peptides', 'instagram_peptides'],
    proposed_slot: hLater(8),
    created_at: hAgo(3),
    video_url: null,
    thumb_url: null,
    gate_status: 'pending',
    gate_reasons: null,
    formula_score: null,
    judge_decision: null,
    correction_attempts: 0,
    meme_meta: {
      concept: 'aspirational protocol aesthetic with structured caption',
      classifier_scores: { on_format: 0.88, on_voice: 0.80, on_brand: 0.82, legibility: 0.91, compliance: 1.0 },
      profile_version: 1,
    },
  },
];

// Build a week-start ISO string from an offset (0 = most recent Monday)
function weekStart(weeksAgo) {
  const d = new Date(NOW);
  const day = d.getDay();
  const diff = (day === 0 ? 6 : day - 1); // Monday = 0
  d.setDate(d.getDate() - diff - weeksAgo * 7);
  d.setHours(0, 0, 0, 0);
  return d.toISOString().slice(0, 10);
}

export const analytics = {
  channels: [
    {
      channel: 'tiktok_peptides',
      weekly: [
        { week_start: weekStart(5), views: 12400, likes: 890,  comments: 123, shares: 45,  posts: 7 },
        { week_start: weekStart(4), views: 18200, likes: 1340, comments: 198, shares: 76,  posts: 7 },
        { week_start: weekStart(3), views: 15600, likes: 1100, comments: 145, shares: 58,  posts: 7 },
        { week_start: weekStart(2), views: 22100, likes: 1890, comments: 267, shares: 112, posts: 7 },
        { week_start: weekStart(1), views: 19800, likes: 1560, comments: 234, shares: 89,  posts: 7 },
        { week_start: weekStart(0), views: 8200,  likes: 640,  comments: 89,  shares: 34,  posts: 3 },
      ],
    },
    {
      channel: 'instagram_peptides',
      weekly: [
        { week_start: weekStart(5), views: 8300,  likes: 1240, comments: 67,  shares: 23,  posts: 7 },
        { week_start: weekStart(4), views: 11200, likes: 1780, comments: 89,  shares: 34,  posts: 7 },
        { week_start: weekStart(3), views: 9800,  likes: 1560, comments: 78,  shares: 28,  posts: 7 },
        { week_start: weekStart(2), views: 14500, likes: 2340, comments: 134, shares: 56,  posts: 7 },
        { week_start: weekStart(1), views: 12100, likes: 1980, comments: 112, shares: 45,  posts: 7 },
        { week_start: weekStart(0), views: 5400,  likes: 876,  comments: 48,  shares: 19,  posts: 3 },
      ],
    },
  ],
  clips: [
    {
      clip_id: 'mock_a1',
      hook: 'Most researchers overlook this critical peptide dosing window',
      platform: 'tiktok',
      permalink: 'https://tiktok.com/@viciresearch/video/mock_p1',
      views: 45200,
      likes: 3400,
      comments: 234,
      shares: 178,
      posted_at: hAgo(24 * 8),
      mode: 'production',
      kind: 'clip',
      campaign: 'peptides',
    },
    {
      clip_id: 'mock_a2',
      hook: 'The half-life difference between TB-500 and BPC-157 explained',
      platform: 'instagram',
      permalink: 'https://instagram.com/reel/mockPEP2',
      views: 38900,
      likes: 5600,
      comments: 189,
      shares: 145,
      posted_at: hAgo(24 * 9),
      mode: 'production',
      kind: 'clip',
      campaign: 'peptides',
    },
    {
      clip_id: 'mock_a3',
      hook: 'When someone asks if peptides are just "fancy supplements"',
      platform: 'instagram',
      permalink: 'https://instagram.com/p/mockMEME1',
      views: 21400,
      likes: 2800,
      comments: 112,
      shares: 89,
      posted_at: hAgo(24 * 5),
      mode: 'demo',
      kind: 'meme',
      campaign: 'demo_run',
    },
  ],
};

// Sources — mock data for the Sources view (history tab).
// Includes new pipeline fields: stage, clips_identified, clips_rendered,
// clips_approved, clips_rejected, clips_pending, exhaustion.
export const sources = [
  {
    id: 1,
    source_id: 'youtube:mock_p001',
    platform: 'youtube',
    url: 'https://youtube.com/watch?v=mock_p001',
    title: 'BPC-157 Full Breakdown — Dosing, Timing and Mechanism',
    author_handle: 'PeptideScience',
    campaign: 'peptides',
    status: 'done',
    stage: 'complete',
    clips_identified: 3,
    clips_rendered: 3,
    clips_approved: 1,
    clips_rejected: 1,
    clips_pending: 1,
    stage_error: null,
    stage_updated_at: hAgo(3),
    exhaustion: 'fully_exhausted',
    processed_at: hAgo(3),
    clip_count: 3,
    clips: [
      { id: 'mock_clip_p001', hook: 'Most researchers overlook this critical peptide dosing window', status: 'approved',        gate_status: 'ready'      },
      { id: 'mock_clip_p002', hook: 'BPC-157 reaches peak plasma concentration within 30 minutes',  status: 'pending_review', gate_status: 'ready'      },
      { id: 'mock_clip_p003', hook: 'Stacking TB-500 with BPC-157 — the right ratio',               status: 'rejected',       gate_status: 'didnt_pass' },
    ],
    used_ranges_count: 3,
    thumbnail_url: null,
  },
  {
    id: 2,
    source_id: 'youtube:mock_p002',
    platform: 'youtube',
    url: 'https://youtube.com/watch?v=mock_p002',
    title: 'GHK-Cu Peptide — Full Science Review',
    author_handle: 'BiohackLab',
    campaign: 'peptides',
    status: 'partially_done',
    stage: 'reviewing',
    clips_identified: 3,
    clips_rendered: 1,
    clips_approved: 0,
    clips_rejected: 0,
    clips_pending: 1,
    stage_error: null,
    stage_updated_at: hAgo(12),
    exhaustion: 'partially_used',
    processed_at: hAgo(12),
    clip_count: 1,
    clips: [
      { id: 'mock_clip_p004', hook: 'GHK-Cu upregulates over 4000 genes involved in repair', status: 'pending_review', gate_status: 'ready' },
    ],
    used_ranges_count: 1,
    thumbnail_url: null,
  },
  {
    id: 3,
    source_id: 'youtube:mock_p003',
    platform: 'youtube',
    url: 'https://youtube.com/watch?v=mock_p003',
    title: 'Semax and Selank — Nootropic peptides explained',
    author_handle: 'BiohackLab',
    campaign: 'peptides',
    status: 'selected',
    stage: 'queued',
    clips_identified: null,
    clips_rendered: 0,
    clips_approved: 0,
    clips_rejected: 0,
    clips_pending: 0,
    stage_error: null,
    stage_updated_at: null,
    exhaustion: 'in_progress',
    processed_at: null,
    clip_count: 0,
    clips: [],
    used_ranges_count: 0,
    thumbnail_url: null,
  },
];

// In-progress sources — shown in the live "In progress" panel (SSE / polling).
// One source has clips_detail exercising every per-clip state.
// One source is failed (demonstrates error display).
export const sourcesProgress = [
  // In-progress source with clips_detail — stage: correcting
  // Exercises: ready · correcting fix 1/2 · didn't pass · rendering
  {
    id: 4,
    source_id: 'youtube:inprog_p001',
    platform: 'youtube',
    url: 'https://youtube.com/watch?v=inprog_p001',
    title: 'Peptide Protocols for Accelerated Recovery — Science Deep Dive',
    author_handle: 'PeptideScience',
    campaign: 'peptides',
    status: 'selected',
    stage: 'correcting',
    clips_identified: 4,
    clips_rendered: 4,
    clips_approved: 0,
    clips_rejected: 1,
    clips_pending: 2,
    stage_error: null,
    stage_updated_at: hAgo(0.1),
    exhaustion: 'in_progress',
    processed_at: hAgo(0.5),
    clip_count: 4,
    clips: [],
    used_ranges_count: 4,
    thumbnail_url: null,
    // clips_detail — SSE §6 payload exercising all four per-clip states
    clips_detail: [
      {
        id: 'cd_p001',
        gate_status: 'ready',
        status: 'approved',
        correction_attempts: 0,
        last_failure_reasons: [],
        judge: 'approved',
      },
      {
        id: 'cd_p002',
        gate_status: 'pending',
        status: 'correcting',
        correction_attempts: 1,
        last_failure_reasons: [
          'Caption text is missing from the hook frame',
          'Speaker is not centred within the 9:16 crop',
        ],
        judge: null,
      },
      {
        id: 'cd_p003',
        gate_status: 'rejected',
        status: 'rejected',
        correction_attempts: 2,
        last_failure_reasons: [
          'Watermark not visible in any sampled frame',
          'Hook text absent from first 5 frames of the clip',
        ],
        judge: 'rejected',
      },
      {
        id: 'cd_p004',
        gate_status: 'pending',
        status: 'rendering',
        correction_attempts: 0,
        last_failure_reasons: [],
        judge: null,
      },
    ],
  },
  // Failed source — demonstrates error block
  {
    id: 5,
    source_id: 'youtube:inprog_p002',
    platform: 'youtube',
    url: 'https://youtube.com/watch?v=inprog_p002',
    title: 'Epithalon Anti-Aging Protocol — Full Review',
    author_handle: 'BiohackLab',
    campaign: 'peptides',
    status: 'selected',
    stage: 'failed',
    clips_identified: null,
    clips_rendered: 0,
    clips_approved: 0,
    clips_rejected: 0,
    clips_pending: 0,
    stage_error: 'Apify transcript actor failed: ACTOR_TIMED_OUT after 120s. Video may be age-restricted or has restricted access. Check that the YouTube account is public and not geo-blocked.',
    stage_updated_at: hAgo(0.5),
    exhaustion: 'in_progress',
    processed_at: null,
    clip_count: 0,
    clips: [],
    used_ranges_count: 0,
    thumbnail_url: null,
    clips_detail: null,
  },
];

// Approved clips — shown in the Queue "Approved" collapsible section.
// Separate from fixtures.clips to avoid polluting the pending queue in mock mode.
export const approvedClips = [
  {
    id: 'mock_clip_p_approved_001',
    campaign: 'peptides',
    kind: 'clip',
    mode: 'production',
    aspect: '9:16',
    hook: 'BPC-157 reaches peak plasma concentration within 30 minutes of subcutaneous injection',
    score: 0.89,
    status: 'approved',
    caption:
      'BPC-157 reaches peak plasma concentration within 30 minutes\n\nvia @PeptideScience\n#peptides #bpc157 #recovery #biohacking',
    source: {
      handle: 'PeptideScience',
      url: 'https://youtube.com/watch?v=mock_p001',
      title: 'BPC-157 Full Breakdown — Dosing, Timing and Mechanism',
      platform: 'youtube',
    },
    start: 480,
    end: 535,
    duration: 55,
    destination_channels: ['tiktok_peptides'],
    proposed_slot: hLater(10),
    scheduled_at: null,
    created_at: hAgo(2),
    video_url: null,
    thumb_url: null,
    gate_status: 'ready',
    gate_reasons: [],
    formula_score: 0.89,
    judge_decision: null,
    correction_attempts: 0,
    review_feedback: { action: 'approved', reasons: [], note: null, decided_at: hAgo(1.5) },
  },
  {
    id: 'mock_clip_p_approved_002',
    campaign: 'peptides',
    kind: 'clip',
    mode: 'production',
    aspect: '9:16',
    hook: 'The GHK-Cu skin repair mechanism works at the gene expression level',
    score: 0.82,
    status: 'scheduled',
    caption:
      'The GHK-Cu skin repair mechanism works at the gene expression level\n\nvia @BiohackLab\n#peptides #ghkcu #antiaging #longevity',
    source: {
      handle: 'BiohackLab',
      url: 'https://youtube.com/watch?v=mock_p002',
      title: 'GHK-Cu Peptide — Full Science Review',
      platform: 'youtube',
    },
    start: 200,
    end: 255,
    duration: 55,
    destination_channels: ['tiktok_peptides', 'instagram_peptides'],
    proposed_slot: hLater(14),
    scheduled_at: hLater(14),
    created_at: hAgo(3),
    video_url: null,
    thumb_url: null,
    gate_status: 'ready',
    gate_reasons: [],
    formula_score: 0.82,
    judge_decision: null,
    correction_attempts: 0,
    review_feedback: { action: 'approved', reasons: [], note: null, decided_at: hAgo(2) },
  },
];

// ── Event-state snapshots for in-progress panel mock mode ─────────────────────
// Shape mirrors GET /api/sources/{id}/events/state response (§3 of PROGRESS_EVENTS_CONTRACTS.md).
// Set localStorage.mockScene to one of the keys below to show that UI state.
// Consumed by sources.js when localStorage.mock === "1".

const _nowMs = Date.now();
const _msAgo = (ms) => new Date(_nowMs - ms).toISOString();

export const inProgressScenes = {
  // 1. Empty — no sources currently processing
  empty: [],

  // 2. Mid-identifying — one source reading the transcript, no clips yet
  midIdentifying: [
    {
      source_id: 'youtube:mock_identifying',
      stage: 'identifying',
      title: 'Advanced Peptide Protocols for Recovery — Full Deep Dive',
      url: 'https://youtube.com/watch?v=mock_ident',
      platform: 'youtube',
      author_handle: 'PeptideScience',
      campaign: 'peptides',
      thumbnail_url: null,
      stage_error: null,
      clips_detail: [],
      last_event_id: '15',
      progress_n: null,
      progress_total: null,
      latest_detail: 'Reading transcript / selecting moments',
      latest_ts: _msAgo(12_000),
      stage_elapsed: { queued: 3, transcribing: 42, downloading: 28, identifying: 37 },
    },
  ],

  // 3. Mid-rendering — 8 clips found, 5 rendered, mixed chip states
  midRendering: [
    {
      source_id: 'youtube:mock_rendering',
      stage: 'rendering',
      title: 'BPC-157 Full Breakdown — Dosing, Timing and Mechanism',
      url: 'https://youtube.com/watch?v=mock_render',
      platform: 'youtube',
      author_handle: 'PeptideScience',
      campaign: 'peptides',
      thumbnail_url: null,
      stage_error: null,
      clips_detail: [
        { clip_id: 101, stage: 'ready',       status: 'done',    correction_attempts: 0, reason: null },
        { clip_id: 102, stage: 'rendering',   status: 'running', correction_attempts: 0, reason: null },
        { clip_id: 103, stage: 'reviewing',   status: 'running', correction_attempts: 0, reason: null },
        { clip_id: 104, stage: 'correction',  status: 'running', correction_attempts: 1,
          reason: 'Caption burns are illegible at playback speed' },
        { clip_id: 105, stage: 'rendering',   status: 'running', correction_attempts: 0, reason: null },
        { clip_id: 106, stage: 'ready',       status: 'done',    correction_attempts: 1, reason: null },
        { clip_id: 107, stage: 'didnt_pass',  status: 'done',    correction_attempts: 2,
          reason: 'Hook text absent from first 5 frames; watermark not visible after 2 correction attempts' },
        { clip_id: 108, stage: 'rendering',   status: 'running', correction_attempts: 0, reason: null },
      ],
      last_event_id: '58',
      progress_n: 5,
      progress_total: 8,
      latest_detail: 'Creating clip 5 of 8 — rendering on Modal',
      latest_ts: _msAgo(8_000),
      stage_elapsed: { queued: 4, transcribing: 51, downloading: 33, identifying: 62,
                       identified: 5, rendering: 124 },
    },
  ],

  // 4. Correction in progress — one clip being re-rendered after reviewer feedback
  correctionInProgress: [
    {
      source_id: 'youtube:mock_correction',
      stage: 'correction',
      title: 'GHK-Cu Peptide — Full Science Review',
      url: 'https://youtube.com/watch?v=mock_correct',
      platform: 'youtube',
      author_handle: 'BiohackLab',
      campaign: 'peptides',
      thumbnail_url: null,
      stage_error: null,
      clips_detail: [
        { clip_id: 201, stage: 'ready',      status: 'done',    correction_attempts: 0, reason: null },
        { clip_id: 202, stage: 'ready',      status: 'done',    correction_attempts: 1, reason: null },
        { clip_id: 203, stage: 'correction', status: 'running', correction_attempts: 1,
          reason: 'Speaker not centred in 9:16 crop — re-rendering with adjusted face track' },
        { clip_id: 204, stage: 'rendering',  status: 'running', correction_attempts: 0, reason: null },
      ],
      last_event_id: '72',
      progress_n: 3,
      progress_total: 4,
      latest_detail: 'Correcting clip — applying adjusted face track (fix 1/2)',
      latest_ts: _msAgo(22_000),
      stage_elapsed: { queued: 3, transcribing: 48, downloading: 31, identifying: 55,
                       identified: 4, rendering: 180, correction: 22 },
    },
  ],

  // 5. Complete — all clips processed; panel shows terminal summary
  complete: [
    {
      source_id: 'youtube:mock_complete',
      stage: 'complete',
      title: 'Semax and Selank — Nootropic Peptides Explained',
      url: 'https://youtube.com/watch?v=mock_complete',
      platform: 'youtube',
      author_handle: 'BiohackLab',
      campaign: 'peptides',
      thumbnail_url: null,
      stage_error: null,
      clips_detail: [
        { clip_id: 301, stage: 'ready',      status: 'done', correction_attempts: 0, reason: null },
        { clip_id: 302, stage: 'ready',      status: 'done', correction_attempts: 0, reason: null },
        { clip_id: 303, stage: 'didnt_pass', status: 'done', correction_attempts: 1,
          reason: 'Hook text missing from first 3 seconds' },
        { clip_id: 304, stage: 'ready',      status: 'done', correction_attempts: 0, reason: null },
        { clip_id: 305, stage: 'didnt_pass', status: 'done', correction_attempts: 2,
          reason: 'Low formula score after 2 correction attempts' },
      ],
      last_event_id: '95',
      progress_n: 5,
      progress_total: 5,
      latest_detail: "3 ready · 2 didn't pass · source exhausted",
      latest_ts: _msAgo(5_000),
      stage_elapsed: { queued: 3, transcribing: 51, downloading: 30, identifying: 58,
                       identified: 4, rendering: 220, complete: 5 },
    },
  ],
};

// Approval-rate time series — keyed by campaign name
export const approvalRate = {
  peptides: {
    campaign: 'peptides',
    weeks: [
      { week_start: weekStart(7), approved: 3, rejected: 2, rate: 0.60,   profile_versions: [] },
      { week_start: weekStart(6), approved: 5, rejected: 1, rate: 0.833,  profile_versions: [1] },
      { week_start: weekStart(5), approved: 4, rejected: 3, rate: 0.571,  profile_versions: [1] },
      { week_start: weekStart(4), approved: 6, rejected: 1, rate: 0.857,  profile_versions: [1] },
      { week_start: weekStart(3), approved: 5, rejected: 2, rate: 0.714,  profile_versions: [1] },
      { week_start: weekStart(2), approved: 7, rejected: 1, rate: 0.875,  profile_versions: [2] },
      { week_start: weekStart(1), approved: 6, rejected: 2, rate: 0.750,  profile_versions: [2] },
      { week_start: weekStart(0), approved: 3, rejected: 1, rate: 0.750,  profile_versions: [2] },
    ],
    total_decisions: 39,
    enough_data: true,
  },
  demo_run: {
    campaign: 'demo_run',
    weeks: [
      { week_start: weekStart(1), approved: 1, rejected: 3, rate: 0.25,  profile_versions: [] },
      { week_start: weekStart(0), approved: 2, rejected: 1, rate: 0.667, profile_versions: [] },
    ],
    total_decisions: 7,
    enough_data: false,
  },
};

// Preference profile — keyed by campaign name
export const profile = {
  peptides: {
    campaign: 'peptides',
    version: 2,
    rules: [
      'Hook must reference a specific mechanism or surprising statistic about peptide science',
      'Speaker should be visible and credible in the first 2 seconds',
      'Clip should contain a clear, science-backed insight — not anecdote alone',
      'Avoid clips where dosing numbers are stated without context or safety caveats',
      'Duration between 35 and 58 seconds — not shorter, not longer',
      'No claims naming specific vendors or black-market sources',
      'Prefer clips where peer-reviewed studies are cited or on-screen',
      'BPC-157, TB-500, GHK-Cu, and Semax topics perform best',
    ],
    created_at: hAgo(24 * 3),
    meta: {
      decisions_count: 39,
      model: 'claude-sonnet-4-5',
      approved_examples: 31,
      rejected_examples: 8,
    },
  },
};

// Modal spend payload — contract §5
export const spend = {
  estimated: true,
  budget_usd: 30,
  month_to_date_usd: 4.32,
  remaining_credit_usd: 25.68,
  by_campaign: [
    { campaign: 'peptides',  usd: 3.10, jobs: 41 },
    { campaign: 'demo_run',  usd: 1.22, jobs: 12 },
  ],
  recent: [
    { clip_id: 'mock_clip_p001',  campaign: 'peptides',  gpu: 'l4', duration_s: 42.3, usd: 0.0094, created_at: hAgo(3)  },
    { clip_id: 'mock_clip_p_fail', campaign: 'peptides', gpu: 't4', duration_s: 38.7, usd: 0.0063, created_at: hAgo(5)  },
    { clip_id: 'mock_meme_001',   campaign: 'demo_run',  gpu: 'l4', duration_s: 18.2, usd: 0.0040, created_at: hAgo(12) },
  ],
  apify: {
    total_usd: 0.47,
    runs: 14,
    items: 152,
    by_kind: [
      { kind: 'discovery',  usd: 0.36, runs: 12, items: 120 },
      { kind: 'transcript', usd: 0.11, runs: 2,  items: 32  },
    ],
    avg_cost_per_video_usd: 0.003,
  },
  plan_note: 'Estimates based on recorded GPU duration × published rates (modal.com/pricing). Verify in Modal dashboard.',
};
