/**
 * api.js — Thin API client.
 *
 * Every authenticated request sends `Authorization: Bearer <token>`.
 * A 401 response throws an error with `.status === 401` so the caller
 * (app.js) can re-prompt for the password.
 *
 * Network failures (server not reachable) throw a plain Error without
 * a `.status` property — the caller can detect these and use mock data
 * when localStorage.mock === "1".
 */

let _token = '';

/** Set the bearer token used by all subsequent requests. */
export function setToken(t) {
  _token = t;
}

export function getToken() {
  return _token;
}

// ── Internal request helper ───────────────────────────────────────────────────

async function request(method, path, { body, multipart = false } = {}) {
  const headers = {};
  if (_token) headers['Authorization'] = `Bearer ${_token}`;
  if (body && !multipart) headers['Content-Type'] = 'application/json';

  const opts = { method, headers };
  if (body) opts.body = multipart ? body : JSON.stringify(body);

  let res;
  try {
    res = await fetch(path, opts);
  } catch (networkErr) {
    // Server unreachable — rethrow without a .status so callers can
    // distinguish it from an HTTP error.
    throw networkErr;
  }

  if (res.status === 401) {
    const err = new Error('Unauthorized');
    err.status = 401;
    throw err;
  }

  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      message = body.detail || body.message || message;
    } catch { /* ignore parse error */ }
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }

  return res.json();
}

// ── Public API surface ────────────────────────────────────────────────────────

export const api = {
  // Session cookie — lets <video>/<img> tags authenticate (they can't send
  // the Bearer header). Called after unlock and on boot with a saved token.
  createSession() {
    return request('POST', '/api/auth/session');
  },
  destroySession() {
    return request('DELETE', '/api/auth/session');
  },

  // Hero media (unauthenticated — called before login)
  getHero() {
    return fetch('/api/hero').then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    });
  },

  // Stats (used for empty-state copy + notification badge)
  getStats() {
    return request('GET', '/api/stats');
  },

  // Clips — gains ?kind=clip|meme filter per contract §6
  getClips(params = {}) {
    const entries = Object.entries(params).filter(([, v]) => v != null && v !== '');
    const qs = entries.length ? '?' + new URLSearchParams(entries).toString() : '';
    return request('GET', '/api/clips' + qs);
  },

  approveClip(id, captionOverride) {
    const body = captionOverride ? { caption: captionOverride } : undefined;
    return request('POST', `/api/clips/${id}/approve`, body ? { body } : {});
  },

  // payload: {reasons: ["weak_hook",...], note?: "optional"} — legacy {reason:"text"} also accepted
  rejectClip(id, payload) {
    return request('POST', `/api/clips/${id}/reject`, { body: payload ?? {} });
  },

  patchClip(id, caption) {
    return request('PATCH', `/api/clips/${id}`, { body: { caption } });
  },

  // AI review gate override — moves a failed clip into the review queue
  overrideGate(id) {
    return request('POST', `/api/clips/${id}/override-gate`);
  },

  // Campaigns
  getCampaigns() {
    return request('GET', '/api/campaigns');
  },

  getCampaign(name) {
    return request('GET', `/api/campaigns/${encodeURIComponent(name)}`);
  },

  createCampaign(formData) {
    return request('POST', '/api/campaigns', { body: formData, multipart: true });
  },

  updateCampaign(name, formData) {
    return request('PUT', `/api/campaigns/${encodeURIComponent(name)}`, { body: formData, multipart: true });
  },

  // PATCH /api/campaigns/{name}/engines  body: {clips?: bool, memes?: bool}
  patchCampaignEngines(name, body) {
    return request('PATCH', `/api/campaigns/${encodeURIComponent(name)}/engines`, { body });
  },

  // PATCH /api/campaigns/{name}/mode  body: {mode}
  patchCampaignMode(name, mode) {
    return request('PATCH', `/api/campaigns/${encodeURIComponent(name)}/mode`, { body: { mode } });
  },

  // Analytics
  getAnalytics(params = {}) {
    const entries = Object.entries(params).filter(([, v]) => v != null && v !== '');
    const qs = entries.length ? '?' + new URLSearchParams(entries).toString() : '';
    return request('GET', '/api/analytics' + qs);
  },

  // Modal spend widget — contract §5
  getSpend(params = {}) {
    const entries = Object.entries(params).filter(([, v]) => v != null && v !== '');
    const qs = entries.length ? '?' + new URLSearchParams(entries).toString() : '';
    return request('GET', '/api/spend' + qs);
  },

  // Manual run trigger
  triggerRun(campaign) {
    return request('POST', `/api/runs/${encodeURIComponent(campaign)}`);
  },

  // Sources view
  getSources(params = {}) {
    const entries = Object.entries(params).filter(([, v]) => v != null && v !== '');
    const qs = entries.length ? '?' + new URLSearchParams(entries).toString() : '';
    return request('GET', '/api/sources' + qs);
  },

  // In-progress sources (stage != complete and not failed >24h)
  getSourcesProgress() {
    return request('GET', '/api/sources?in_progress=1');
  },

  // Approval-rate time series — GET /api/analytics/approval-rate?campaign=X&weeks=N
  getApprovalRate(campaign, weeks = 8) {
    const qs = new URLSearchParams({ campaign, weeks: String(weeks) });
    return request('GET', `/api/analytics/approval-rate?${qs}`);
  },

  // Preference profile for a campaign — GET /api/campaigns/{name}/profile
  getProfile(campaign) {
    return request('GET', `/api/campaigns/${encodeURIComponent(campaign)}/profile`);
  },
};
