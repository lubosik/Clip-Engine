/**
 * queue.js — Queue view: pending clips, approve / reject / edit caption.
 *
 * Exported API:
 *   initQueue(container, ctx) — renders the view, sets up polling interval.
 *   refreshQueue()            — re-fetches clips (called by stats poller).
 */

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtScore(score) {
  if (score >= 0.85) return ['score-high', (score * 100).toFixed(0) + '%'];
  if (score >= 0.70) return ['score-mid',  (score * 100).toFixed(0) + '%'];
  return                    ['score-low',  (score * 100).toFixed(0) + '%'];
}

function fmtSlot(isoStr) {
  if (!isoStr) return '';
  const d = new Date(isoStr);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const tomorrow = new Date(now);
  tomorrow.setDate(tomorrow.getDate() + 1);
  const isTomorrow = d.toDateString() === tomorrow.toDateString();

  const timeStr = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (sameDay) return `Today at ${timeStr}`;
  if (isTomorrow) return `Tomorrow at ${timeStr}`;
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' at ' + timeStr;
}

function isToday(isoStr) {
  if (!isoStr) return false;
  return new Date(isoStr).toDateString() === new Date().toDateString();
}

function platformLabel(platform) {
  const map = { youtube: 'YouTube', tiktok: 'TikTok', instagram: 'Instagram' };
  return map[platform] || platform;
}

// ── State ─────────────────────────────────────────────────────────────────────

/** @type {Array<Object>} */
let _clips = [];
let _campaigns = [];
let _activeCampaign = 'all';   // 'all' | campaign name
let _todayOnly = false;
let _ctx = null;               // { api, mockFetch, toast, openSheet, closeSheet, onBadge }

// ── Public ────────────────────────────────────────────────────────────────────

/**
 * @param {HTMLElement} container
 * @param {{ api, mockFetch, toast, openSheet, closeSheet, onBadge }} ctx
 */
export function initQueue(container, ctx) {
  _ctx = ctx;
  container.innerHTML = '';   // reset

  // Notification prompt banner (rendered once; hidden after grant/deny)
  const notifBanner = _buildNotifBanner();
  container.appendChild(notifBanner);

  // Filter bar
  const filterBar = document.createElement('div');
  filterBar.className = 'filter-bar';
  filterBar.id = 'queue-filter-bar';
  container.appendChild(filterBar);

  // Cards container
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

  // Show skeleton while loading on first render
  if (_clips.length === 0) {
    cardsEl.innerHTML = _skeletonHTML();
  }

  try {
    const params = { status: 'pending_review', limit: 50 };
    if (_activeCampaign !== 'all') params.campaign = _activeCampaign;

    const data = await _ctx.mockFetch(
      () => _ctx.api.getClips(params),
      () => {
        const { clips: fc } = _ctx.fixtures;
        return _activeCampaign === 'all'
          ? fc
          : fc.filter((c) => c.campaign === _activeCampaign);
      }
    );

    _clips = Array.isArray(data) ? data : [];

    // Collect campaign names for filter bar
    const names = [...new Set(_clips.map((c) => c.campaign))].filter(Boolean);
    if (names.length > 0) {
      const prev = new Set(_campaigns);
      names.forEach((n) => {
        if (!prev.has(n)) _campaigns.push(n);
      });
      _renderFilters();
    }

    _ctx.onBadge(_clips.length);
    _renderCards();
  } catch (err) {
    if (err.status === 401) {
      _ctx.onUnauthorized();
      return;
    }
    _ctx.toast('Failed to load clips: ' + err.message, 'error');
    if (_clips.length === 0) {
      cardsEl.innerHTML = `<div class="empty-state"><div class="empty-state-icon">⚠️</div><h3>Could not load clips</h3><p>${err.message}</p></div>`;
    }
  }
}

function _renderFilters() {
  const bar = document.getElementById('queue-filter-bar');
  if (!bar) return;
  bar.innerHTML = '';

  const chips = [
    { id: 'all',   label: 'All' },
    { id: 'today', label: "Today's batch" },
    ..._campaigns.map((n) => ({ id: n, label: n })),
  ];

  chips.forEach(({ id, label }) => {
    const btn = document.createElement('button');
    btn.className = 'filter-chip' + (_isActive(id) ? ' active' : '');
    btn.textContent = label;
    btn.addEventListener('click', () => _onFilterClick(id));
    bar.appendChild(btn);
  });
}

function _isActive(id) {
  if (id === 'today') return _todayOnly;
  if (id === 'all') return _activeCampaign === 'all' && !_todayOnly;
  return _activeCampaign === id && !_todayOnly;
}

