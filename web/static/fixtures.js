/**
 * fixtures.js — static mock data for demo / offline mode.
 * Activated when: localStorage.mock === "1" AND a real /api fetch fails
 * with a network error (i.e. the server is not running).
 *
 * Shape mirrors the live API contract in REVAMP_CONTRACTS.md §6 exactly.
 * Updated for revamp v2: kind/mode/aspect on clips, schedule object on
 * campaigns, sources_summary array, engines, spend payload, hero nulls.
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
    name: 'fitness',
    enabled: true,
    mode: 'production',
    sources_summary: [
      { platform: 'youtube', count: 3, label: 'YouTube · 3 terms' },
      { platform: 'tiktok',  count: 2, label: 'TikTok · 2 hashtags' },
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
// Mix: 3 regular clips (9:16), 2 memes (1:1 and 4:5).
export const clips = [
  {
    id: 'mock_clip_001',
    campaign: 'fitness',
    kind: 'clip',
    mode: 'production',
    aspect: '9:16',
    hook: 'Most people are leaving 40 % of their gains on the table with this one mistake',
    score: 0.92,
    reason: 'Strong hook, clear actionable insight, no unsafe claims',
    caption:
      'Most people are leaving 40 % of their gains on the table with this one mistake\n\nvia @HouseofHypertrophy\n#fitness #hypertrophy #protein #gymtok',
    source: {
      handle: 'HouseofHypertrophy',
      url: 'https://youtube.com/watch?v=mock001',
      title: 'The Complete Guide to Progressive Overload',
      platform: 'youtube',
    },
    start: 342.5,
    end: 398.2,
    duration: 55.7,
    destination_channels: ['tiktok_fitness', 'instagram_fitness'],
    proposed_slot: hLater(4),
    created_at: hAgo(0.5),
    video_url: null,
    thumb_url: null,
    gate_status: 'ready',
    gate_reasons: [
      { phase: '1', check: 'resolution', pass: true, reason: '1080x1920 OK' },
      { phase: '1', check: 'hook_present_in_hook_frame', pass: true, reason: 'Hook text visible in hook frame' },
      { phase: '1', check: 'real_humans', pass: true, reason: 'Real human speaker detected' },
      { phase: '2', check: 'formula_score', pass: true, reason: 'Score 0.92 >= threshold 0.60' },
    ],
    formula_score: 0.92,
  },
  {
    id: 'mock_clip_002',
    campaign: 'fitness',
    kind: 'clip',
    mode: 'production',
    aspect: '9:16',
    hook: "This is why you're not building muscle despite training hard",
    score: 0.87,
    reason: 'Addresses common pain point, good standalone value',
    caption:
      "This is why you're not building muscle despite training hard\n\nvia @JeffNippard\n#fitness #hypertrophy #gymtok",
    source: {
      handle: 'JeffNippard',
      url: 'https://youtube.com/watch?v=mock002',
      title: 'Science of Muscle Growth',
      platform: 'youtube',
    },
    start: 120.0,
    end: 175.5,
    duration: 55.5,
    destination_channels: ['tiktok_fitness'],
    proposed_slot: hLater(5),
    created_at: hAgo(0.75),
    video_url: null,
    thumb_url: null,
    gate_status: 'ready',
    gate_reasons: [
      { phase: '1', check: 'resolution', pass: true, reason: '1080x1920 OK' },
      { phase: '1', check: 'speaker_centered', pass: true, reason: 'Speaker within center region' },
      { phase: '2', check: 'formula_score', pass: true, reason: 'Score 0.87 >= threshold 0.60' },
    ],
    formula_score: 0.87,
  },
  {
    id: 'mock_clip_003',
    campaign: 'fitness',
    kind: 'clip',
    mode: 'demo',
    aspect: '9:16',
    hook: 'The optimal protein intake per meal is not what you think',
    score: 0.78,
    reason: 'Surprising fact, science-backed, broad appeal',
    caption:
      'The optimal protein intake per meal is not what you think\n\nvia @AlexanderFergus\n#fitness #protein #gymtok',
    source: {
      handle: 'AlexanderFergus',
      url: 'https://youtube.com/watch?v=mock003',
      title: 'Protein Timing Deep Dive',
      platform: 'youtube',
    },
    start: 68.0,
    end: 112.0,
    duration: 44.0,
    destination_channels: ['tiktok_fitness', 'instagram_fitness'],
    proposed_slot: hLater(6),
    created_at: hAgo(1),
    video_url: null,
    thumb_url: null,
    gate_status: 'didnt_pass',
    gate_reasons: [
      { phase: '1', check: 'watermark_visible', pass: false, reason: 'No watermark detected in any frame' },
      { phase: '1', check: 'captions_present', pass: false, reason: 'No word-by-word captions found in mid-clip frame' },
    ],
    formula_score: null,
  },
  // Meme fixtures
  {
    id: 'mock_meme_001',
    campaign: 'demo_run',
    kind: 'meme',
    mode: 'demo',
    aspect: '1:1',
    hook: 'When someone says they train twice a day',
    score: 0.83,
    reason: 'On-brand humor, relatable, no unsafe content',
    caption: 'When someone says they train twice a day \u{1F602}\n\n#gymhumor #fitness #gymtok',
    source: null,
    start: null,
    end: null,
    duration: null,
    destination_channels: ['instagram_fitness'],
    proposed_slot: hLater(7),
    created_at: hAgo(2),
    video_url: null,
    thumb_url: null,
    gate_status: 'pending',
    gate_reasons: null,
    formula_score: null,
    meme_meta: {
      concept: 'relatable gym humor — overtraining archetype',
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
    hook: 'Progressive overload but make it aesthetic',
    score: 0.76,
    reason: 'Clean format, on-brand visual style',
    caption: 'Progressive overload but make it aesthetic\n\n#fitness #gains #gymlife',
    source: null,
    start: null,
    end: null,
    duration: null,
    destination_channels: ['tiktok_fitness', 'instagram_fitness'],
    proposed_slot: hLater(8),
    created_at: hAgo(3),
    video_url: null,
    thumb_url: null,
    gate_status: 'pending',
    gate_reasons: null,
    formula_score: null,
    meme_meta: {
      concept: 'aspirational training aesthetic with structured caption',
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
      channel: 'tiktok_fitness',
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
      channel: 'instagram_fitness',
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
      hook: 'Most people are leaving 40 % of their gains on the table',
      platform: 'tiktok',
      permalink: 'https://tiktok.com/@viciresearch/video/mock1',
      views: 45200,
      likes: 3400,
      comments: 234,
      shares: 178,
      posted_at: hAgo(24 * 8),
      mode: 'production',
      kind: 'clip',
      campaign: 'fitness',
    },
    {
      clip_id: 'mock_a2',
      hook: 'The optimal protein intake per meal is not what you think',
      platform: 'instagram',
      permalink: 'https://instagram.com/reel/mockABC',
      views: 38900,
      likes: 5600,
      comments: 189,
      shares: 145,
      posted_at: hAgo(24 * 9),
      mode: 'production',
      kind: 'clip',
      campaign: 'fitness',
    },
    {
      clip_id: 'mock_a3',
      hook: 'When someone says they train twice a day',
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

// Sources — mock data for the Sources view (history tab)
// Includes new pipeline fields: stage, clips_identified, clips_rendered,
// clips_approved, clips_rejected, clips_pending, exhaustion
export const sources = [
  {
    id: 1,
    source_id: 'youtube:mock001',
    platform: 'youtube',
    url: 'https://youtube.com/watch?v=mock001',
    title: 'The Complete Guide to Progressive Overload',
    author_handle: 'HouseofHypertrophy',
    campaign: 'fitness',
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
      { id: '1', hook: 'Most people leave 40% of gains on the table', status: 'approved', gate_status: 'ready' },
      { id: '2', hook: 'Progressive overload is about more than just weight', status: 'pending_review', gate_status: 'ready' },
      { id: '3', hook: 'The optimal rep range myth explained', status: 'rejected', gate_status: 'didnt_pass' },
    ],
    used_ranges_count: 3,
    thumbnail_url: 'https://i.ytimg.com/vi/mock001/hqdefault.jpg',
  },
  {
    id: 2,
    source_id: 'youtube:mock002',
    platform: 'youtube',
    url: 'https://youtube.com/watch?v=mock002',
    title: 'Science of Muscle Growth',
    author_handle: 'JeffNippard',
    campaign: 'fitness',
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
      { id: '4', hook: "This is why you're not building muscle", status: 'pending_review', gate_status: 'ready' },
    ],
    used_ranges_count: 1,
    thumbnail_url: 'https://i.ytimg.com/vi/mock002/hqdefault.jpg',
  },
  {
    id: 3,
    source_id: 'tiktok:mock003',
    platform: 'tiktok',
    url: 'https://tiktok.com/@fitnessguru/video/mock003',
    title: 'Quick protein hack every lifter needs',
    author_handle: 'fitnessguru',
    campaign: 'fitness',
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

// In-progress sources — shown in the live "In progress" panel (SSE / polling)
// Three stages: rendering 3/10, reviewing 2/5 approved, failed
export const sourcesProgress = [
  {
    id: 4,
    source_id: 'youtube:inprog001',
    platform: 'youtube',
    url: 'https://youtube.com/watch?v=inprog001',
    title: 'Advanced Muscle Building Techniques That Actually Work',
    author_handle: 'ScottHermanFitness',
    campaign: 'fitness',
    status: 'selected',
    stage: 'rendering',
    clips_identified: 10,
    clips_rendered: 3,
    clips_approved: 0,
    clips_rejected: 0,
    clips_pending: 3,
    stage_error: null,
    stage_updated_at: hAgo(0.1),
    exhaustion: 'in_progress',
    processed_at: hAgo(0.5),
    clip_count: 3,
    clips: [],
    used_ranges_count: 3,
    thumbnail_url: 'https://i.ytimg.com/vi/inprog001/hqdefault.jpg',
  },
  {
    id: 5,
    source_id: 'youtube:inprog002',
    platform: 'youtube',
    url: 'https://youtube.com/watch?v=inprog002',
    title: 'The TRUTH About Intermittent Fasting',
    author_handle: 'ThomasDeLauerFit',
    campaign: 'fitness',
    status: 'selected',
    stage: 'reviewing',
    clips_identified: 7,
    clips_rendered: 5,
    clips_approved: 2,
    clips_rejected: 1,
    clips_pending: 2,
    stage_error: null,
    stage_updated_at: hAgo(0.25),
    exhaustion: 'in_progress',
    processed_at: hAgo(1),
    clip_count: 5,
    clips: [],
    used_ranges_count: 5,
    thumbnail_url: null,
  },
  {
    id: 6,
    source_id: 'tiktok:inprog003',
    platform: 'tiktok',
    url: 'https://tiktok.com/@infusedperformance/video/inprog003',
    title: 'Creatine supplementation guide',
    author_handle: 'infusedperformance',
    campaign: 'fitness',
    status: 'selected',
    stage: 'failed',
    clips_identified: null,
    clips_rendered: 0,
    clips_approved: 0,
    clips_rejected: 0,
    clips_pending: 0,
    stage_error: 'Apify transcript actor failed: ACTOR_TIMED_OUT after 120s. Video may be age-restricted or has restricted access. Check that the TikTok account is public and not geo-blocked.',
    stage_updated_at: hAgo(0.5),
    exhaustion: 'in_progress',
    processed_at: null,
    clip_count: 0,
    clips: [],
    used_ranges_count: 0,
    thumbnail_url: null,
  },
];

// Approved clips — shown in the Queue "Approved" collapsible section
// Separate from fixtures.clips to avoid polluting the pending queue in mock mode
export const approvedClips = [
  {
    id: 'mock_clip_approved_001',
    campaign: 'fitness',
    kind: 'clip',
    mode: 'production',
    aspect: '9:16',
    hook: 'Sleep is the most underrated muscle builder — here is the science',
    score: 0.89,
    status: 'approved',
    caption: 'Sleep is the most underrated muscle builder\n\nvia @HouseofHypertrophy\n#fitness #sleep #recovery #hypertrophy',
    source: { handle: 'HouseofHypertrophy', url: 'https://youtube.com/watch?v=mock001', title: 'The Complete Guide to Progressive Overload', platform: 'youtube' },
    start: 480,
    end: 535,
    duration: 55,
    destination_channels: ['tiktok_fitness'],
    proposed_slot: hLater(10),
    scheduled_at: null,
    created_at: hAgo(2),
    video_url: null,
    thumb_url: null,
    gate_status: 'ready',
    gate_reasons: [],
    formula_score: 0.89,
    review_feedback: { action: 'approved', reasons: [], note: null, decided_at: hAgo(1.5) },
  },
  {
    id: 'mock_clip_approved_002',
    campaign: 'fitness',
    kind: 'clip',
    mode: 'production',
    aspect: '9:16',
    hook: 'You only need 3 exercises to build a complete upper body',
    score: 0.82,
    status: 'scheduled',
    caption: 'You only need 3 exercises to build a complete upper body\n\nvia @JeffNippard\n#fitness #gym #training',
    source: { handle: 'JeffNippard', url: 'https://youtube.com/watch?v=mock002', title: 'Science of Muscle Growth', platform: 'youtube' },
    start: 200,
    end: 255,
    duration: 55,
    destination_channels: ['tiktok_fitness', 'instagram_fitness'],
    proposed_slot: hLater(14),
    scheduled_at: hLater(14),
    created_at: hAgo(3),
    video_url: null,
    thumb_url: null,
    gate_status: 'ready',
    gate_reasons: [],
    formula_score: 0.82,
    review_feedback: { action: 'approved', reasons: [], note: null, decided_at: hAgo(2) },
  },
];

// Approval-rate time series — keyed by campaign name
export const approvalRate = {
  fitness: {
    campaign: 'fitness',
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
  fitness: {
    campaign: 'fitness',
    version: 2,
    rules: [
      'Hook must be a surprising or counterintuitive claim about fitness science',
      'Speaker should be visible and energetic in the first 2 seconds',
      'Clip should contain a clear actionable insight, not just motivation',
      'Avoid clips where the speaker reads from notes or looks away from camera',
      'Duration between 35 and 58 seconds — not shorter, not longer',
      'No supplement claims that name specific brands',
      'Prefer clips where data or studies are cited explicitly',
      'Clips about protein, progressive overload, or sleep perform best',
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
    { campaign: 'fitness',  usd: 3.10, jobs: 41 },
    { campaign: 'demo_run', usd: 1.22, jobs: 12 },
  ],
  recent: [
    { clip_id: 'mock_clip_001', campaign: 'fitness',  gpu: 'l4', duration_s: 42.3, usd: 0.0094, created_at: hAgo(3)  },
    { clip_id: 'mock_clip_002', campaign: 'fitness',  gpu: 't4', duration_s: 38.7, usd: 0.0063, created_at: hAgo(5)  },
    { clip_id: 'mock_meme_001', campaign: 'demo_run', gpu: 'l4', duration_s: 18.2, usd: 0.0040, created_at: hAgo(12) },
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
