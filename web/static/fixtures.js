/**
 * fixtures.js — static mock data for demo / offline mode.
 * Activated when: localStorage.mock === "1" AND a real /api fetch fails
 * with a network error (i.e. the server is not running).
 *
 * Shape mirrors the live API contract in ARCHITECTURE.md §5 exactly.
 */

const NOW = Date.now();
const hAgo = (h) => new Date(NOW - h * 3_600_000).toISOString();
const hLater = (h) => new Date(NOW + h * 3_600_000).toISOString();

export const stats = {
  pending: 3,
  approved: 14,
  scheduled: 6,
  posted: 52,
  next_run_at: hLater(2),
};

export const campaigns = [
  {
    name: 'fitness',
    enabled: true,
    sources_summary: 'YouTube (3 terms) · TikTok (2 hashtags)',
    schedule: '1/day at 17:00 ET',
    last_run_at: hAgo(3),
    pending_count: 3,
  },
];

// Clip fixture — video_url is null in mock mode; the <video> element
// shows a black placeholder. thumb_url is also null.
export const clips = [
  {
    id: 'mock_clip_001',
    campaign: 'fitness',
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
        { week_start: weekStart(5), views: 12400, likes: 890, comments: 123, shares: 45, posts: 7 },
        { week_start: weekStart(4), views: 18200, likes: 1340, comments: 198, shares: 76, posts: 7 },
        { week_start: weekStart(3), views: 15600, likes: 1100, comments: 145, shares: 58, posts: 7 },
        { week_start: weekStart(2), views: 22100, likes: 1890, comments: 267, shares: 112, posts: 7 },
        { week_start: weekStart(1), views: 19800, likes: 1560, comments: 234, shares: 89, posts: 7 },
        { week_start: weekStart(0), views: 8200, likes: 640, comments: 89, shares: 34, posts: 3 },
      ],
    },
    {
      channel: 'instagram_fitness',
      weekly: [
        { week_start: weekStart(5), views: 8300, likes: 1240, comments: 67, shares: 23, posts: 7 },
        { week_start: weekStart(4), views: 11200, likes: 1780, comments: 89, shares: 34, posts: 7 },
        { week_start: weekStart(3), views: 9800, likes: 1560, comments: 78, shares: 28, posts: 7 },
        { week_start: weekStart(2), views: 14500, likes: 2340, comments: 134, shares: 56, posts: 7 },
        { week_start: weekStart(1), views: 12100, likes: 1980, comments: 112, shares: 45, posts: 7 },
        { week_start: weekStart(0), views: 5400, likes: 876, comments: 48, shares: 19, posts: 3 },
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
    },
    {
      clip_id: 'mock_a3',
      hook: 'This training method builds 50 % more muscle in half the time',
      platform: 'tiktok',
      permalink: 'https://tiktok.com/@viciresearch/video/mock3',
      views: 34100,
      likes: 2800,
      comments: 156,
      shares: 123,
      posted_at: hAgo(24 * 10),
    },
  ],
};
