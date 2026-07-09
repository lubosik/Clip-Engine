/**
 * queue.js — Queue view: cinematic glass panels, review overlay.
 *
 * Design:
 *   - Pending items render as glass panels rising from the reflective floor.
 *   - Score as luminous edge meter (left-edge colored segment).
 *   - Mixed aspect ratios: 9:16 clips, 1:1 / 4:5 memes.
 *   - Kind tag (Clip / Meme) + mode badge (DEMO / LIVE) on each panel.
 *   - Destination platform glyphs (inline SVG).
 *   - Source credit etched at panel base.
 *   - Tap → panel steps forward → full-screen review overlay.
 *   - Approve: panel rises and dissolves into light, auto-advances.
 *   - Reject: panel sinks.
 *   - Filter bar: All / Today's batch / [campaigns] || Clips / Memes (kind).
 *   - Empty state: floor + light-stream line.
 *
 * Exported API:
 *   initQueue(container, ctx)
 *   refreshQueue()
 */

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
  x: `<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-4.714-6.231-5.401 6.231H2.747l7.73-8.835L1.254 2.25H8.08l4.259 5.631 5.905-5.631zm-1.161 17.52h1.833L7.084 4.126H5.117z"/>
  </svg>`,
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function _scoreMeta(score) {
  if (score >= 0.85) return { cls: 's-high', label: `${Math.round(score * 100)}` };
  if (score >= 0.70) return { cls: 's-mid',  label: `${Math.round(score * 100)}` };
  return                   { cls: 's-low',   label: `${Math.round(score * 100)}` };
}

function _fmtSlot(isoStr) {
  if (!isoStr) return '';
  const d = new Date(isoStr);
  const now = new Date();
  const tomorrow = new Date(now);
  tomorrow.setDate(tomorrow.getDate() + 1);

  const timeStr = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === now.toDateString()) return `Today at ${timeStr}`;
  if (d.toDateString() === tomorrow.toDateString()) return `Tomorrow at ${timeStr}`;
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' at ' + timeStr;
}

function _isToday(isoStr) {
  if (!isoStr) return false;
  return new Date(isoStr).toDateString() === new Date().toDateString();
}

function _escHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function _platformGlyphs(channels) {
  if (!channels || channels.length === 0) return '';
  // Guess platform from channel name
  const infer = (name) => {
    const n = (name || '').toLowerCase();
    if (n.includes('youtube') || n.includes('yt')) return 'youtube';
    if (n.includes('tiktok') || n.includes('tt'))  return 'tiktok';
    if (n.includes('instagram') || n.includes('ig')) return 'instagram';
    if (n.includes('twitter') || n.includes('_x'))  return 'x';
    return null;
  };

  return channels
    .map((ch) => {
      const platform = typeof ch === 'string' ? infer(ch) : ch;
      const icon = PLATFORM_ICON[platform] || '';
      if (!icon) return '';
      return `<span class="platform-glyph" title="${_escHtml(typeof ch === 'string' ? ch : platform)}">${icon}</span>`;
    })
    .join('');
}

// ── State ─────────────────────────────────────────────────────────────────────

/** @type {Array<Object>} */
let _clips        = [];
let _campaigns    = [];
let _activeCampaign = 'all';
let _todayOnly    = false;
let _kindFilter   = 'all';   // 'all' | 'clip' | 'meme'
let _ctx          = null;
let _reviewIdx    = -1;      // index into _visibleClips() of currently-open review

// ── Public ────────────────────────────────────────────────────────────────────

export function initQueue(container, ctx) {
  _ctx = ctx;
  container.innerHTML = '';

  // Notification prompt banner
  const notifBanner = _buildNotifBanner();
  container.appendChild(notifBanner);

  // Filter bars
  const filterWrap = document.createElement('div');
  filterWrap.id = 'queue-filter-wrap';
  container.appendChild(filterWrap);

  // Cards grid
  const cardsEl = document.createElement('div');
  cardsEl.id = 'queue-cards';
  container.appendChild(cardsEl);

  _renderFilters();
  _load();
}

export function refreshQueue() {
  _load();
}

// ── Internal ──────────────────────────────────────────────────────────────────

