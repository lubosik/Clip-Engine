/**
 * app.js — Clip Engine PWA entry point (revamp v2).
 *
 * Responsibilities:
 *   - Hero video: fetch GET /api/hero (unauthenticated), set video src
 *     respecting orientation + prefers-reduced-motion + save-data.
 *     CSS cinematic gradient is the always-present fallback.
 *   - Light-stream auth animation: kick off SVG path draw after DOMContentLoaded.
 *   - Auth: password → bearer token, re-prompt on 401, mock bypass.
 *   - Tab routing: Queue / Campaigns / Analytics.
 *   - Toast and bottom-sheet primitives (shared across views).
 *   - Stats polling every 60 s: update tab badge + fire browser notification.
 *   - Settings sheet: Mock mode, Notifications, Sign out + compact spend line.
 *   - Service worker registration.
 */

import { api, setToken } from './api.js';
import * as fixtures from './fixtures.js';
import { initQueue, refreshQueue } from './queue.js';
import { initCampaigns } from './campaigns.js';
import { initAnalytics } from './analytics.js';

// ── Constants ─────────────────────────────────────────────────────────────────

const TOKEN_KEY = 'clipEngineToken';
const POLL_MS   = 60_000;
const VIEWS     = ['queue', 'campaigns', 'analytics'];

// ── State ─────────────────────────────────────────────────────────────────────

let _activeTab        = 'queue';
let _pollTimer        = null;
let _lastPendingCount = 0;
let _viewInitialized  = { queue: false, campaigns: false, analytics: false };

// ── Boot ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  _registerSW();
  _animateLightStream();
  _initHeroMedia();   // fire-and-forget, non-blocking

  const savedToken = localStorage.getItem(TOKEN_KEY) || '';
  if (savedToken) {
    setToken(savedToken);
    // Refresh the media session cookie BEFORE the queue renders <video>/<img>
    // tags (they authenticate via cookie, not the Bearer header). Boot anyway
    // if it fails — API fetches still work with the header.
    api.createSession().catch(() => {}).finally(_bootApp);
  } else {
    _showAuth();
  }
});

// ── Service Worker ────────────────────────────────────────────────────────────

function _registerSW() {
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register('/sw.js').catch(() => {
    // Non-fatal — app still works without SW.
  });
  // When a new SW takes control after a deploy, reload once so HTML/CSS/JS
  // all come from the same cache version (prevents mixed-version layouts).
  // On a first visit there is no prior controller — skip the reload then.
  const hadController = !!navigator.serviceWorker.controller;
  let reloaded = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (!hadController || reloaded) return;
    reloaded = true;
    window.location.reload();
  });
}

// ── Light-stream auth SVG animation ──────────────────────────────────────────

