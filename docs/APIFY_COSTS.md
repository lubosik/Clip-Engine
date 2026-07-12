# APIFY_COSTS.md — real per-video costs and how we keep them down

Verified 2026-07-12 against the live Apify store API (BRONZE tier = our
Starter plan, account `kongwaT`, $29/month hard limit, cycle resets on the
12th of each month).

## Real prices per actor (pay-per-event, BRONZE tier)

| Actor | What we use it for | Price |
|---|---|---|
| `streamers/youtube-scraper` | YT discovery | **$0.003 / video result** (+$0.001/video if channel date-filter used) |
| `pintostudio/youtube-transcript-scraper` | YT transcripts | **$0.01 / video** (one dataset item per video) |
| `clockworks/free-tiktok-scraper` | TikTok discovery | $0.002 / video (+$0.001 add-ons per filter; $0.001/comment; $0.041/transcription-minute) |
| `agentx/tiktok-transcript` | TikTok transcripts | **$0.38 / transcript — 38x the YouTube path. AVOID.** |
| `apify/instagram-scraper` | IG discovery | $0.0023 / result |
| `apify/instagram-reel-scraper` | IG reels | $0.0023 / reel + $0.001 / start ($0.041/transcript, $0.015/video-download) |

## Cost per usable clip (YouTube path, the active one)

- Discovery: ~$0.003/video scraped. A 12-term × 10-result run ≈ 120 results ≈ **$0.36**.
- Transcript: **$0.01 per selected source** (cached forever after — re-runs are free).
- A typical demo run that mines 1-2 already-discovered podcasts spends **$0.01–$0.02** on Apify.
- The waste mode is re-running discovery: the same search terms re-bill every
  result (~$0.36+/run) even though we already have those sources in the DB.

## The levers now in code

1. **Real spend ledger** — every actor run writes an `apify_runs` row with the
   REAL billed `usageTotalUsd` (migration 004). `GET /api/spend` now returns an
   `apify` block: total, runs, items, by-kind, and `avg_cost_per_video_usd`.
2. **Backlog-first discovery skip** — when a campaign has ≥
   `sources.skip_discovery_backlog` (default 20) unfinished sources in the DB,
   paid discovery is SKIPPED entirely; the run mines the backlog (93 peptides
   sources were sitting there on 2026-07-12, 62 with cached transcripts =
   $0 Apify). `--force-discovery` (CLI) / `force_discovery: true` (POST
   /api/runs body) overrides.
3. **`results_per_search`** — per-campaign dial on discovery batch size
   (peptides set to 10 → ~$0.36/discovery run instead of $0.72).
4. **Real-cost spend guard** — `--max-apify-spend` now compares against the
   accumulated real `usageTotalUsd` (falls back to the $0.01/item estimate only
   when actors report no cost). Once the cap is hit mid-run, only sources with
   cached transcripts (zero Apify cost) continue.

## Upgrade paths not yet built

- **TikTok transcripts via local whisper**: download the video (yt-dlp) and
  transcribe with faster-whisper CPU (already in the Railway image). A 60s
  TikTok is seconds of CPU vs $0.38/actor call. Requires reordering
  `_process_source` (download before transcript) for TikTok only. Build this
  BEFORE enabling any TikTok-heavy campaign.
- Cheaper middle option: `clockworks/free-tiktok-scraper` charges
  $0.041/transcription-minute — a 1-minute TikTok ≈ $0.04 (10x cheaper than
  agentx) but the single-video input shape is unverified (see HANDOFF gotcha
  about actor input shapes — verify with a real call first).

## Budget math (Starter, $29/month)

At current settings a peptides demo run costs ≈$0.01–$0.36 (backlog vs fresh
discovery). A daily cron with discovery every run would burn ~$11/month on
re-scraping alone; with the backlog skip it drops to a few discovery runs per
month (only when the backlog drains below 20) ≈ **$1–2/month discovery +
$0.01/new source transcript** — comfortably inside $29.
