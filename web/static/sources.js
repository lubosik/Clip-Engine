/**
 * sources.js — Sources view: every video the engine has mined.
 *
 * Shows a cinematic glass card list of all source videos that have been
 * processed (status != pending) or produced clips, so the operator can
 * audit what's been mined and confirm nothing is re-clipped.
 *
 * Exported API:
 *   initSources(container, ctx)
 */

// ── Platform SVG glyphs (inline, same set as queue.js) ───────────────────────

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
    case 'ready':      return { label: 'Ready',     cls: 'chip-accent' };
    case 'overridden': return { label: 'Overridden', cls: 'chip-amber' };
    case 'didnt_pass': return { label: 'Failed',    cls: 'chip-amber'  };
    default:           return { label: 'Pending',   cls: ''            };
  }
}

// ── Build a single source card HTML string ───────────────────────────────────

function _buildCard(src) {
  const { label: statusLabel, cls: statusCls } = _statusLabel(src.status);
  const thumbHtml = src.thumbnail_url
    ? `<img
         class="source-thumb"
         src="${_esc(src.thumbnail_url)}"
         alt=""
         loading="lazy"
         onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"
       >
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

  return `
    <article class="source-card" data-source-id="${_esc(src.source_id)}">
      <div class="source-card-media">
        ${thumbHtml}
      </div>
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
        </div>
        <div class="source-card-meta text-muted">
          ${src.processed_at ? `Processed ${_fmtDate(src.processed_at)}` : 'Not yet processed'}
          ${src.used_ranges_count > 0 ? ` · ${src.used_ranges_count} range${src.used_ranges_count !== 1 ? 's' : ''} used` : ''}
        </div>
        ${clipsSection}
      </div>
    </article>`;
}

// ── Filter logic ─────────────────────────────────────────────────────────────

function _filter(sources, query) {
  if (!query) return sources;
  const q = query.toLowerCase();
  return sources.filter((s) => {
    const fields = [s.title, s.author_handle, s.campaign, s.platform, s.url, s.source_id];
    return fields.some((f) => (f || '').toLowerCase().includes(q));
  });
}

// ── Render ────────────────────────────────────────────────────────────────────

function _render(container, sources, query) {
  const filtered = _filter(sources, query);
  const list = container.querySelector('.sources-list');
  if (!list) return;

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

  list.innerHTML = filtered.map(_buildCard).join('');
}

// ── Init ──────────────────────────────────────────────────────────────────────

/**
 * @param {HTMLElement} container  The #view-sources element
 * @param {{ api, fixtures, mockFetch, toast, onUnauthorized }} ctx
 */
export function initSources(container, ctx) {
  container.innerHTML = `
    <div class="sources-header">
      <input
        type="search"
        class="sources-search form-control"
        placeholder="Search by title, creator, campaign…"
        aria-label="Search sources"
      >
    </div>
    <div class="sources-list" aria-live="polite" aria-label="Source videos"></div>`;

  const searchEl = container.querySelector('.sources-search');
  const listEl   = container.querySelector('.sources-list');
  let _sources = [];
  let _query   = '';

  // Loading state
  listEl.innerHTML = `<div class="sources-loading text-muted">Loading sources…</div>`;

  // Fetch
  ctx.mockFetch(
    () => ctx.api.getSources(),
    () => ctx.fixtures.sources || [],
  ).then((data) => {
    _sources = Array.isArray(data) ? data : [];
    _render(container, _sources, _query);
  }).catch((err) => {
    if (err && err.status === 401) {
      ctx.onUnauthorized();
      return;
    }
    listEl.innerHTML = `<div class="sources-empty text-muted">Could not load sources. Check your connection.</div>`;
  });

  // Client-side search
  let _debounce = null;
  searchEl.addEventListener('input', () => {
    clearTimeout(_debounce);
    _debounce = setTimeout(() => {
      _query = searchEl.value.trim();
      _render(container, _sources, _query);
    }, 200);
  });
}