function _onFilterClick(id) {
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

function _visibleClips() {
  return _clips.filter((c) => {
    if (_activeCampaign !== 'all' && c.campaign !== _activeCampaign) return false;
    if (_todayOnly && !isToday(c.proposed_slot)) return false;
    return true;
  });
}

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
        ? `Next run: ${fmtSlot(stats.next_run_at)}`
        : 'No run scheduled.';
      cardsEl.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">✂️</div>
          <h3>No clips waiting</h3>
          <p>${nextRun}</p>
        </div>`;
    } catch (_) {
      cardsEl.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">✂️</div>
          <h3>No clips waiting</h3>
        </div>`;
    }
    return;
  }

  cardsEl.innerHTML = '';
  visible.forEach((clip) => {
    cardsEl.appendChild(_buildCard(clip));
  });
}

// ── Card builder ──────────────────────────────────────────────────────────────

function _buildCard(clip) {
  const el = document.createElement('div');
  el.className = 'clip-card';
  el.dataset.id = clip.id;

  const [scoreClass, scoreLabel] = fmtScore(clip.score);

  // Video or placeholder
  let mediaHtml;
  if (clip.video_url) {
    mediaHtml = `
      <video
        class="clip-video"
        playsinline
        controls
        preload="metadata"
        ${clip.thumb_url ? `poster="${clip.thumb_url}"` : ''}
        src="${clip.video_url}"
      ></video>`;
  } else {
    mediaHtml = `
      <div class="clip-video-placeholder">
        <span>No video (mock mode)</span>
      </div>`;
  }

  const destChips = (clip.destination_channels || [])
    .map((ch) => `<span class="chip">${ch}</span>`)
    .join('');

  const campaignChip = clip.campaign
    ? `<span class="chip chip-accent">${clip.campaign}</span>`
    : '';

  el.innerHTML = `
    ${mediaHtml}
    <div class="clip-body">
      <div class="clip-hook">${_escHtml(clip.hook || '')}</div>
      <div class="clip-meta">
        <span class="score-badge ${scoreClass}">${scoreLabel}</span>
        <span class="clip-handle">via @${_escHtml(clip.source?.handle || '?')} · ${platformLabel(clip.source?.platform)}</span>
      </div>
      <div class="chips-row">
        ${campaignChip}
        ${destChips}
      </div>
      ${clip.proposed_slot ? `<div class="clip-slot">Proposed: ${fmtSlot(clip.proposed_slot)}</div>` : ''}
      <div class="caption-preview">${_escHtml(clip.caption || '')}</div>
      <div class="clip-actions">
        <button class="btn btn-primary js-approve" aria-label="Approve">Approve</button>
        <button class="btn btn-secondary js-edit" aria-label="Edit caption">Edit</button>
        <button class="btn btn-danger js-reject" aria-label="Reject">Reject</button>
      </div>
    </div>`;

  el.querySelector('.js-approve').addEventListener('click', () => _onApprove(clip, el));
  el.querySelector('.js-reject').addEventListener('click', () => _onReject(clip, el));
  el.querySelector('.js-edit').addEventListener('click', () => _onEdit(clip, el));

  return el;
}

// ── Actions ───────────────────────────────────────────────────────────────────

async function _onApprove(clip, el) {
  // Optimistic update
  el.classList.add('optimistic-approve');
  el.querySelector('.js-approve').textContent = 'Approving…';

  try {
    await _ctx.mockFetch(
      () => _ctx.api.approveClip(clip.id),
      () => ({ status: 'approved' })
    );
    // Remove from local state and DOM
    _clips = _clips.filter((c) => c.id !== clip.id);
    el.remove();
    _ctx.toast('Approved', 'success');
    _ctx.onBadge(_visibleClips().length);
  } catch (err) {
    // Rollback
    el.classList.remove('optimistic-approve');
    el.querySelector('.js-approve').textContent = 'Approve';
    if (err.status === 401) { _ctx.onUnauthorized(); return; }
    _ctx.toast('Approve failed: ' + err.message, 'error');
  }
}

function _onReject(clip, el) {
  // Show bottom sheet for optional reason
  _ctx.openSheet({
    title: 'Reject clip',
    body: `
      <p class="text-muted text-small" style="margin-bottom:12px">
        Optional: give a reason so the ranker can learn.
      </p>
      <div class="form-group mb-0">
        <label class="form-label">Reason</label>
        <textarea id="reject-reason" class="form-control" rows="3"
          placeholder="e.g. too similar to clip posted last week"></textarea>
      </div>`,
    primaryLabel: 'Reject',
    primaryClass: 'btn-danger',
    onPrimary: async () => {
      const reason = document.getElementById('reject-reason')?.value?.trim() ?? '';
      _ctx.closeSheet();
      el.classList.add('optimistic-reject');
      el.querySelector('.js-reject').textContent = 'Rejecting…';

      try {
        await _ctx.mockFetch(
          () => _ctx.api.rejectClip(clip.id, reason),
          () => ({ status: 'rejected' })
        );
        _clips = _clips.filter((c) => c.id !== clip.id);
        el.remove();
        _ctx.toast('Rejected', 'success');
        _ctx.onBadge(_visibleClips().length);
      } catch (err) {
        el.classList.remove('optimistic-reject');
        el.querySelector('.js-reject').textContent = 'Reject';
        if (err.status === 401) { _ctx.onUnauthorized(); return; }
        _ctx.toast('Reject failed: ' + err.message, 'error');
      }
    },
  });
}

