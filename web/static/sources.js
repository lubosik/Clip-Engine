/**
 * sources.js — Sources view.
 *
 * Two sections (top to bottom):
 *   1. "In progress" — live via EventSource('/api/sources/stream'), falls back
 *      to 5-second polling of GET /api/sources?in_progress=1 on SSE error.
 *   2. "History" — all sources (searchable), with exhaustion chips and clip
 *      counts added to each row.
 *
 * SSE lifecycle:
 *   - Opened when the view initialises.
 *   - Diffs the JSON payload by source id — skips re-render if unchanged.
 *   - On 'error': closes, starts 5-second polling fallback.
 *   - MutationObserver on the container closes ES/poll when .active is removed
 *     (user navigates away), and re-opens when .active is added (user returns).
 *   - On re-init (after logout/login): previous handles are cleaned up first.
 *
 * Exported API:
 *   initSources(container, ctx)
 */

// ── Module-level SSE lifecycle handles ────────────────────────────────────────

let _esHandle   = null;   // EventSource or null
let _pollHandle = null;   // setInterval id or null
let _esObserver = null;   // MutationObserver watching the container .active class
let _ctx        = null;   // shared context — set in initSources

function _cleanupLive() {
  if (_esHandle)   { _esHandle.close();         _esHandle   = null; }
  if (_pollHandle) { clearInterval(_pollHandle); _pollHandle = null; }
}

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

