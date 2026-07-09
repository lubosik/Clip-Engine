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
    caption: 'When someone says they train twice a day 😂\n\n#gymhumor #fitness #gymtok',
    source: null,
    start: null,
    end: null,
    duration: null,
    destination_channels: ['instagram_fitness'],
    proposed_slot: hLater(7),
    created_at: hAgo(2),
    video_url: null,
    thumb_url: null,
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
  plan_note: 'Estimates based on recorded GPU duration × published rates (modal.com/pricing). Verify in Modal dashboard.',
};
