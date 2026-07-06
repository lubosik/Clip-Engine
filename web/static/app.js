/**
 * app.js — Clip Engine PWA entry point.
 *
 * Responsibilities:
 *   - Auth: prompt for password, store as bearer token, re-prompt on 401.
 *   - Tab routing: Queue / Campaigns / Analytics.
 *   - Toast and bottom-sheet primitives (shared across views).
 *   - Stats polling every 60 s: update tab badge + fire browser notification
 *     if pending count increases.
 *   - Service worker registration.
 *   - Mock mode: if a network fetch fails AND localStorage.mock === "1",
 *     substitute fixture data (so the UI can be demoed without a server).
 */

import { api, setToken } from './api.js';
import * as fixtures from './fixtures.js';
import { initQueue, refreshQueue } from './queue.js';
import { initCampaigns } from './campaigns.js';
import { initAnalytics } from './analytics.js';

// ── Constants ─────────────────────────────────────────────────────────────────

const TOKEN_KEY  = 'clipEngineToken';
const POLL_MS    = 60_000;
const VIEWS      = ['queue', 'campaigns', 'analytics'];

// ── State ─────────────────────────────────────────────────────────────────────

let _activeTab       = 'queue';
let _pollTimer       = null;
let _lastPendingCount = 0;
let _viewInitialized = { queue: false, campaigns: false, analytics: false };

// ── Boot ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  _registerSW();

  const savedToken = localStorage.getItem(TOKEN_KEY) || '';
  if (savedToken) {
    setToken(savedToken);
    _bootApp();
  } else {
    _showAuth();
  }
});

// ── Service Worker ────────────────────────────────────────────────────────────

function _registerSW() {
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register('/sw.js').catch(() => {
    // Non-fatal — the app still works without a service worker.
  });
}

// ── Auth ──────────────────────────────────────────────────────────────────────

function _showAuth(message) {
  const screen = document.getElementById('auth-screen');
  const app    = document.getElementById('app');
  screen.classList.remove('hidden');
  app.style.display = 'none';

  const errEl  = document.getElementById('auth-error');
  const inp    = document.getElementById('auth-password');
  const btn    = document.getElementById('auth-submit');

  if (message) {
    errEl.textContent = message;
    errEl.style.display = 'block';
  } else {
    errEl.style.display = 'none';
  }

  inp.value = '';
  setTimeout(() => inp.focus(), 80);

  const attempt = async () => {
    const pw = inp.value.trim();
    if (!pw) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';

    setToken(pw);

    try {
      // Verify the token with a lightweight request
      await api.getStats();
      localStorage.setItem(TOKEN_KEY, pw);
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
          errEl.textContent = 'Could not reach server. Set localStorage.mock="1" for demo mode.';
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
    btn.classList.toggle('active', btn.dataset.tab === tab);
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
  };
}

// ── Mock fetch wrapper ────────────────────────────────────────────────────────

/**
 * Try the real API first. If the fetch throws a network error (no .status)
 * AND localStorage.mock === "1", call mockFn() instead.
 * 401 errors always propagate — they mean the server IS running and rejected us.
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
    if (err.status) {
      // HTTP error from server — propagate (including 401)
      throw err;
    }
    // Network error — use mock if enabled
    if (localStorage.getItem('mock') === '1') {
      return mockFn();
    }
    throw err;
  }
}

// ── Stats polling ─────────────────────────────────────────────────────────────

function _startPolling() {
  _poll();   // immediate first poll
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

    // Fire notification if pending increased
    if (pending > _lastPendingCount && _lastPendingCount >= 0) {
      const delta = pending - _lastPendingCount;
      _maybeNotify(delta);
    }
    _lastPendingCount = pending;

    // Refresh queue view silently if it's open
    if (_activeTab === 'queue') {
      try { refreshQueue(); } catch (_) { /* view may not be mounted yet */ }
    }
  } catch (_) {
    // Polling failures are silent — the main error handling is in the views
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
    tag: 'clip-engine-pending',   // replace previous notification
    renotify: true,
  });
}

// ── Toast ─────────────────────────────────────────────────────────────────────

/**
 * Show a toast notification.
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

  // Auto-dismiss
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transition = 'opacity 0.3s';
    setTimeout(() => el.remove(), 320);
  }, duration);
}

// ── Bottom sheet ──────────────────────────────────────────────────────────────

let _sheetBackdrop = null;

/**
 * Open a bottom sheet.
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
  closeSheet();   // ensure clean slate

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

  // Animate in
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

function _openSettings() {
  const mockEnabled  = localStorage.getItem('mock') === '1';
  const notifStatus  = 'Notification' in window ? Notification.permission : 'unsupported';

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
      // Save mock toggle
      const mockChk = document.getElementById('setting-mock');
      if (mockChk) {
        if (mockChk.checked) localStorage.setItem('mock', '1');
        else localStorage.removeItem('mock');
      }
      closeSheet();
    },
  });

  // Wire notification button (outside the primary/secondary flow)
  setTimeout(() => {
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