function _animateLightStream() {
  // Animate stroke-dashoffset from 480 → 0 on all ls-draw-path elements.
  // CSS @keyframes would need to know the dasharray length; JS is cleaner here.
  const paths = document.querySelectorAll('.ls-draw-path');
  if (!paths.length) return;

  const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  if (reduced) {
    // Just reveal instantly
    paths.forEach((p) => { p.style.strokeDashoffset = '0'; });
    return;
  }

  // Stagger the glow (first path) and sharp line (second path)
  paths.forEach((path, i) => {
    const delay = 600 + i * 120;
    const duration = 1800;
    const startTime = performance.now() + delay;

    const totalLength = parseFloat(path.getAttribute('stroke-dasharray') || '480');
    path.style.strokeDashoffset = String(totalLength);

    const tick = (now) => {
      if (now < startTime) { requestAnimationFrame(tick); return; }
      const elapsed = now - startTime;
      const progress = Math.min(elapsed / duration, 1);
      // Ease-out cubic
      const eased = 1 - Math.pow(1 - progress, 3);
      path.style.strokeDashoffset = String(totalLength * (1 - eased));
      if (progress < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
}

// ── Hero media ────────────────────────────────────────────────────────────────

async function _initHeroMedia() {
  // Skip video if user prefers reduced motion or save-data
  const reduced  = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  const saveData = navigator.connection?.saveData === true;
  if (reduced || saveData) return;

  try {
    const hero = await api.getHero().catch(() => null);
    if (!hero) return;

    // Mock mode returns all nulls — CSS backdrop is the intentional fallback.
    if (!hero.video && !hero.video_vertical) return;

    const isPortrait = window.innerHeight > window.innerWidth;
    const videoSrc   = (isPortrait && hero.video_vertical) ? hero.video_vertical : (hero.video || hero.video_vertical);
    const posterSrc  = (isPortrait && hero.poster_mobile) ? hero.poster_mobile : (hero.poster || hero.poster_mobile);

    if (!videoSrc) return;

    const vA = document.getElementById('hero-video');
    const vB = document.getElementById('hero-video-b');
    if (!vA) return;

    // Load the same source into both layers (poster on both for the fade-in).
    // preload=auto so both are fully buffered — the crossfade must never wait
    // on the network, and presigned URLs expire after ~1h.
    [vA, vB].forEach((v) => {
      if (!v) return;
      if (posterSrc) v.poster = posterSrc;
      v.preload = 'auto';
      v.src = videoSrc;
      v.load();
    });

    vA.addEventListener('canplay', () => {
      vA.classList.add('active');
      const p = vA.play();
      if (p && p.catch) p.catch(() => {});
      if (vB) {
        _startSeamlessLoop(vA, vB);
      } else {
        vA.loop = true;  // graceful fallback: one layer → plain native loop
      }
    }, { once: true });
  } catch {
    // Non-fatal — CSS backdrop is always there.
  }
}

// Crossfade two identical video layers end→start so the loop has no visible
// seam. Primary path: an rAF ticker watches the active layer and, ~0.8s before
// its end, starts the idle layer from 0 and swaps the `.active` class (CSS
// transitions opacity → a true crossfade). Safety nets, because a missed
// window used to freeze the hero on the last frame:
//   - `ended` on the active layer forces an immediate swap (worst case the
//     fade starts from the frozen last frame — still smooth, never a stop);
//   - a media `error` on either layer degrades the surviving layer to a
//     native loop rather than dying.
function _startSeamlessLoop(vA, vB) {
  const CROSSFADE = 0.8;  // seconds — matches the .hero-bg-video opacity transition
  let active = vA;
  let idle = vB;
  let swapping = false;
  let dead = false;

  const swap = () => {
    if (swapping || dead) return;
    swapping = true;

    try { idle.currentTime = 0; } catch (_) { /* seek may throw pre-metadata */ }
    const p = idle.play();
    if (p && p.catch) p.catch(() => {});
    idle.classList.add('active');
    active.classList.remove('active');

    // Park the outgoing layer once its tail is past the fade window.
    const outgoing = active;
    setTimeout(() => {
      outgoing.pause();
      try { outgoing.currentTime = 0; } catch (_) { /* ignore */ }
    }, (CROSSFADE + 0.2) * 1000);

    const tmp = active; active = idle; idle = tmp;
    // Re-arm once the fresh active layer is clearly away from t=0.
    setTimeout(() => { swapping = false; }, 500);
  };

  // Safety net 1: never let an ended layer freeze the hero.
  vA.addEventListener('ended', () => { if (active === vA) swap(); });
  vB.addEventListener('ended', () => { if (active === vB) swap(); });

  // Safety net 2: if a layer errors (e.g. expired presigned URL on an
  // unbuffered layer), fall back to natively looping the surviving one.
  const degrade = (survivor) => () => {
    if (dead) return;
    dead = true;
    survivor.loop = true;
    survivor.classList.add('active');
    const p = survivor.play();
    if (p && p.catch) p.catch(() => {});
  };
  vA.addEventListener('error', degrade(vB));
  vB.addEventListener('error', degrade(vA));

  // Primary: rAF ticker (precise near the end; timeupdate only fires ~4Hz).
  const tick = () => {
    if (dead) return;
    const d = active.duration;
    if (d && isFinite(d) && !swapping) {
      const lead = Math.min(CROSSFADE, d * 0.3);
      if (active.currentTime >= d - lead) swap();
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

// ── Auth ──────────────────────────────────────────────────────────────────────

function _showAuth(message) {
  const screen = document.getElementById('auth-screen');
  const app    = document.getElementById('app');
  screen.classList.remove('hidden');
  app.style.display = 'none';

  const errEl = document.getElementById('auth-error');
  const inp   = document.getElementById('auth-password');
  const btn   = document.getElementById('auth-submit');

  if (message) {
    errEl.textContent = message;
    errEl.style.display = 'block';
  } else {
    errEl.style.display = 'none';
  }

  inp.value = '';
  setTimeout(() => inp.focus(), 80);

  // Re-animate light-stream on each auth show (e.g. after logout)
  _animateLightStream();

  const attempt = async () => {
    const pw = inp.value.trim();
    if (!pw) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';

    setToken(pw);

    try {
      await api.getStats();
      localStorage.setItem(TOKEN_KEY, pw);
      // Set the media session cookie so <video>/<img> tags authenticate.
      await api.createSession().catch(() => {});
      _bootApp();
    } catch (err) {
      setToken('');
      btn.disabled = false;
      btn.textContent = 'Unlock';
      if (err.status === 401) {
        errEl.textContent = 'Incorrect password. Try again.';
        errEl.style.display = 'block';
        inp.value = '';
        inp.focus();
      } else {
        // Server unreachable — allow through if mock mode is enabled
        if (localStorage.getItem('mock') === '1') {
          localStorage.setItem(TOKEN_KEY, pw);
          _bootApp();
        } else {
          errEl.textContent = 'Could not reach server. Enable Mock mode in Settings to demo offline.';
          errEl.style.display = 'block';
        }
      }
    }
  };

  btn.onclick = attempt;
  inp.onkeydown = (e) => { if (e.key === 'Enter') attempt(); };
}

function _bootApp() {
  document.getElementById('auth-screen').classList.add('hidden');
  const app = document.getElementById('app');
  app.style.display = '';

  _initTabBar();
  _activateTab('queue');
  _startPolling();
}

// ── Tab bar ───────────────────────────────────────────────────────────────────

function _initTabBar() {
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      if (tab) _activateTab(tab);
    });
  });
}

function _activateTab(tab) {
  if (!VIEWS.includes(tab)) return;

  // Hide campaigns FAB when not on campaigns tab
  const fab = document.getElementById('campaigns-fab');
  if (fab) fab.style.display = tab === 'campaigns' ? '' : 'none';

  // Update tab buttons
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    const isActive = btn.dataset.tab === tab;
    btn.classList.toggle('active', isActive);
    if (isActive) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
  });

  // Show / hide views
  VIEWS.forEach((v) => {
    const el = document.getElementById(`view-${v}`);
    if (el) el.classList.toggle('active', v === tab);
  });

  // Update header title
  const titles = { queue: 'Clip Engine', campaigns: 'Campaigns', analytics: 'Analytics' };
  const titleEl = document.getElementById('header-title');
  if (titleEl) titleEl.textContent = titles[tab] || 'Clip Engine';

  _activeTab = tab;

  // Lazy-init views
  if (!_viewInitialized[tab]) {
    _viewInitialized[tab] = true;
    _initView(tab);
  }
}

function _initView(tab) {
  const container = document.getElementById(`view-${tab}`);
  if (!container) return;

  const ctx = _makeCtx();

  switch (tab) {
    case 'queue':     initQueue(container, ctx);     break;
    case 'campaigns': initCampaigns(container, ctx); break;
    case 'analytics': initAnalytics(container, ctx); break;
  }
}

// ── Context object passed to each view ───────────────────────────────────────

function _makeCtx() {
  return {
    api,
    fixtures,
    mockFetch,
    toast,
    openSheet,
    closeSheet,
    onBadge: _updateQueueBadge,
    onUnauthorized: _onUnauthorized,
    goToAnalytics: () => _activateTab('analytics'),
  };
}

// ── Mock fetch wrapper ────────────────────────────────────────────────────────

/**
 * Try the real API first. If the fetch throws a network error (no .status)
 * AND localStorage.mock === "1", call mockFn() instead.
 * 401 errors always propagate.
 *
 * @template T
 * @param {() => Promise<T>} fetchFn
 * @param {() => T} mockFn
 * @returns {Promise<T>}
 */
async function mockFetch(fetchFn, mockFn) {
  try {
    return await fetchFn();
  } catch (err) {
    if (err.status) throw err;  // HTTP error — propagate (including 401)
    if (localStorage.getItem('mock') === '1') return mockFn();
    throw err;
  }
}

// ── Stats polling ─────────────────────────────────────────────────────────────

function _startPolling() {
  _poll();
  _pollTimer = setInterval(_poll, POLL_MS);
}

async function _poll() {
  try {
    const stats = await mockFetch(
      () => api.getStats(),
      () => fixtures.stats
    );

    const pending = stats.pending ?? 0;
    _updateQueueBadge(pending);

    if (pending > _lastPendingCount && _lastPendingCount >= 0) {
      _maybeNotify(pending - _lastPendingCount);
    }
    _lastPendingCount = pending;

    if (_activeTab === 'queue') {
      try { refreshQueue(); } catch { /* view may not be mounted yet */ }
    }
  } catch {
    // Polling failures are silent — main error handling is in the views.
  }
}

function _updateQueueBadge(count) {
  const badge = document.getElementById('queue-tab-badge');
  const headerBadge = document.getElementById('header-badge');
  if (!badge) return;

  if (count > 0) {
    badge.textContent = count > 99 ? '99+' : String(count);
    badge.classList.add('visible');
    if (headerBadge) { headerBadge.textContent = count; headerBadge.style.display = ''; }
  } else {
    badge.classList.remove('visible');
    if (headerBadge) headerBadge.style.display = 'none';
  }
}

// ── Browser notifications ─────────────────────────────────────────────────────

function _maybeNotify(delta) {
  if (!('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;

  new Notification('Clip Engine', {
    body: delta === 1
      ? '1 new clip ready for review'
      : `${delta} new clips ready for review`,
    icon: '/icons/icon-192.png',
    badge: '/icons/icon-192.png',
    tag: 'clip-engine-pending',
    renotify: true,
  });
}

// ── Toast ─────────────────────────────────────────────────────────────────────

/**
 * @param {string} message
 * @param {'info'|'success'|'error'|'warning'} [type]
 * @param {number} [duration] ms
 */
function toast(message, type = 'info', duration = 3500) {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const el = document.createElement('div');
  el.className = `toast ${type !== 'info' ? type : ''}`.trim();
  el.textContent = message;

  container.appendChild(el);

  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transition = 'opacity 0.3s';
    setTimeout(() => el.remove(), 320);
  }, duration);
}

// ── Bottom sheet ──────────────────────────────────────────────────────────────

let _sheetBackdrop = null;

/**
 * @param {{
 *   title: string,
 *   body: string,
 *   primaryLabel: string,
 *   primaryClass?: string,
 *   secondaryLabel?: string,
 *   onPrimary: () => void,
 *   onSecondary?: () => void
 * }} opts
 */
function openSheet(opts) {
  closeSheet();

  const backdrop = document.createElement('div');
  backdrop.className = 'sheet-backdrop';
  _sheetBackdrop = backdrop;

  const secondaryBtn = opts.secondaryLabel
    ? `<button class="btn btn-secondary" id="sheet-secondary">${_esc(opts.secondaryLabel)}</button>`
    : '';

  backdrop.innerHTML = `
    <div class="sheet">
      <div class="sheet-handle"></div>
      <div class="sheet-header">
        <div class="sheet-title">${_esc(opts.title)}</div>
      </div>
      <div class="sheet-body">${opts.body}</div>
      <div class="sheet-footer">
        ${secondaryBtn}
        <button class="btn ${opts.primaryClass || 'btn-primary'}" id="sheet-primary">
          ${_esc(opts.primaryLabel)}
        </button>
      </div>
    </div>`;

  document.body.appendChild(backdrop);

  requestAnimationFrame(() => {
    requestAnimationFrame(() => backdrop.classList.add('open'));
  });

  backdrop.addEventListener('click', (e) => {
    if (e.target === backdrop) closeSheet();
  });

  backdrop.querySelector('#sheet-primary').addEventListener('click', opts.onPrimary);

  if (opts.onSecondary) {
    const secBtn = backdrop.querySelector('#sheet-secondary');
    if (secBtn) secBtn.addEventListener('click', opts.onSecondary);
  }
}

function closeSheet() {
  if (!_sheetBackdrop) return;
  const b = _sheetBackdrop;
  _sheetBackdrop = null;
  b.classList.remove('open');
  setTimeout(() => b.remove(), 300);
}

// ── Settings sheet ────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  const settingsBtn = document.getElementById('settings-btn');
  if (settingsBtn) settingsBtn.addEventListener('click', _openSettings);
});

