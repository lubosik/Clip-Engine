/**
 * sources.js — Sources view (revamp v3, 2026-07-29).
 *
 * In-progress panel (§4 of PROGRESS_EVENTS_CONTRACTS.md):
 *   - Discovers in-progress sources via GET /api/sources?in_progress=1.
 *   - Opens one EventSource per source at GET /api/sources/{id}/events.
 *     Native retry + Last-Event-ID header handle reconnection.
 *   - Cap: 3 concurrent EventSource connections; further sources poll the
 *     state endpoint every 4 s instead.
 *   - Initial render from GET /api/sources/{id}/events/state snapshot.
 *   - After 3 consecutive EventSource errors: fall back to polling state
 *     endpoint every 4 s (same rendering path as the cap-overflow case).
 *   - Stage-weighted progress bar (§4 percent map); 500 ms ease-out transition;
 *     no transition under prefers-reduced-motion.
 *   - Per-clip chip rail appears when progress_total is known (identified stage).
 *   - Live caption line = latest event.detail verbatim.
 *   - Stalled indicator: no event received for > 90 s while a non-terminal
 *     stage is active.
 *   - Terminal settle: stage=complete → mini history row with "X ready /
 *     Y didn't pass" linking to queue sections.
 *
 * Mock mode (localStorage.mock === "1"):
 *   - Reads localStorage.mockScene (default "midRendering") to pick a fixture
 *     from fixtures.inProgressScenes.
 *   - Renders directly from fixtures; no EventSource opened.
 *
 * History section is unchanged from v2.
 *
 * Exported API:
 *   initSources(container, ctx)
 */

// ── Module-level lifecycle handles ────────────────────────────────────────────

let _ctx        = null;   // shared context from app.js
let _esObserver = null;   // MutationObserver for view active/inactive
let _listTimer  = null;   // setInterval: re-discover in-progress sources

// Per-source live state: Map<sourceId, stateSnapshot>
// State snapshot shape mirrors GET /api/sources/{id}/events/state response.
const _sourceStates = new Map();

// Per-source transport: Map<sourceId, { esHandle, pollHandle, errorCount }>
const _sourceConns  = new Map();

// Rerender handle — debounced to batch multiple rapid events
let _renderPending = false;

// Container DOM reference (set in initSources)
let _container = null;

// Max concurrent SSE connections
const MAX_SSE = 3;

// Stall threshold: 90 s since last event/heartbeat
const STALL_MS = 90_000;

// Poll interval for fallback (overflow sources + SSE-error fallback)
const POLL_MS_SOURCE = 4_000;

// Discovery re-poll: check for newly added in-progress sources every 15 s
const DISCOVER_MS = 15_000;

// ── Platform SVG glyphs ───────────────────────────────────────────────────────