function _onEdit(clip, el) {
  let editedCaption = clip.caption || '';

  _ctx.openSheet({
    title: 'Edit caption',
    body: `
      <div class="form-group">
        <label class="form-label">Caption</label>
        <textarea id="edit-caption" class="form-control" rows="6"
          style="min-height:140px">${_escHtml(editedCaption)}</textarea>
        <p class="form-hint">Saved to this clip. Tap "Save &amp; Approve" to approve immediately.</p>
      </div>`,
    primaryLabel: 'Save & Approve',
    primaryClass: 'btn-primary',
    secondaryLabel: 'Save only',
    onPrimary: async () => {
      const newCaption = document.getElementById('edit-caption')?.value ?? editedCaption;
      _ctx.closeSheet();
      el.classList.add('optimistic-approve');
      el.querySelector('.js-approve').textContent = 'Approving…';

      try {
        await _ctx.mockFetch(
          () => _ctx.api.approveClip(clip.id, newCaption),
          () => ({ status: 'approved' })
        );
        _clips = _clips.filter((c) => c.id !== clip.id);
        el.remove();
        _ctx.toast('Caption saved. Approved', 'success');
        _ctx.onBadge(_visibleClips().length);
      } catch (err) {
        el.classList.remove('optimistic-approve');
        el.querySelector('.js-approve').textContent = 'Approve';
        if (err.status === 401) { _ctx.onUnauthorized(); return; }
        _ctx.toast('Approve failed: ' + err.message, 'error');
      }
    },
    onSecondary: async () => {
      const newCaption = document.getElementById('edit-caption')?.value ?? editedCaption;
      _ctx.closeSheet();
      try {
        await _ctx.mockFetch(
          () => _ctx.api.patchClip(clip.id, newCaption),
          () => ({ caption: newCaption })
        );
        // Update local state
        const idx = _clips.findIndex((c) => c.id === clip.id);
        if (idx !== -1) {
          _clips[idx] = { ..._clips[idx], caption: newCaption };
          // Refresh preview in card
          const preview = el.querySelector('.caption-preview');
          if (preview) preview.textContent = newCaption;
        }
        _ctx.toast('Caption saved', 'success');
      } catch (err) {
        if (err.status === 401) { _ctx.onUnauthorized(); return; }
        _ctx.toast('Save failed: ' + err.message, 'error');
      }
    },
  });
}

// ── Notification banner ───────────────────────────────────────────────────────

function _buildNotifBanner() {
  const wrap = document.createElement('div');

  // Don't show if already granted or denied
  if (!('Notification' in window) || Notification.permission !== 'default') {
    wrap.style.display = 'none';
    return wrap;
  }

  wrap.className = 'notif-banner';
  wrap.innerHTML = `
    <p>Enable notifications to be alerted when new clips are ready.</p>
    <button class="btn btn-secondary btn-sm" id="enable-notif-btn">Enable</button>
    <button class="btn btn-ghost btn-sm" id="dismiss-notif-btn">✕</button>`;

  wrap.querySelector('#enable-notif-btn').addEventListener('click', async () => {
    const perm = await Notification.requestPermission();
    if (perm === 'granted') {
      _ctx.toast('Notifications enabled', 'success');
    }
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
    <div class="clip-card">
      <div class="skeleton" style="width:100%;aspect-ratio:9/16;max-height:65vw;"></div>
      <div class="clip-body">
        <div class="skeleton" style="height:18px;width:90%;margin-bottom:10px;"></div>
        <div class="skeleton" style="height:14px;width:60%;margin-bottom:8px;"></div>
        <div style="display:flex;gap:6px;margin-bottom:12px;">
          <div class="skeleton" style="height:26px;width:80px;border-radius:14px;"></div>
          <div class="skeleton" style="height:26px;width:100px;border-radius:14px;"></div>
        </div>
        <div style="display:flex;gap:8px;">
          <div class="skeleton" style="height:44px;flex:1;"></div>
          <div class="skeleton" style="height:44px;width:60px;"></div>
          <div class="skeleton" style="height:44px;width:70px;"></div>
        </div>
      </div>
    </div>`;
  return s() + s();
}

// ── Utility ───────────────────────────────────────────────────────────────────

function _escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