async function _load() {
  const cardsEl = document.getElementById('queue-cards');
  if (!cardsEl) return;

  if (_clips.length === 0) {
    cardsEl.innerHTML = _skeletonHTML();
  }

  try {
    const params = { status: 'pending_review', limit: 50 };
    if (_activeCampaign !== 'all') params.campaign = _activeCampaign;
    if (_kindFilter !== 'all') params.kind = _kindFilter;

    const data = await _ctx.mockFetch(
      () => _ctx.api.getClips(params),
      () => {
        let fc = Array.isArray(_ctx.fixtures.clips) ? _ctx.fixtures.clips : [];
        if (_activeCampaign !== 'all') fc = fc.filter((c) => c.campaign === _activeCampaign);
        if (_kindFilter !== 'all') fc = fc.filter((c) => c.kind === _kindFilter);
        return fc;
      }
    );

    _clips = Array.isArray(data) ? data : [];

    const names = [...new Set(_clips.map((c) => c.campaign))].filter(Boolean);
    names.forEach((n) => { if (!_campaigns.includes(n)) _campaigns.push(n); });
    _renderFilters();

    _ctx.onBadge(_clips.length);
    _renderCards();
  } catch (err) {
    if (err.status === 401) { _ctx.onUnauthorized(); return; }
    _ctx.toast('Failed to load clips: ' + err.message, 'error');
    if (_clips.length === 0) {
      cardsEl.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-floor" aria-hidden="true"></div>
          <h3>Could not load clips</h3>
          <p>${_escHtml(err.message)}</p>
        </div>`;
    }
  }
}

// ── Filters ───────────────────────────────────────────────────────────────────

function _renderFilters() {
  const wrap = document.getElementById('queue-filter-wrap');
  if (!wrap) return;
  wrap.innerHTML = '';

  // Row 1 — batch / campaign filters
  const bar1 = document.createElement('div');
  bar1.className = 'filter-bar';
  bar1.setAttribute('role', 'group');
  bar1.setAttribute('aria-label', 'Batch and campaign filter');

  const row1Items = [
    { id: 'all',   label: 'All' },
    { id: 'today', label: "Today's batch" },
    ..._campaigns.map((n) => ({ id: n, label: n })),
  ];

  row1Items.forEach(({ id, label }) => {
    const btn = document.createElement('button');
    btn.className = 'filter-chip' + (_isCampaignActive(id) ? ' active' : '');
    btn.textContent = label;
    btn.setAttribute('aria-pressed', String(_isCampaignActive(id)));
    btn.addEventListener('click', () => _onCampaignFilter(id));
    bar1.appendChild(btn);
  });

  wrap.appendChild(bar1);

  // Row 2 — kind filter
  const bar2 = document.createElement('div');
  bar2.className = 'kind-filter-bar';
  bar2.setAttribute('role', 'group');
  bar2.setAttribute('aria-label', 'Content kind filter');

  const kindItems = [
    { id: 'all',  label: 'All' },
    { id: 'clip', label: 'Clips' },
    { id: 'meme', label: 'Memes' },
  ];

  kindItems.forEach(({ id, label }) => {
    const btn = document.createElement('button');
    const isActive = _kindFilter === id;
    btn.className = 'filter-chip'
      + (isActive ? (id === 'meme' ? ' kind-active-meme' : ' active') : '');
    btn.textContent = label;
    btn.setAttribute('aria-pressed', String(isActive));
    btn.addEventListener('click', () => _onKindFilter(id));
    bar2.appendChild(btn);
  });

  wrap.appendChild(bar2);
}

function _isCampaignActive(id) {
  if (id === 'today') return _todayOnly;
  if (id === 'all') return _activeCampaign === 'all' && !_todayOnly;
  return _activeCampaign === id && !_todayOnly;
}

function _onCampaignFilter(id) {
  if (id === 'today') {
    _todayOnly = !_todayOnly;
    if (_todayOnly) _activeCampaign = 'all';
  } else {
    _todayOnly = false;
    _activeCampaign = id;
  }
  _renderFilters();
  _renderCards();
}

function _onKindFilter(id) {
  _kindFilter = id;
  _renderFilters();
  _load();  // re-fetch with kind param
}

function _visibleClips() {
  return _clips.filter((c) => {
    if (_activeCampaign !== 'all' && c.campaign !== _activeCampaign) return false;
    if (_todayOnly && !_isToday(c.proposed_slot)) return false;
    return true;
  });
}

// ── Cards render ──────────────────────────────────────────────────────────────

async function _renderCards() {
  const cardsEl = document.getElementById('queue-cards');
  if (!cardsEl) return;

  const visible = _visibleClips();

  if (visible.length === 0) {
    try {
      const stats = await _ctx.mockFetch(
        () => _ctx.api.getStats(),
        () => _ctx.fixtures.stats
      );
      const nextRun = stats.next_run_at
        ? `Next run: ${_fmtSlot(stats.next_run_at)}`
        : 'No run scheduled.';
      cardsEl.innerHTML = _emptyStateHTML(nextRun);
    } catch {
      cardsEl.innerHTML = _emptyStateHTML('No clips waiting.');
    }
    return;
  }

  cardsEl.innerHTML = '';
  visible.forEach((clip, idx) => {
    cardsEl.appendChild(_buildCard(clip, idx));
  });
}

function _emptyStateHTML(subtitle) {
  return `
    <div class="empty-state">
      <svg class="empty-state-lightstream" viewBox="0 0 320 80" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <defs>
          <linearGradient id="ls-grad-es" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%"   stop-color="#00e5ff" stop-opacity="0"/>
            <stop offset="45%"  stop-color="#00e5ff" stop-opacity="0.70"/>
            <stop offset="75%"  stop-color="#7c4ef0" stop-opacity="0.50"/>
            <stop offset="100%" stop-color="#7c4ef0" stop-opacity="0"/>
          </linearGradient>
        </defs>
        <path d="M-10 55 Q60 20 130 40 Q200 60 280 15 Q310 5 340 25"
          stroke="url(#ls-grad-es)" stroke-width="1.5" fill="none"
          opacity="0.80"/>
        <path d="M-10 55 Q60 20 130 40 Q200 60 280 15 Q310 5 340 25"
          stroke="url(#ls-grad-es)" stroke-width="4" fill="none"
          opacity="0.25" style="filter:blur(3px)"/>
      </svg>
      <h3>No clips waiting</h3>
      <p>${_escHtml(subtitle)}</p>
      <div class="empty-state-floor" aria-hidden="true"></div>
    </div>`;
}

// ── Card builder ──────────────────────────────────────────────────────────────

function _buildCard(clip, idx) {
  const aspect  = clip.aspect || '9:16';
  const kind    = clip.kind   || 'clip';
  const mode    = clip.mode   || 'production';
  const score   = clip.score  ?? 0;
  const { cls: meterCls } = _scoreMeta(score);

  const el = document.createElement('div');
  el.className = 'clip-card';
  el.dataset.id     = clip.id;
  el.dataset.aspect = aspect;
  el.dataset.kind   = kind;

  // Media HTML
  let mediaContent;
  if (kind === 'meme' && clip.thumb_url) {
    mediaContent = `<img class="clip-thumb" src="${_escHtml(clip.thumb_url)}" alt="Meme preview" loading="lazy">`;
  } else if (clip.video_url || clip.thumb_url) {
    mediaContent = `
      <video
        class="clip-video"
        playsinline
        preload="none"
        ${clip.thumb_url ? `poster="${_escHtml(clip.thumb_url)}"` : ''}
        src="${_escHtml(clip.video_url || '')}"
        aria-label="${_escHtml(clip.hook || 'Clip preview')}"
      ></video>`;
  } else {
    // Mock mode — no URL
    const iconSvg = kind === 'meme'
      ? `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>`
      : `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg>`;
    mediaContent = `
      <div class="clip-video-placeholder" aria-hidden="true">
        ${iconSvg}
      </div>`;
  }

  const platformGlyphs = _platformGlyphs(clip.destination_channels);
  const kindLabel  = kind === 'meme' ? 'Meme' : 'Clip';
  const modeHtml   = mode === 'demo'
    ? `<span class="badge-demo">DEMO</span>`
    : `<span class="badge-live">LIVE</span>`;

  const sourceHandle = clip.source?.handle || clip.source_handle || null;
  const sourcePlatform = clip.source?.platform || '';
  const sourcePlatformIcon = PLATFORM_ICON[sourcePlatform] || '';

  el.innerHTML = `
    <div class="score-meter ${meterCls}" style="height:${Math.round(score * 100)}%" aria-hidden="true"></div>

    <div class="clip-media">
      ${mediaContent}
      <div class="media-overlay-tl">
        <span class="kind-tag kind-${kind}">${kindLabel}</span>
      </div>
      <div class="media-overlay-tr">
        ${modeHtml}
      </div>
      ${sourceHandle
        ? `<div class="media-source-credit">
             ${sourcePlatformIcon ? `<span style="display:inline-flex;vertical-align:middle;margin-right:3px;opacity:0.65">${sourcePlatformIcon}</span>` : ''}
             via @${_escHtml(sourceHandle)}
           </div>`
        : ''}
      ${platformGlyphs
        ? `<div class="platform-glyphs" aria-label="Destination platforms">${platformGlyphs}</div>`
        : ''}
    </div>

    <div class="clip-body">
      <div class="clip-hook">${_escHtml(clip.hook || '')}</div>
      <div class="clip-meta">
        ${clip.score != null ? `<span class="review-score ${meterCls}" aria-label="Score ${Math.round(score*100)}">${Math.round(score * 100)}</span>` : ''}
        ${clip.campaign ? `<span class="chip chip-accent">${_escHtml(clip.campaign)}</span>` : ''}
      </div>
      ${clip.proposed_slot ? `<div class="clip-slot">Proposed: ${_fmtSlot(clip.proposed_slot)}</div>` : ''}
    </div>`;

  // Tap → open review
  el.addEventListener('click', (e) => {
    // Don't open review if clicking a button inside (there are none in the new card, but guard)
    if (e.target.closest('button')) return;
    _openReview(clip, el);
  });
  // Keyboard accessibility
  el.tabIndex = 0;
  el.setAttribute('role', 'button');
  el.setAttribute('aria-label', `Review: ${clip.hook || clip.id}`);
  el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _openReview(clip, el); }
  });

  return el;
}

// ── Review overlay ────────────────────────────────────────────────────────────

function _openReview(clip, cardEl) {
  // Remove any existing review overlay
  document.getElementById('review-overlay-root')?.remove();

  // Step card forward
  cardEl.classList.add('panel-active');

  const kind  = clip.kind  || 'clip';
  const mode  = clip.mode  || 'production';
  const score = clip.score ?? 0;
  const { cls: scoreCls } = _scoreMeta(score);

  const sourceHandle   = clip.source?.handle || null;
  const sourcePlatform = clip.source?.platform || '';
  const platformGlyphs = _platformGlyphs(clip.destination_channels);

  // Build media HTML for review
  let mediaHtml;
  if (kind === 'meme') {
    if (clip.thumb_url) {
      mediaHtml = `<img class="review-image" src="${_escHtml(clip.thumb_url)}" alt="Meme preview" loading="lazy">`;
    } else {
      mediaHtml = `
        <div class="review-media-placeholder">
          <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
            <rect x="3" y="3" width="18" height="18" rx="2"/>
            <circle cx="8.5" cy="8.5" r="1.5"/>
            <polyline points="21 15 16 10 5 21"/>
          </svg>
          <span>No preview (mock mode)</span>
        </div>`;
    }
  } else {
    // Clips: use video_url if available, otherwise API path (server 307 redirect handles R2)
    const videoSrc = clip.video_url || `/api/clips/${encodeURIComponent(clip.id)}/video`;
    if (clip.video_url || !clip.video_url === false) {
      mediaHtml = `
        <video
          class="review-video"
          playsinline
          controls
          preload="metadata"
          ${clip.thumb_url ? `poster="${_escHtml(clip.thumb_url)}"` : ''}
          src="${_escHtml(videoSrc)}"
          aria-label="${_escHtml(clip.hook || 'Clip preview')}"
        ></video>`;
    } else {
      mediaHtml = `
        <div class="review-media-placeholder">
          <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
            <polygon points="23 7 16 12 23 17 23 7"/>
            <rect x="1" y="5" width="15" height="14" rx="2"/>
          </svg>
          <span>No video (mock mode)</span>
        </div>`;
    }
  }

  // Visible clips for prev/next navigation
  const visible = _visibleClips();
  const currentIdx = visible.findIndex((c) => c.id === clip.id);

  const overlay = document.createElement('div');
  overlay.id = 'review-overlay-root';
  overlay.className = 'review-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-label', 'Clip review');

  overlay.innerHTML = `
    <div class="review-top-bar">
      <button class="review-close" id="rv-close" aria-label="Close review">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
             stroke-linecap="round" aria-hidden="true">
          <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </button>
      <div class="review-top-title">${_escHtml(clip.campaign || 'Review')}</div>
      <button class="review-nav-btn" id="rv-prev" aria-label="Previous clip" ${currentIdx <= 0 ? 'disabled' : ''}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
             stroke-linecap="round" aria-hidden="true">
          <polyline points="15 18 9 12 15 6"/>
        </svg>
      </button>
      <button class="review-nav-btn" id="rv-next" aria-label="Next clip" ${currentIdx >= visible.length - 1 ? 'disabled' : ''}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
             stroke-linecap="round" aria-hidden="true">
          <polyline points="9 18 15 12 9 6"/>
        </svg>
      </button>
    </div>

    <div class="review-media-wrap">
      ${mediaHtml}
      <div class="review-floor-reflect" aria-hidden="true"></div>
    </div>

    <div class="review-meta">
      <span class="review-score ${scoreCls}" aria-label="Score ${Math.round(score * 100)}">${Math.round(score * 100)}</span>
      ${sourceHandle
        ? `<span class="review-source-credit">
             ${PLATFORM_ICON[sourcePlatform] ? `<span style="display:inline-flex;vertical-align:middle;margin-right:3px;opacity:0.65">${PLATFORM_ICON[sourcePlatform]}</span>` : ''}
             via @${_escHtml(sourceHandle)}
           </span>`
        : ''}
      ${platformGlyphs ? `<div class="platform-glyphs" aria-label="Destinations">${platformGlyphs}</div>` : ''}
      ${mode === 'demo' ? '<span class="badge-demo">DEMO</span>' : '<span class="badge-live">LIVE</span>'}
    </div>

    <div class="review-hook-text">${_escHtml(clip.hook || '')}</div>

    <div class="review-caption-wrap">
      <div class="review-caption-display" id="rv-caption-display">${_escHtml(clip.caption || '')}</div>
    </div>

    ${clip.proposed_slot ? `<div class="review-slot">Proposed: ${_fmtSlot(clip.proposed_slot)}</div>` : ''}

    <div class="review-controls" id="rv-controls">
      <button class="btn btn-danger" id="rv-reject" style="flex:none;min-width:80px">Reject</button>
      <button class="btn btn-secondary btn-icon" id="rv-edit" aria-label="Edit caption" title="Edit caption">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/>
          <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/>
        </svg>
      </button>
      <button class="btn btn-primary" id="rv-approve">Approve</button>
    </div>`;

  document.body.appendChild(overlay);

  // Animate in
  requestAnimationFrame(() => {
    requestAnimationFrame(() => overlay.classList.add('open'));
  });

  const close = () => {
    overlay.classList.remove('open');
    cardEl.classList.remove('panel-active');
    setTimeout(() => overlay.remove(), 340);
  };

  overlay.querySelector('#rv-close').addEventListener('click', close);

  // Backdrop click to close
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) close();
  });

  // Keyboard close
  const onKey = (e) => {
    if (e.key === 'Escape') { close(); document.removeEventListener('keydown', onKey); }
  };
  document.addEventListener('keydown', onKey);

  // Prev / Next navigation
  const prevBtn = overlay.querySelector('#rv-prev');
  const nextBtn = overlay.querySelector('#rv-next');
  if (prevBtn && currentIdx > 0) {
    prevBtn.addEventListener('click', () => {
      close();
      setTimeout(() => {
        const prevClip = visible[currentIdx - 1];
        const prevCard = document.querySelector(`.clip-card[data-id="${CSS.escape(prevClip.id)}"]`);
        if (prevClip && prevCard) _openReview(prevClip, prevCard);
      }, 340);
    });
  }
  if (nextBtn && currentIdx < visible.length - 1) {
    nextBtn.addEventListener('click', () => {
      close();
      setTimeout(() => {
        const nextClip = visible[currentIdx + 1];
        const nextCard = document.querySelector(`.clip-card[data-id="${CSS.escape(nextClip.id)}"]`);
        if (nextClip && nextCard) _openReview(nextClip, nextCard);
      }, 340);
    });
  }

  // Approve
  overlay.querySelector('#rv-approve').addEventListener('click', () => {
    _doApprove(clip, cardEl, overlay, null);
  });

  // Reject
  overlay.querySelector('#rv-reject').addEventListener('click', () => {
    _showRejectField(overlay, clip, cardEl);
  });

  // Edit caption
  overlay.querySelector('#rv-edit').addEventListener('click', () => {
    _editCaption(overlay, clip, cardEl);
  });
}

// ── Review actions ────────────────────────────────────────────────────────────

async function _doApprove(clip, cardEl, overlay, captionOverride) {
  const approveBtn = overlay.querySelector('#rv-approve');
  if (approveBtn) { approveBtn.disabled = true; approveBtn.innerHTML = '<span class="spinner"></span>'; }

  try {
    await _ctx.mockFetch(
      () => _ctx.api.approveClip(clip.id, captionOverride || undefined),
      () => ({ status: 'approved' })
    );

    // Close review overlay
    overlay.classList.remove('open');
    overlay.addEventListener('transitionend', () => overlay.remove(), { once: true });

    // Animate card: rises and dissolves into light
    cardEl.classList.remove('panel-active');
    cardEl.classList.add('approving');

    // Wait for animation, then remove from state
    setTimeout(() => {
      _clips = _clips.filter((c) => c.id !== clip.id);
      cardEl.remove();
      _ctx.onBadge(_visibleClips().length);

      // Auto-advance to next visible clip
      const remaining = _visibleClips();
      if (remaining.length > 0) {
        const nextCard = document.querySelector(`.clip-card[data-id="${CSS.escape(remaining[0].id)}"]`);
        if (nextCard) setTimeout(() => _openReview(remaining[0], nextCard), 200);
      }
    }, 580);

    _ctx.toast('Approved', 'success');
  } catch (err) {
    if (approveBtn) { approveBtn.disabled = false; approveBtn.textContent = 'Approve'; }
    if (err.status === 401) { overlay.remove(); _ctx.onUnauthorized(); return; }
    _ctx.toast('Approve failed: ' + err.message, 'error');
  }
}

function _showRejectField(overlay, clip, cardEl) {
  // Show a reason textarea inside the review overlay
  const controlsEl = overlay.querySelector('#rv-controls');
  if (!controlsEl) return;

  const existing = overlay.querySelector('#rv-reject-sheet');
  if (existing) { existing.remove(); return; } // toggle

  const sheet = document.createElement('div');
  sheet.id = 'rv-reject-sheet';
  sheet.className = 'review-reject-sheet';
  sheet.innerHTML = `
    <textarea id="rv-reject-reason" class="review-caption-editor" rows="3"
      placeholder="Optional reason (helps the ranker learn)…" style="min-height:72px"></textarea>
    <div style="display:flex;gap:8px;margin-top:8px;">
      <button class="btn btn-ghost btn-sm" id="rv-reject-cancel">Cancel</button>
      <button class="btn btn-danger" id="rv-reject-confirm">Reject clip</button>
    </div>`;

  // Insert before controls
  controlsEl.before(sheet);

  sheet.querySelector('#rv-reject-cancel').addEventListener('click', () => sheet.remove());
  sheet.querySelector('#rv-reject-confirm').addEventListener('click', async () => {
    const reason = sheet.querySelector('#rv-reject-reason').value.trim();
    sheet.remove();
    await _doReject(clip, cardEl, overlay, reason);
  });
}

async function _doReject(clip, cardEl, overlay, reason) {
  const rejectBtn = overlay.querySelector('#rv-reject');
  if (rejectBtn) { rejectBtn.disabled = true; rejectBtn.innerHTML = '<span class="spinner"></span>'; }

  try {
    await _ctx.mockFetch(
      () => _ctx.api.rejectClip(clip.id, reason),
      () => ({ status: 'rejected' })
    );

    overlay.classList.remove('open');
    overlay.addEventListener('transitionend', () => overlay.remove(), { once: true });

    cardEl.classList.remove('panel-active');
    cardEl.classList.add('rejecting');

    setTimeout(() => {
      _clips = _clips.filter((c) => c.id !== clip.id);
      cardEl.remove();
      _ctx.onBadge(_visibleClips().length);
      if (_visibleClips().length === 0) _renderCards();
    }, 460);

    _ctx.toast('Rejected', 'success');
  } catch (err) {
    if (rejectBtn) { rejectBtn.disabled = false; rejectBtn.textContent = 'Reject'; }
    if (err.status === 401) { overlay.remove(); _ctx.onUnauthorized(); return; }
    _ctx.toast('Reject failed: ' + err.message, 'error');
  }
}

function _editCaption(overlay, clip, cardEl) {
  const captionDisplay = overlay.querySelector('#rv-caption-display');
  const captionWrap    = overlay.querySelector('.review-caption-wrap');
  if (!captionWrap) return;

  // Replace display with textarea if not already editing
  if (overlay.querySelector('#rv-caption-editor')) return;

  const textarea = document.createElement('textarea');
  textarea.id = 'rv-caption-editor';
  textarea.className = 'review-caption-editor';
  textarea.value = clip.caption || '';
  textarea.setAttribute('aria-label', 'Edit caption');

  captionDisplay.replaceWith(textarea);
  textarea.focus();

  // Update controls
  const controlsEl = overlay.querySelector('#rv-controls');
  if (controlsEl) {
    controlsEl.innerHTML = `
      <button class="btn btn-secondary" id="rv-save-only">Save</button>
      <button class="btn btn-primary" id="rv-save-approve">Save &amp; Approve</button>`;

    controlsEl.querySelector('#rv-save-only').addEventListener('click', async () => {
      const newCaption = textarea.value;
      try {
        await _ctx.mockFetch(
          () => _ctx.api.patchClip(clip.id, newCaption),
          () => ({ caption: newCaption })
        );
        clip.caption = newCaption;
        // Restore display
        const display = document.createElement('div');
        display.id = 'rv-caption-display';
        display.className = 'review-caption-display';
        display.textContent = newCaption;
        textarea.replaceWith(display);
        // Restore controls
        controlsEl.innerHTML = `
          <button class="btn btn-danger" id="rv-reject" style="flex:none;min-width:80px">Reject</button>
          <button class="btn btn-secondary btn-icon" id="rv-edit" aria-label="Edit caption" title="Edit caption">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
          </button>
          <button class="btn btn-primary" id="rv-approve">Approve</button>`;
        controlsEl.querySelector('#rv-approve').addEventListener('click', () => _doApprove(clip, cardEl, overlay, null));
        controlsEl.querySelector('#rv-reject').addEventListener('click', () => _showRejectField(overlay, clip, cardEl));
        controlsEl.querySelector('#rv-edit').addEventListener('click', () => _editCaption(overlay, clip, cardEl));
        _ctx.toast('Caption saved', 'success');
      } catch (err) {
        if (err.status === 401) { overlay.remove(); _ctx.onUnauthorized(); return; }
        _ctx.toast('Save failed: ' + err.message, 'error');
      }
    });

    controlsEl.querySelector('#rv-save-approve').addEventListener('click', () => {
      const newCaption = textarea.value;
      _doApprove(clip, cardEl, overlay, newCaption);
    });
  }
}

// ── Notification banner ───────────────────────────────────────────────────────

function _buildNotifBanner() {
  const wrap = document.createElement('div');
  if (!('Notification' in window) || Notification.permission !== 'default') {
    wrap.style.display = 'none';
    return wrap;
  }

  wrap.className = 'notif-banner';
  wrap.innerHTML = `
    <p>Enable notifications to be alerted when new clips are ready.</p>
    <button class="btn btn-secondary btn-sm" id="enable-notif-btn">Enable</button>
    <button class="btn btn-ghost btn-sm" id="dismiss-notif-btn" aria-label="Dismiss">✕</button>`;

  wrap.querySelector('#enable-notif-btn').addEventListener('click', async () => {
    const perm = await Notification.requestPermission();
    if (perm === 'granted') _ctx.toast('Notifications enabled', 'success');
    wrap.style.display = 'none';
  });
  wrap.querySelector('#dismiss-notif-btn').addEventListener('click', () => {
    wrap.style.display = 'none';
  });

  return wrap;
}

// ── Skeleton ──────────────────────────────────────────────────────────────────

function _skeletonHTML() {
  const s = () => `
    <div class="clip-card" style="pointer-events:none" aria-hidden="true">
      <div class="clip-media">
        <div class="skeleton" style="width:100%;aspect-ratio:9/16;max-height:65vw;border-radius:var(--r) var(--r) 0 0;"></div>
      </div>
      <div class="clip-body">
        <div class="skeleton" style="height:16px;width:88%;margin-bottom:10px;"></div>
        <div class="skeleton" style="height:12px;width:55%;margin-bottom:8px;"></div>
        <div style="display:flex;gap:6px;">
          <div class="skeleton" style="height:26px;width:70px;border-radius:6px;"></div>
          <div class="skeleton" style="height:26px;width:90px;border-radius:14px;"></div>
        </div>
      </div>
    </div>`;
  return s() + s() + s();
}