const PLATFORM_ICON = {
  youtube: `<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M22.54 6.42a2.78 2.78 0 00-1.94-1.96C18.88 4 12 4 12 4s-6.88 0-8.59.46a2.78 2.78 0 00-1.95 1.96A29 29 0 001 12a29 29 0 00.46 5.58A2.78 2.78 0 003.41 19.58C5.12 20 12 20 12 20s6.88 0 8.59-.42a2.78 2.78 0 001.94-1.97A29 29 0 0023 12a29 29 0 00-.46-5.58zM9.75 15.02V8.98L15.5 12l-5.75 3.02z"/>
  </svg>`,
  tiktok: `<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M19.59 6.69a4.83 4.83 0 01-3.77-4.25V2h-3.45v13.67a2.89 2.89 0 01-2.88 2.5 2.89 2.89 0 01-2.89-2.89 2.89 2.89 0 012.89-2.89c.28 0 .54.04.79.1V9.01a6.34 6.34 0 00-.79-.05 6.34 6.34 0 00-6.34 6.34 6.34 6.34 0 006.34 6.34 6.34 6.34 0 006.33-6.34V8.94a8.17 8.17 0 004.77 1.52V7a4.85 4.85 0 01-1-.31z"/>
  </svg>`,
  instagram: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
    <rect x="2" y="2" width="20" height="20" rx="5"/>
    <circle cx="12" cy="12" r="4"/>
    <circle cx="17.5" cy="6.5" r="1.2" fill="currentColor" stroke="none"/>
  </svg>`,
};

// Small correction glyph — used on chips that survived at least one correction
const CORRECTION_GLYPH = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none"
  stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"
  aria-label="corrected" class="chip-correction-glyph">
  <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/>
  <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/>
</svg>`;

function _platformIcon(platform) {
  return PLATFORM_ICON[platform] ||
    `<span style="font-size:10px;text-transform:uppercase;opacity:.5">${_esc(platform || '?')}</span>`;
}

function _esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function _fmtDate(isoStr) {
  if (!isoStr) return '—';
  const d = new Date(isoStr);
  if (isNaN(d)) return '—';
  return d.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
}

// ── Stage helpers (contract §4) ───────────────────────────────────────────────

/**
 * Stage-weighted percent map per §4:
 *   queued 2, transcribing 10, downloading 25, identifying 35, identified 40,
 *   then 40→95 proportional to terminal clips / total, complete 100.
 *
 * @param {{ stage: string, progress_total: number|null, clips_detail: Array }} state
 * @returns {number} 0–100
 */
function _barPercent(state) {
  const { stage, progress_total, clips_detail } = state;
  if (!stage || stage === 'failed') return 0;
  if (stage === 'complete') return 100;

  const FIXED = {
    queued: 2,
    transcribing: 10,
    downloading: 25,
    identifying: 35,
    identified: 40,
  };
  if (stage in FIXED) return FIXED[stage];

  // Post-identified stages: 40→95 based on terminal clips
  const total = progress_total || 0;
  if (total > 0) {
    const detail  = Array.isArray(clips_detail) ? clips_detail : [];
    const terminal = detail.filter(
      (c) => c.stage === 'ready' || c.stage === 'didnt_pass'
    ).length;
    return Math.min(95, 40 + Math.round(55 * terminal / total));
  }
  return 40;
}

/** Human-readable stage label for the overall stage line. */
function _stageLabel(state) {
  const { stage, progress_n, progress_total } = state;
  switch (stage) {
    case 'queued':       return 'Queued';
    case 'transcribing': return 'Transcribing…';
    case 'downloading':  return 'Downloading…';
    case 'identifying':  return 'Identifying clips…';
    case 'identified':   return progress_total != null
      ? `Found ${progress_total} clip${progress_total !== 1 ? 's' : ''}`
      : 'Clips identified';
    case 'pre_verify':   return 'Verifying clip';
    case 'rendering': {
      const n = progress_n || '?';
      const N = progress_total || '?';
      return `Rendering ${n} / ${N}`;
    }
    case 'reviewing':    return 'Reviewing clip';
    case 'correction':   return 'Correcting clip';
    case 'correcting':   return 'Correcting clip'; // source DB stage during re-renders
    case 'judging':      return 'Judging clip';
    case 'ready':        return 'Ready';
    case 'didnt_pass':   return "Didn't pass";
    case 'complete':     return 'Complete';
    case 'failed':       return 'Failed';
    default:             return stage || '—';
  }
}

/** Format seconds into human-readable elapsed string. */
function _fmtElapsed(seconds) {
  if (seconds == null || seconds < 0) return null;
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem > 0 ? `${m}m ${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm > 0 ? `${h}h ${rm}m` : `${h}h`;
}

/** Returns true if the source has not received an event for > STALL_MS. */
function _isStalled(state) {
  if (!state.latest_ts) return false;
  const terminal = ['complete', 'failed', 'ready', 'didnt_pass'];
  if (terminal.includes(state.stage)) return false;
  const tsMs = typeof state.latest_ts === 'number'
    ? state.latest_ts
    : new Date(state.latest_ts).getTime();
  return Date.now() - tsMs > STALL_MS;
}

// ── Clip chip state derivation ────────────────────────────────────────────────

/**
 * Derive a chip micro-state and render options from a clips_detail entry.
 *
 * clips_detail item shape (from state endpoint / SSE events):
 *   { clip_id, stage, status, correction_attempts, reason }
 *
 * @param {{ stage: string, status: string, correction_attempts: number, reason: string|null }} clip
 * @returns {{ label: string, cls: string, reason: string|null, showGlyph: boolean, revealable: boolean }}
 */
function _chipState(clip) {
  const { stage, status, correction_attempts, reason } = clip;
  const wasCorrected = (correction_attempts || 0) > 0;

  if (stage === 'ready') {
    return { label: 'ready', cls: 'chip-accent', reason: null, showGlyph: wasCorrected, revealable: false };
  }
  if (stage === 'didnt_pass') {
    return { label: "didn't pass", cls: 'chip-amber', reason, showGlyph: wasCorrected, revealable: !!reason };
  }
  if (stage === 'correction') {
    const n = correction_attempts || 1;
    return { label: `correcting (fix ${n}/2)`, cls: 'chip-amber chip-correcting', reason, showGlyph: false, revealable: !!reason };
  }
  if (stage === 'reviewing') {
    return { label: 'reviewing', cls: 'chip-reviewing', reason: null, showGlyph: false, revealable: false };
  }
  if (stage === 'rendering') {
    return { label: 'rendering', cls: 'chip-rendering', reason: null, showGlyph: false, revealable: false };
  }
  if (stage === 'pre_verify' && status === 'failed') {
    return { label: 'pre-verify failed', cls: 'chip-amber', reason, showGlyph: false, revealable: !!reason };
  }
  // Default: waiting (clip known but not yet started)
  return { label: 'waiting', cls: 'chip-waiting', reason: null, showGlyph: false, revealable: false };
}

/**
 * Build HTML for the per-clip chip rail.
 * Appears once progress_total is known (identified stage or later).
 *
 * @param {Array}        clipsDetail  clips_detail array from state snapshot
 * @param {number|null}  total        progress_total — total expected clips
 * @returns {string}
 */
function _buildChipRail(clipsDetail, total) {
  if (!total) return '';

  const detail = Array.isArray(clipsDetail) ? clipsDetail : [];
  const count  = Math.max(detail.length, total);

  const chips = [];
  for (let i = 0; i < count; i++) {
    const clip = detail[i] || null;
    const idx  = i + 1;  // 1-based chip number

    if (!clip) {
      // Clip is known (total set) but no event yet — show waiting chip
      chips.push(`<span class="chip chip-waiting chip-clip" aria-label="Clip ${idx}: waiting">${idx}</span>`);
      continue;
    }

    const { label, cls, reason, showGlyph, revealable } = _chipState(clip);
    const glyphHtml = showGlyph ? CORRECTION_GLYPH : '';
    const ariaLabel = `Clip ${idx}: ${label}`;

    if (revealable && reason) {
      // Amber chip: tap-to-reveal reason
      chips.push(`
        <details class="chip-reveal-wrap">
          <summary class="chip ${cls} chip-clip chip-revealable" aria-label="${_esc(ariaLabel)}">
            ${idx}${glyphHtml}
          </summary>
          <div class="chip-reveal-reason">${_esc(reason)}</div>
        </details>`);
    } else {
      chips.push(
        `<span class="chip ${cls} chip-clip" aria-label="${_esc(ariaLabel)}">${idx}${glyphHtml}</span>`
      );
    }
  }

  return `<div class="source-chip-rail" role="list" aria-label="Clip status rail">${chips.join('')}</div>`;
}

// ── In-progress card builder ──────────────────────────────────────────────────

/**
 * Build HTML for one in-progress source card from a state snapshot.
 * Handles every stage including complete (terminal) and failed.
 *
 * @param {object} state  State snapshot (mirrors state endpoint response)
 * @returns {string}
 */
function _buildProgressCard(state) {
  const {
    source_id, stage, title, url, platform, author_handle, campaign,
    thumbnail_url, stage_error, clips_detail, progress_total,
    latest_detail, stage_elapsed,
  } = state;

  const isFailed   = stage === 'failed';
  const isComplete = stage === 'complete';
  const pct        = _barPercent(state);
  const stageText  = _stageLabel(state);
  const stalled    = _isStalled(state);

  // ── Thumbnail ───────────────────────────────────────────────────────────────
  const thumbHtml = thumbnail_url
    ? `<img class="source-thumb" src="${_esc(thumbnail_url)}" alt="" loading="lazy"
           onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
       <div class="source-thumb-fallback" style="display:none">${_platformIcon(platform)}</div>`
    : `<div class="source-thumb-fallback">${_platformIcon(platform)}</div>`;

  // ── Chips row ───────────────────────────────────────────────────────────────
  const stageChipCls = isFailed ? 'chip-amber' : isComplete ? 'chip-accent' : '';
  const chipsRow = `
    <div class="chips-row" style="margin-bottom:4px">
      <span class="chip">${_platformIcon(platform)} ${_esc(platform || '—')}</span>
      ${author_handle ? `<span class="chip">@${_esc(author_handle)}</span>` : ''}
      <span class="chip">${_esc(campaign)}</span>
      <span class="chip ${stageChipCls}">${_esc(stageText)}</span>
    </div>`;

  // ── Progress bar (omitted for failed) ──────────────────────────────────────
  let barHtml = '';
  if (!isFailed && !isComplete) {
    barHtml = `
      <div class="source-stage-bar-track"
           role="progressbar"
           aria-valuenow="${pct}"
           aria-valuemin="0"
           aria-valuemax="100"
           aria-label="${_esc(stageText)} — ${pct}%">
        <div class="source-stage-bar-fill" style="width:${pct}%">
          <div class="source-stage-bar-streak" aria-hidden="true"></div>
        </div>
      </div>`;
  }

  // ── Elapsed + stalled ───────────────────────────────────────────────────────
  let elapsedHtml = '';
  const elapsedSecs = stage_elapsed?.[stage];
  const elapsedStr  = _fmtElapsed(elapsedSecs);
  if (elapsedStr && !isComplete) {
    const stalledBadge = stalled
      ? `<span class="source-stalled-badge" aria-label="Source may be stalled">stalled?</span>`
      : '';
    elapsedHtml = `
      <div class="source-stage-elapsed${stalled ? ' source-stage-elapsed--stalled' : ''}">
        ${_esc(stageText)} · ${_esc(elapsedStr)}${stalledBadge}
      </div>`;
  }

  // ── Live caption line ───────────────────────────────────────────────────────
  const captionHtml = latest_detail && !isComplete && !isFailed
    ? `<div class="source-caption-line">${_esc(latest_detail)}</div>`
    : '';

  // ── Chip rail (appears once clips are identified) ───────────────────────────
  const chipRailHtml = (!isFailed && !isComplete && progress_total != null)
    ? _buildChipRail(clips_detail, progress_total)
    : '';

  // ── Terminal summary (complete) ─────────────────────────────────────────────
  let terminalHtml = '';
  if (isComplete) {
    const detail       = Array.isArray(clips_detail) ? clips_detail : [];
    const readyCount   = detail.filter((c) => c.stage === 'ready').length;
    const failedCount  = detail.filter((c) => c.stage === 'didnt_pass').length;
    terminalHtml = `
      <div class="source-terminal-summary">
        <a href="#queue-ready"   class="source-terminal-link source-terminal-link--ready">
          ${readyCount} ready
        </a>
        <span class="source-terminal-sep"> / </span>
        <a href="#queue-failed"  class="source-terminal-link source-terminal-link--failed">
          ${failedCount} didn't pass
        </a>
      </div>`;
  }

  // ── Error block (failed) ────────────────────────────────────────────────────
  let errorHtml = '';
  if (isFailed && stage_error) {
    const truncated = stage_error.length > 200;
    errorHtml = `
      <div class="source-stage-error">
        ${!truncated
          ? `<p class="source-error-text">${_esc(stage_error)}</p>`
          : `<details class="source-error-details">
               <summary class="source-error-summary">${_esc(stage_error.slice(0, 200))}…</summary>
               <p class="source-error-full">${_esc(stage_error)}</p>
             </details>`
        }
      </div>`;
  }

  return `
    <article class="source-card source-inprogress-card${isFailed ? ' source-card--failed' : ''}${isComplete ? ' source-card--complete' : ''}"
             data-source-id="${_esc(source_id)}">
      <div class="source-card-media">${thumbHtml}</div>
      <div class="source-card-body">
        <div class="source-card-title">
          <a href="${_esc(url)}" target="_blank" rel="noopener noreferrer" class="source-title-link">
            ${_esc(title || source_id)}
          </a>
        </div>
        ${chipsRow}
        ${barHtml}
        ${captionHtml}
        ${chipRailHtml}
        ${elapsedHtml}
        ${terminalHtml}
        ${errorHtml}
      </div>
    </article>`;
}

// ── In-progress panel render ──────────────────────────────────────────────────

function _renderInProgress() {
  if (!_container) return;
  const section = _container.querySelector('.sources-inprogress-list');
  if (!section) return;

  if (_sourceStates.size === 0) {
    section.innerHTML = `
      <div class="sources-inprogress-empty text-muted">
        No sources currently processing.
      </div>`;
    return;
  }

  section.innerHTML = Array.from(_sourceStates.values())
    .map(_buildProgressCard)
    .join('');
}

/** Debounced re-render — batch rapid event bursts into one paint. */
function _scheduleRender() {
  if (_renderPending) return;
  _renderPending = true;
  requestAnimationFrame(() => {
    _renderPending = false;
    _renderInProgress();
  });
}

// ── Event application ─────────────────────────────────────────────────────────

/**
 * Apply a single parsed SSE event to the live state for that source.
 * The event wire schema (§2):
 *   { v, source_id, ts, stage, clip_id, progress: {n, total}, status, detail, reason }
 *
 * @param {object} state  Reference from _sourceStates (mutated in-place)
 * @param {object} evt    Parsed SSE JSON
 * @param {string} [sseId]  SSE `id:` field (Last-Event-ID)
 */
function _applyEvent(state, evt, sseId) {
  if (!evt || evt.v !== 1) return;

  state.stage = evt.stage;
  if (evt.detail) state.latest_detail = evt.detail;

  // Track latest_ts as Date.now() ms for stalled detection
  state.latest_ts = typeof evt.ts === 'string' ? new Date(evt.ts).getTime() : Date.now();

  if (sseId != null) state.last_event_id = String(sseId);

  // Update progress counters
  if (evt.progress) {
    if (evt.progress.total != null) state.progress_total = evt.progress.total;
    if (evt.progress.n     != null) state.progress_n     = evt.progress.n;
  }

  // Per-clip clip_detail update
  if (evt.clip_id != null) {
    if (!Array.isArray(state.clips_detail)) state.clips_detail = [];

    let clip = state.clips_detail.find((c) => c.clip_id === evt.clip_id);
    if (!clip) {
      clip = { clip_id: evt.clip_id, stage: '', status: '', correction_attempts: 0, reason: null };
      state.clips_detail.push(clip);
    }

    clip.stage  = evt.stage;
    clip.status = evt.status || '';

    if (evt.stage === 'correction') {
      clip.correction_attempts = (clip.correction_attempts || 0) + 1;
      if (evt.reason) clip.reason = evt.reason;
    }
    if ((evt.stage === 'didnt_pass' || evt.stage === 'judging') && evt.reason) {
      clip.reason = evt.reason;
    }
  }
}

// ── SSE transport per source ──────────────────────────────────────────────────

/** Count how many sources currently hold an open EventSource. */
function _activeSseCount() {
  let n = 0;
  for (const conn of _sourceConns.values()) {
    if (conn.esHandle) n++;
  }
  return n;
}

/**
 * Fetch the state snapshot for one source and merge it into _sourceStates.
 * Used for: initial render, polling fallback, and SSE reconnect seeding.
 */
async function _fetchAndMergeState(sourceId) {
  if (!_ctx) return;
  try {
    const snap = await _ctx.mockFetch(
      () => _ctx.api.getSourceEventsState(sourceId),
      () => {
        // Mock fallback: look up by sourceId in inProgressScenes
        const scene = localStorage.getItem('mockScene') || 'midRendering';
        const scenes = _ctx.fixtures.inProgressScenes || {};
        const list   = scenes[scene] || [];
        return list.find((s) => s.source_id === sourceId) || null;
      }
    );
    if (!snap) return;
    // Merge: preserve local latest_ts if newer (avoid stall regression on stale snap)
    const existing = _sourceStates.get(sourceId);
    const snapTs   = snap.latest_ts
      ? (typeof snap.latest_ts === 'number' ? snap.latest_ts : new Date(snap.latest_ts).getTime())
      : 0;
    const existing_ts = existing?.latest_ts || 0;
    const merged = { ...snap };
    if (existing_ts > snapTs) merged.latest_ts = existing_ts;
    _sourceStates.set(sourceId, merged);
  } catch {
    // Non-fatal; keep existing state
  }
}

/**
 * Start polling the state endpoint every POLL_MS_SOURCE for one source.
 * Used when: SSE cap exceeded, or after 3 consecutive SSE errors.
 */
function _startPollForSource(sourceId) {
  const conn = _sourceConns.get(sourceId);
  if (!conn || conn.pollHandle) return;

  const tick = async () => {
    await _fetchAndMergeState(sourceId);
    _scheduleRender();
  };
  conn.pollHandle = setInterval(tick, POLL_MS_SOURCE);
}

/**
 * Open an EventSource for one source.
 * - Passes last_event_id as query param for initial connect; browser sends
 *   Last-Event-ID header on any automatic reconnect (native SSE resume).
 * - After 3 consecutive errors: closes ES, starts polling fallback.
 */
function _openSSE(sourceId) {
  const conn = _sourceConns.get(sourceId);
  if (!conn) return;
  if (conn.esHandle) { conn.esHandle.close(); conn.esHandle = null; }

  const state  = _sourceStates.get(sourceId);
  const lastId = state?.last_event_id;
  const qs     = lastId ? `?last_event_id=${encodeURIComponent(lastId)}` : '';
  const url    = `/api/sources/${encodeURIComponent(sourceId)}/events${qs}`;

  let es;
  try {
    es = new EventSource(url);
  } catch {
    // EventSource constructor failed — fall back to polling immediately
    _startPollForSource(sourceId);
    return;
  }

  conn.esHandle   = es;
  conn.errorCount = 0;

  es.addEventListener('progress', (evt) => {
    conn.errorCount = 0;  // reset consecutive error counter on any message
    try {
      const parsed = JSON.parse(evt.data);
      const src    = _sourceStates.get(sourceId);
      if (src) {
        _applyEvent(src, parsed, evt.lastEventId);
        _scheduleRender();
      }
    } catch { /* ignore parse errors */ }
  });

  es.addEventListener('error', () => {
    conn.errorCount = (conn.errorCount || 0) + 1;
    if (conn.errorCount >= 3) {
      // Three consecutive errors — give up on SSE, poll instead
      es.close();
      conn.esHandle = null;
      _startPollForSource(sourceId);
    }
    // For fewer errors: let EventSource retry natively (browser respects retry: 3000)
  });
}

/**
 * Connect one source: fetch initial state snapshot, then open SSE or poll
 * depending on how many SSE slots are available.
 */
async function _connectSource(sourceId) {
  if (_sourceConns.has(sourceId)) return;  // already managed

  // Register connection record
  _sourceConns.set(sourceId, { esHandle: null, pollHandle: null, errorCount: 0 });

  // Fetch initial snapshot
  await _fetchAndMergeState(sourceId);
  _scheduleRender();

  // Decide: SSE or poll
  if (_activeSseCount() < MAX_SSE) {
    _openSSE(sourceId);
  } else {
    _startPollForSource(sourceId);
  }
}

/** Disconnect and clean up all transports for one source. */
function _disconnectSource(sourceId) {
  const conn = _sourceConns.get(sourceId);
  if (!conn) return;
  if (conn.esHandle)   { conn.esHandle.close();     conn.esHandle   = null; }
  if (conn.pollHandle) { clearInterval(conn.pollHandle); conn.pollHandle = null; }
  _sourceConns.delete(sourceId);
  _sourceStates.delete(sourceId);
}

/** Disconnect all managed sources and stop discovery polling. */
function _cleanupLive() {
  if (_listTimer) { clearInterval(_listTimer); _listTimer = null; }
  for (const id of [..._sourceConns.keys()]) _disconnectSource(id);
}

// ── In-progress source discovery ──────────────────────────────────────────────

/**
 * Fetch the current list of in-progress sources and connect any new ones.
 * Removed sources (no longer in_progress) are disconnected and dropped.
 */
async function _discoverSources() {
  if (!_ctx) return;

  let list;
  try {
    list = await _ctx.mockFetch(
      () => _ctx.api.getSourcesProgress(),
      () => {
        // In mock mode, the discovery list comes from inProgressScenes
        const scene  = localStorage.getItem('mockScene') || 'midRendering';
        const scenes = _ctx.fixtures.inProgressScenes || {};
        return scenes[scene] || [];
      }
    );
  } catch {
    return;  // Network failure — keep existing connections
  }

  if (!Array.isArray(list)) return;

  const currentIds = new Set(list.map((s) => s.source_id));

  // Connect newly discovered sources
  for (const src of list) {
    if (!_sourceConns.has(src.source_id)) {
      // Seed the state map with the list-level fields so we can render
      // something immediately before the state-endpoint fetch completes.
      _sourceStates.set(src.source_id, {
        source_id:      src.source_id,
        stage:          src.stage || 'queued',
        title:          src.title || src.source_id,
        url:            src.url || '#',
        platform:       src.platform || '',
        author_handle:  src.author_handle || null,
        campaign:       src.campaign || '',
        thumbnail_url:  src.thumbnail_url || null,
        stage_error:    src.stage_error || null,
        clips_detail:   src.clips_detail || [],
        last_event_id:  null,
        progress_n:     src.clips_rendered || null,
        progress_total: src.clips_identified || null,
        latest_detail:  null,
        latest_ts:      null,
        stage_elapsed:  {},
      });
      await _connectSource(src.source_id);
    }
  }

  // Disconnect sources that are no longer in-progress
  for (const id of [..._sourceConns.keys()]) {
    if (!currentIds.has(id)) _disconnectSource(id);
  }

  _scheduleRender();
}

// ── Live updates init ─────────────────────────────────────────────────────────

function _startLiveUpdates() {
  _cleanupLive();

  // Mock mode: load directly from fixture; no SSE
  if (localStorage.getItem('mock') === '1') {
    const scene  = localStorage.getItem('mockScene') || 'midRendering';
    const scenes = (_ctx?.fixtures?.inProgressScenes) || {};
    const list   = scenes[scene] || [];
    _sourceStates.clear();
    for (const snap of list) {
      _sourceStates.set(snap.source_id, {
        ...snap,
        // Ensure latest_ts is a ms number for stalled detection
        latest_ts: snap.latest_ts
          ? (typeof snap.latest_ts === 'number' ? snap.latest_ts : new Date(snap.latest_ts).getTime())
          : null,
      });
    }
    _renderInProgress();
    return;
  }

  // Live mode: discover sources, then poll for new ones periodically
  _discoverSources();
  _listTimer = setInterval(_discoverSources, DISCOVER_MS);
}

// ── History section helpers (unchanged from v2) ───────────────────────────────

function _statusLabel(status) {
  switch (status) {
    case 'done':           return { label: 'Fully used',   cls: 'chip-accent' };
    case 'partially_done': return { label: 'Partial',      cls: 'chip-amber'  };
    case 'selected':       return { label: 'In progress',  cls: ''            };
    default:               return { label: status || '—',  cls: ''            };
  }
}

function _gateLabel(gate_status) {
  switch (gate_status) {
    case 'ready':      return { label: 'Ready',      cls: 'chip-accent' };
    case 'overridden': return { label: 'Overridden', cls: 'chip-amber'  };
    case 'didnt_pass': return { label: 'Failed',     cls: 'chip-amber'  };
    default:           return { label: 'Pending',    cls: ''            };
  }
}

function _exhaustionChip(exhaustion) {
  switch (exhaustion) {
    case 'fully_exhausted': return `<span class="chip chip-accent exhaust-chip">Fully exhausted</span>`;
    case 'partially_used':  return `<span class="chip chip-amber exhaust-chip">Partially used</span>`;
    default:                return '';
  }
}

function _buildHistoryCard(src) {
  const { label: statusLabel, cls: statusCls } = _statusLabel(src.status);

  const thumbHtml = src.thumbnail_url
    ? `<img class="source-thumb" src="${_esc(src.thumbnail_url)}" alt="" loading="lazy"
           onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
       <div class="source-thumb-fallback" style="display:none">${_platformIcon(src.platform)}</div>`
    : `<div class="source-thumb-fallback">${_platformIcon(src.platform)}</div>`;

  const clipsHtml = (src.clips || []).map((c) => {
    const { label: gLabel, cls: gCls } = _gateLabel(c.gate_status);
    return `
      <div class="source-clip-row">
        <span class="source-clip-id text-muted">#${_esc(c.id)}</span>
        <span class="source-clip-hook">${_esc(c.hook || '—')}</span>
        <span class="chip ${gCls}" style="flex-shrink:0">${_esc(gLabel)}</span>
      </div>`;
  }).join('');

  const clipsSection = src.clip_count > 0
    ? `<details class="source-clips-details">
         <summary class="source-clips-summary">${src.clip_count} clip${src.clip_count !== 1 ? 's' : ''} produced</summary>
         <div class="source-clips-list">${clipsHtml}</div>
       </details>`
    : `<div class="source-clips-empty text-muted">No clips yet</div>`;

  const parts = [];
  if (src.clips_identified != null) parts.push(`${src.clips_identified} identified`);
  if (src.clips_rendered   != null) parts.push(`${src.clips_rendered} rendered`);
  if (src.clips_approved   != null) parts.push(`${src.clips_approved} approved`);
  if (src.clips_rejected   != null) parts.push(`${src.clips_rejected} rejected`);
  const countsHtml = parts.length
    ? `<div class="source-clip-counts text-muted">${parts.join(' · ')}</div>`
    : '';

  return `
    <article class="source-card" data-source-id="${_esc(src.source_id)}">
      <div class="source-card-media">${thumbHtml}</div>
      <div class="source-card-body">
        <div class="source-card-title">
          <a href="${_esc(src.url)}" target="_blank" rel="noopener noreferrer" class="source-title-link">
            ${_esc(src.title || src.source_id)}
          </a>
        </div>
        <div class="chips-row" style="margin-bottom:6px">
          <span class="chip">${_platformIcon(src.platform)} ${_esc(src.platform || '—')}</span>
          ${src.author_handle ? `<span class="chip">@${_esc(src.author_handle)}</span>` : ''}
          <span class="chip">${_esc(src.campaign)}</span>
          <span class="chip ${statusCls}">${_esc(statusLabel)}</span>
          ${_exhaustionChip(src.exhaustion)}
        </div>
        <div class="source-card-meta text-muted">
          ${src.processed_at ? `Processed ${_fmtDate(src.processed_at)}` : 'Not yet processed'}
          ${src.used_ranges_count > 0 ? ` · ${src.used_ranges_count} range${src.used_ranges_count !== 1 ? 's' : ''} used` : ''}
        </div>
        ${countsHtml}
        ${clipsSection}
      </div>
    </article>`;
}

// ── Filter ────────────────────────────────────────────────────────────────────

function _filter(sources, query) {
  if (!query) return sources;
  const q = query.toLowerCase();
  return sources.filter((s) => {
    const fields = [s.title, s.author_handle, s.campaign, s.platform, s.url, s.source_id];
    return fields.some((f) => (f || '').toLowerCase().includes(q));
  });
}

// ── Render history ────────────────────────────────────────────────────────────

function _renderHistory(sources, query) {
  if (!_container) return;
  const list = _container.querySelector('.sources-history-list');
  if (!list) return;
  const filtered = _filter(sources, query);

  if (filtered.length === 0) {
    list.innerHTML = `
      <div class="sources-empty">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"
             style="color:var(--text-3);margin-bottom:12px" aria-hidden="true">
          <rect x="2" y="2" width="20" height="20" rx="2.18"/>
          <line x1="7" y1="2" x2="7" y2="22"/>
          <line x1="17" y1="2" x2="17" y2="22"/>
          <line x1="2" y1="12" x2="22" y2="12"/>
        </svg>
        <p>${query ? 'No sources match your search.' : 'No mined sources yet. Run a campaign to see sources here.'}</p>
      </div>`;
    return;
  }

  list.innerHTML = filtered.map(_buildHistoryCard).join('');
}

// ── Init ──────────────────────────────────────────────────────────────────────

/**
 * @param {HTMLElement} container  The #view-sources element
 * @param {{ api, fixtures, mockFetch, toast, onUnauthorized }} ctx
 */
export function initSources(container, ctx) {
  // Clean up prior session
  _cleanupLive();
  if (_esObserver) { _esObserver.disconnect(); _esObserver = null; }

  _ctx       = ctx;
  _container = container;
  _sourceStates.clear();
  _sourceConns.clear();

  let _historySources = [];
  let _query          = '';

  // ── DOM skeleton ────────────────────────────────────────────────────────────
  container.innerHTML = `
    <div class="sources-section">
      <div class="sources-section-header">In progress</div>
      <div class="sources-inprogress-list" aria-live="polite" aria-label="In-progress sources">
        <div class="sources-loading text-muted">Loading…</div>
      </div>
    </div>

    <div class="sources-section">
      <div class="sources-section-header">History</div>
      <div class="sources-history-header">
        <input
          type="search"
          class="sources-search form-control"
          placeholder="Search by title, creator, campaign…"
          aria-label="Search sources"
        >
      </div>
      <div class="sources-history-list" aria-live="polite" aria-label="Source video history">
        <div class="sources-loading text-muted">Loading sources…</div>
      </div>
    </div>`;

  // ── Live in-progress updates ────────────────────────────────────────────────
  _startLiveUpdates();

  // Pause/resume live updates when the view is deactivated/reactivated
  _esObserver = new MutationObserver(() => {
    if (container.classList.contains('active')) {
      _startLiveUpdates();
    } else {
      _cleanupLive();
    }
  });
  _esObserver.observe(container, { attributeFilter: ['class'] });

  // ── History fetch ───────────────────────────────────────────────────────────
  ctx.mockFetch(
    () => ctx.api.getSources(),
    () => ctx.fixtures.sources || [],
  ).then((data) => {
    _historySources = Array.isArray(data) ? data : [];
    _renderHistory(_historySources, _query);
  }).catch((err) => {
    if (err && err.status === 401) { ctx.onUnauthorized(); return; }
    const histList = container.querySelector('.sources-history-list');
    if (histList) {
      histList.innerHTML = `<div class="sources-empty text-muted">Could not load sources. Check your connection.</div>`;
    }
  });

  // ── History search ──────────────────────────────────────────────────────────
  const searchEl = container.querySelector('.sources-search');
  let _debounce  = null;
  searchEl.addEventListener('input', () => {
    clearTimeout(_debounce);
    _debounce = setTimeout(() => {
      _query = searchEl.value.trim();
      _renderHistory(_historySources, _query);
    }, 200);
  });
}