function _platformIcon(platform) {
  return PLATFORM_ICON[platform] || `<span style="font-size:10px;text-transform:uppercase;opacity:.5">${_esc(platform || '?')}</span>`;
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

// ── Stage label + percent (contract §7) ──────────────────────────────────────

function _stageLabel(src) {
  const { stage, clips_rendered = 0, clips_identified, clips_approved = 0 } = src;
  switch (stage) {
    case 'queued':       return 'Queued';
    case 'transcribing': return 'Transcribing';
    case 'identifying':  return 'Identifying clips';
    case 'rendering': {
      const n = clips_rendered || 0;
      const N = clips_identified != null ? clips_identified : '?';
      return `Rendering ${n}/${N}`;
    }
    case 'reviewing': {
      const n = clips_approved || 0;
      const N = clips_rendered || 0;
      return `In review ${n}/${N} approved`;
    }
    case 'complete': return 'Complete';
    case 'failed':   return 'Failed';
    default:         return stage || '—';
  }
}

function _stagePercent(src) {
  const { stage, clips_rendered = 0, clips_identified, clips_approved = 0, clips_rejected = 0 } = src;
  switch (stage) {
    case 'queued':       return 5;
    case 'transcribing': return 20;
    case 'identifying':  return 35;
    case 'rendering': {
      if (!clips_identified || clips_identified === 0) return 35;
      return Math.min(90, 35 + Math.round(55 * ((clips_rendered || 0) / clips_identified)));
    }
    case 'reviewing': {
      const decided = (clips_approved || 0) + (clips_rejected || 0);
      const rendered = clips_rendered || 0;
      if (rendered === 0) return 90;
      return Math.min(100, 90 + Math.round(10 * (decided / rendered)));
    }
    case 'complete': return 100;
    default:         return 0;  // failed or unknown — no bar
  }
}

// ── In-progress card builder ──────────────────────────────────────────────────

function _buildProgressCard(src) {
  const isFailed = src.stage === 'failed';
  const pct      = _stagePercent(src);
  const stageText = _stageLabel(src);

  const thumbHtml = src.thumbnail_url
    ? `<img class="source-thumb" src="${_esc(src.thumbnail_url)}" alt="" loading="lazy"
          onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
       <div class="source-thumb-fallback" style="display:none">${_platformIcon(src.platform)}</div>`
    : `<div class="source-thumb-fallback">${_platformIcon(src.platform)}</div>`;

  // Progress bar — light-stream motif; omitted for failed
  const barHtml = !isFailed ? `
    <div class="source-stage-bar-track"
         role="progressbar"
         aria-valuenow="${pct}"
         aria-valuemin="0"
         aria-valuemax="100"
         aria-label="${_esc(stageText)} — ${pct}%">
      <div class="source-stage-bar-fill" style="width:${pct}%">
        <div class="source-stage-bar-streak" aria-hidden="true"></div>
      </div>
    </div>` : '';

  // "Identified N clips" note — shown once clips_identified is known
  const identifiedHtml = (!isFailed && src.clips_identified != null)
    ? `<div class="source-identified-note">Identified ${src.clips_identified} clip${src.clips_identified !== 1 ? 's' : ''} from this video</div>`
    : '';

  // Error block for failed sources — truncate at 200 chars, expandable via <details>
  let errorHtml = '';
  if (isFailed && src.stage_error) {
    const full      = src.stage_error;
    const truncated = full.length > 200;
    errorHtml = `
      <div class="source-stage-error">
        ${!truncated
          ? `<p class="source-error-text">${_esc(full)}</p>`
          : `<details class="source-error-details">
               <summary class="source-error-summary">${_esc(full.slice(0, 200))}…</summary>
               <p class="source-error-full">${_esc(full)}</p>
             </details>`
        }
      </div>`;
  }

  return `
    <article class="source-card source-inprogress-card${isFailed ? ' source-card--failed' : ''}"
             data-source-id="${_esc(src.source_id)}">
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
          <span class="chip ${isFailed ? 'chip-amber' : 'chip-accent'}">${_esc(stageText)}</span>
        </div>
        ${barHtml}
        ${identifiedHtml}
        ${errorHtml}
      </div>
    </article>`;
}

// ── History card builder ──────────────────────────────────────────────────────

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

  // Per-row counts (new)
  const hasCountData = src.clips_rendered != null || src.clips_approved != null;
  const parts = [];
  if (src.clips_identified != null) parts.push(`${src.clips_identified} identified`);
  if (src.clips_rendered   != null) parts.push(`${src.clips_rendered} rendered`);
  if (src.clips_approved   != null) parts.push(`${src.clips_approved} approved`);
  if (src.clips_rejected   != null) parts.push(`${src.clips_rejected} rejected`);
  const countsHtml = hasCountData
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

// ── Filter (history search) ───────────────────────────────────────────────────

function _filter(sources, query) {
  if (!query) return sources;
  const q = query.toLowerCase();
  return sources.filter((s) => {
    const fields = [s.title, s.author_handle, s.campaign, s.platform, s.url, s.source_id];
    return fields.some((f) => (f || '').toLowerCase().includes(q));
  });
}

// ── Render helpers ────────────────────────────────────────────────────────────

function _renderInProgress(sources, container) {
  const section = container.querySelector('.sources-inprogress-list');
  if (!section) return;

  if (!Array.isArray(sources) || sources.length === 0) {
    section.innerHTML = `
      <div class="sources-inprogress-empty text-muted">
        No sources currently processing.
      </div>`;
    return;
  }

  section.innerHTML = sources.map(_buildProgressCard).join('');
}

function _renderHistory(sources, query, container) {
  const list = container.querySelector('.sources-history-list');
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

// ── Live-update fetch (used by both polling and initial load) ─────────────────

async function _fetchAndRenderProgress(container) {
  if (!_ctx) return;
  try {
    const sources = await _ctx.mockFetch(
      () => _ctx.api.getSourcesProgress(),
      () => _ctx.fixtures.sourcesProgress || [],
    );
    _renderInProgress(sources, container);
  } catch {
    // Non-fatal — show whatever we already have
  }
}

// ── SSE + polling lifecycle ───────────────────────────────────────────────────

function _startLiveUpdates(container) {
  _cleanupLive();

  // Immediate first fetch so the panel shows data before the first SSE event
  _fetchAndRenderProgress(container);

  let lastPayloadStr = null;

  try {
    // Auth rides the ce_session cookie; EventSource cannot send custom headers
    const es = new EventSource('/api/sources/stream');
    _esHandle = es;

    es.addEventListener('progress', (evt) => {
      try {
        // Avoid re-rendering if the payload did not change
        if (evt.data === lastPayloadStr) return;
        lastPayloadStr = evt.data;
        const parsed = JSON.parse(evt.data);
        if (Array.isArray(parsed.sources)) {
          _renderInProgress(parsed.sources, container);
        }
      } catch { /* ignore parse errors */ }
    });

    es.addEventListener('error', () => {
      if (_esHandle) { _esHandle.close(); _esHandle = null; }
      // Fall back to polling every 5 s; avoids duplicate interval on repeated errors
      if (!_pollHandle) {
        _pollHandle = setInterval(() => _fetchAndRenderProgress(container), 5000);
      }
    });
  } catch {
    // EventSource constructor failed (unsupported env) — go straight to polling
    _pollHandle = setInterval(() => _fetchAndRenderProgress(container), 5000);
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────

/**
 * @param {HTMLElement} container  The #view-sources element
 * @param {{ api, fixtures, mockFetch, toast, onUnauthorized }} ctx
 */
export function initSources(container, ctx) {
  // Tear down any live handles from a prior session (e.g. after logout → login)
  _cleanupLive();
  if (_esObserver) { _esObserver.disconnect(); _esObserver = null; }
  _ctx = ctx;

  let _historySources = [];
  let _query = '';

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

  // ── SSE / polling for in-progress ──────────────────────────────────────────
  _startLiveUpdates(container);

  // Watch for the view being activated/deactivated so we can pause/resume SSE.
  // app.js toggles .active on the container when switching tabs.
  _esObserver = new MutationObserver(() => {
    if (container.classList.contains('active')) {
      // Returned to this view — restart live feed
      _startLiveUpdates(container);
    } else {
      // Left this view — stop to save connections / battery
      _cleanupLive();
    }
  });
  _esObserver.observe(container, { attributeFilter: ['class'] });

  // ── History fetch ──────────────────────────────────────────────────────────
  ctx.mockFetch(
    () => ctx.api.getSources(),
    () => ctx.fixtures.sources || [],
  ).then((data) => {
    _historySources = Array.isArray(data) ? data : [];
    _renderHistory(_historySources, _query, container);
  }).catch((err) => {
    if (err && err.status === 401) {
      ctx.onUnauthorized();
      return;
    }
    const histList = container.querySelector('.sources-history-list');
    if (histList) {
      histList.innerHTML = `<div class="sources-empty text-muted">Could not load sources. Check your connection.</div>`;
    }
  });

  // ── History search ─────────────────────────────────────────────────────────
  const searchEl = container.querySelector('.sources-search');
  let _debounce  = null;
  searchEl.addEventListener('input', () => {
    clearTimeout(_debounce);
    _debounce = setTimeout(() => {
      _query = searchEl.value.trim();
      _renderHistory(_historySources, _query, container);
    }, 200);
  });
}