async function _openSettings() {
  const mockEnabled = localStorage.getItem('mock') === '1';
  const notifStatus = 'Notification' in window ? Notification.permission : 'unsupported';

  // Fetch spend for compact line (non-blocking fallback)
  let spendLine = '';
  try {
    const spendData = await mockFetch(
      () => api.getSpend(),
      () => fixtures.spend
    );
    const pct = spendData.month_to_date_usd / spendData.budget_usd;
    const isWarning = pct >= 0.80;
    const label = `$${spendData.month_to_date_usd.toFixed(2)} / $${spendData.budget_usd} est. this month`;
    spendLine = `
      <div class="settings-row">
        <div>
          <div class="settings-row-label">Modal GPU spend</div>
          <div class="settings-row-sub spend-compact-line${isWarning ? ' warning' : ''}" id="settings-spend-link">
            ${_esc(label)}${isWarning ? ' ⚠' : ''}
          </div>
        </div>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="color:var(--text-3)">
          <polyline points="9 18 15 12 9 6"/>
        </svg>
      </div>`;
  } catch {
    // Non-fatal — omit spend line
  }

  openSheet({
    title: 'Settings',
    body: `
      <div>
        <div class="settings-row">
          <div>
            <div class="settings-row-label">Mock mode</div>
            <div class="settings-row-sub">Use fixture data when server is offline</div>
          </div>
          <label class="toggle">
            <input type="checkbox" id="setting-mock" ${mockEnabled ? 'checked' : ''}>
            <div class="toggle-track"></div>
            <div class="toggle-knob"></div>
          </label>
        </div>
        <div class="settings-row">
          <div>
            <div class="settings-row-label">Notifications</div>
            <div class="settings-row-sub">Status: ${_esc(notifStatus)}</div>
          </div>
          ${notifStatus === 'default'
            ? `<button class="btn btn-secondary btn-sm" id="setting-notif-btn">Enable</button>`
            : `<span class="text-muted text-small">${_esc(notifStatus)}</span>`}
        </div>
        ${spendLine}
        <div class="settings-row">
          <div>
            <div class="settings-row-label">Sign out</div>
            <div class="settings-row-sub">Clear saved token</div>
          </div>
          <button class="btn btn-danger btn-sm" id="setting-logout">Sign out</button>
        </div>
      </div>`,
    primaryLabel: 'Done',
    primaryClass: 'btn-secondary',
    onPrimary: () => {
      const mockChk = document.getElementById('setting-mock');
      if (mockChk) {
        if (mockChk.checked) localStorage.setItem('mock', '1');
        else localStorage.removeItem('mock');
      }
      closeSheet();
    },
  });

  // Wire spend link → Analytics tab
  setTimeout(() => {
    const spendLink = document.getElementById('settings-spend-link');
    if (spendLink) {
      spendLink.addEventListener('click', () => {
        closeSheet();
        _activateTab('analytics');
      });
    }

    const notifBtn = document.getElementById('setting-notif-btn');
    if (notifBtn) {
      notifBtn.addEventListener('click', async () => {
        const perm = await Notification.requestPermission();
        notifBtn.textContent = perm;
        notifBtn.disabled = true;
        if (perm === 'granted') toast('Notifications enabled', 'success');
      });
    }

    const logoutBtn = document.getElementById('setting-logout');
    if (logoutBtn) {
      logoutBtn.addEventListener('click', () => {
        closeSheet();
        _logout();
      });
    }
  }, 100);
}

function _logout() {
  // Clear the media session cookie server-side (needs the token, so first).
  api.destroySession().catch(() => {});
  localStorage.removeItem(TOKEN_KEY);
  setToken('');
  clearInterval(_pollTimer);
  _pollTimer = null;
  _lastPendingCount = 0;
  _viewInitialized = { queue: false, campaigns: false, analytics: false };
  _showAuth();
}

function _onUnauthorized() {
  toast('Session expired. Please log in again.', 'error');
  localStorage.removeItem(TOKEN_KEY);
  setToken('');
  clearInterval(_pollTimer);
  _pollTimer = null;
  setTimeout(() => _showAuth('Session expired. Please enter the password.'), 800);
}

// ── Utility ───────────────────────────────────────────────────────────────────

function _esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
