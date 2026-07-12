/**
 * analytics.js — Analytics view.
 *
 * Renders (top → bottom):
 *   1. Spend widget — month-to-date AI cost bar + per-campaign breakdown
 *      (fetches GET /api/spend; amber warning at ≥80% budget)
 *   2. Per-channel weekly bar charts (inline SVG, no chart library)
 *   3. Top clips table
 *
 * Exported API:
 *   initAnalytics(container, ctx)
 */

export function initAnalytics(container, ctx) {
  _render(container, ctx);
}

// ── Main render ───────────────────────────────────────────────────────────────

async function _render(container, ctx) {
  container.innerHTML = `
    <div id="analytics-inner">
      <!-- Spend widget placeholder -->
      <div id="spend-widget-wrap"></div>

      <div id="analytics-header-row"
           style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;margin-top:8px;">
        <div class="section-title" style="margin-bottom:0">Weekly performance</div>
        <select id="analytics-weeks" class="form-control"
                style="width:auto;min-height:36px;font-size:13px;padding:6px 28px 6px 10px;">
          <option value="6">6 weeks</option>
          <option value="8" selected>8 weeks</option>
          <option value="12">12 weeks</option>
        </select>
      </div>

      <div id="analytics-body">
        ${_skeleton()}
      </div>
    </div>`;

  // Load spend + analytics in parallel
  const weeksEl = container.querySelector('#analytics-weeks');
  weeksEl.addEventListener('change', () => _loadAnalytics(container, ctx, parseInt(weeksEl.value, 10)));

  _loadSpend(container, ctx);
  _loadAnalytics(container, ctx, 8);
}

// ── Spend widget ──────────────────────────────────────────────────────────────

async function _loadSpend(container, ctx) {
  const wrap = document.getElementById('spend-widget-wrap');
  if (!wrap) return;

  // Skeleton spend card
  wrap.innerHTML = `
    <div class="spend-widget" aria-busy="true">
      <div class="skeleton" style="height:14px;width:140px;margin-bottom:12px;"></div>
      <div class="skeleton" style="height:8px;width:100%;border-radius:99px;"></div>
      <div class="skeleton" style="height:12px;width:60%;margin-top:10px;"></div>
    </div>`;

  try {
    const data = await ctx.mockFetch(
      () => ctx.api.getSpend(),
      () => ctx.fixtures.spend
    );
    _paintSpend(wrap, data);
  } catch (err) {
    if (err.status === 401) { ctx.onUnauthorized(); return; }
    wrap.innerHTML = `
      <div class="spend-widget">
        <div class="spend-widget-title">AI Spend</div>
        <p style="color:var(--text-3);font-size:13px">Could not load spend data.</p>
      </div>`;
  }
}

function _paintSpend(wrap, data) {
  if (!data) { wrap.innerHTML = ''; return; }

  const budget  = data.budget_usd        || 0;
  const mtd     = data.month_to_date_usd || 0;
  const pct     = budget > 0 ? Math.min(100, (mtd / budget) * 100) : 0;
  const warning = pct >= 80;
  const byCamp  = Array.isArray(data.by_campaign) ? data.by_campaign : [];
  const apify   = data.apify || null;

  // Apify section — REAL billed costs from the apify_runs ledger
  let apifyBlock = '';
  if (apify && (apify.runs > 0 || apify.total_usd > 0)) {
    const kindRows = (Array.isArray(apify.by_kind) ? apify.by_kind : []).map((k) => `
      <div class="spend-campaign-row">
        <span class="spend-campaign-name">${_esc(k.kind)} · ${k.items} items</span>
        <span class="spend-campaign-val">$${_fmtUsd(k.usd)}</span>
      </div>`).join('');
    const perVideo = apify.avg_cost_per_video_usd != null
      ? `<div class="spend-campaign-row">
           <span class="spend-campaign-name">avg / video scraped</span>
           <span class="spend-campaign-val">$${_fmtUsd(apify.avg_cost_per_video_usd)}</span>
         </div>`
      : '';
    apifyBlock = `
      <div class="spend-by-campaign" style="margin-top:10px">
        <div class="spend-campaign-row" style="font-weight:600">
          <span class="spend-campaign-name">Apify (real billed)</span>
          <span class="spend-campaign-val">$${_fmtUsd(apify.total_usd)}</span>
        </div>
        ${kindRows}
        ${perVideo}
      </div>`;
  }

  // Per-campaign rows using existing CSS class names
  const breakdownRows = byCamp.map((row) => `
    <div class="spend-campaign-row">
      <span class="spend-campaign-name">${_esc(row.campaign)}</span>
      <span class="spend-campaign-val">$${_fmtUsd(row.usd)}</span>
    </div>`).join('');

  // Recent charges (collapsible)
  const recentRows = (Array.isArray(data.recent) ? data.recent.slice(0, 3) : []).map((row) => `
    <div class="spend-campaign-row">
      <span class="spend-campaign-name" title="${_esc(row.gpu || '')}">${_esc(row.campaign)} · ${_esc(row.gpu || 'cpu')}</span>
      <span class="spend-campaign-val">$${_fmtUsd(row.usd)}</span>
    </div>`).join('');

  wrap.innerHTML = `
    <div class="spend-widget" role="region" aria-label="AI spend this month">
      <div class="spend-header-row">
        <div class="spend-widget-title">
          AI Spend — This Month
          ${data.estimated ? '<span style="font-size:9px;font-weight:600;letter-spacing:.5px;padding:2px 6px;background:rgba(255,180,84,.15);border:1px solid rgba(255,180,84,.30);border-radius:4px;color:var(--amber);vertical-align:middle;margin-left:6px">estimated</span>' : ''}
        </div>
        <span class="spend-budget">$${_fmtUsd(budget)} budget</span>
      </div>

      <div class="spend-amount${warning ? ' warning' : ''}">$${_fmtUsd(mtd)}</div>

      <div class="spend-bar-track" role="progressbar"
           aria-valuenow="${Math.round(pct)}" aria-valuemin="0" aria-valuemax="100"
           aria-label="${Math.round(pct)}% of monthly AI budget used">
        <div class="spend-bar-fill${warning ? ' warning' : ''}"
             style="width:${pct.toFixed(1)}%"></div>
      </div>

      ${warning ? `
        <div class="spend-warning-banner" role="alert">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" aria-hidden="true">
            <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86
                     a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/>
            <line x1="12" y1="17" x2="12.01" y2="17"/>
          </svg>
          ${Math.round(pct)}% of monthly budget used
        </div>` : ''}

      ${byCamp.length > 0 ? `
        <div class="spend-by-campaign">
          ${breakdownRows}
        </div>` : ''}

      ${apifyBlock}

      ${recentRows ? `
        <details style="margin-top:8px">
          <summary style="font-size:11px;color:var(--text-3);cursor:pointer;user-select:none;letter-spacing:.4px;text-transform:uppercase">
            Recent charges
          </summary>
          <div style="margin-top:6px">${recentRows}</div>
        </details>` : ''}

      ${data.plan_note ? `
        <div class="spend-estimated-note" style="margin-top:8px">${_esc(data.plan_note)}</div>` : ''}
    </div>`;
}

// ── Analytics (charts + table) ────────────────────────────────────────────────

async function _loadAnalytics(container, ctx, weeks) {
  const body = document.getElementById('analytics-body');
  if (!body) return;

  body.innerHTML = _skeleton();

  try {
    const data = await ctx.mockFetch(
      () => ctx.api.getAnalytics({ weeks }),
      () => ctx.fixtures.analytics
    );
    _paintAnalytics(body, data);
  } catch (err) {
    if (err.status === 401) { ctx.onUnauthorized(); return; }
    body.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-floor" aria-hidden="true"></div>
        <h3>No analytics yet</h3>
        <p>Analytics are pulled weekly after clips are posted.</p>
      </div>`;
    if (err.message && !err.message.includes('network')) {
      ctx.toast('Analytics: ' + err.message, 'error');
    }
  }
}

function _paintAnalytics(body, data) {
  if (!data) {
    body.innerHTML = '<div class="empty-state"><h3>No data</h3></div>';
    return;
  }

  const { channels = [], clips = [] } = data;
  body.innerHTML = '';

  // ── Channel charts ──────────────────────────────────────────────────────────
  if (channels.length > 0) {
    const section = document.createElement('div');
    section.className = 'analytics-section';

    channels.forEach((ch) => {
      const card = document.createElement('div');
      card.className = 'analytics-channel-card';
      card.innerHTML = `<div class="analytics-channel-name">${_esc(ch.channel)}</div>`;
      card.appendChild(_buildChart(ch.weekly || []));
      section.appendChild(card);
    });

    body.appendChild(section);
  }

  // ── Top clips table ─────────────────────────────────────────────────────────
  if (clips.length > 0) {
    const sec2 = document.createElement('div');
    sec2.className = 'analytics-section';
    sec2.innerHTML = `<div class="section-title">Top clips</div>`;

    const table = document.createElement('table');
    table.className = 'analytics-table';
    table.innerHTML = `
      <thead>
        <tr>
          <th>Hook</th>
          <th>Platform</th>
          <th class="num">Views</th>
          <th class="num">Likes</th>
        </tr>
      </thead>`;

    const tbody = document.createElement('tbody');
    clips.forEach((clip) => {
      const tr = document.createElement('tr');
      const modeBadge = clip.mode === 'demo'
        ? '<span class="badge-demo" style="font-size:9px;padding:1px 5px;vertical-align:middle;margin-left:4px">DEMO</span>'
        : '';
      const link = clip.permalink
        ? `<a href="${_esc(clip.permalink)}" target="_blank" rel="noopener">${_esc(_truncate(clip.hook, 55))}</a>`
        : _esc(_truncate(clip.hook, 55));

      tr.innerHTML = `
        <td class="hook-cell" title="${_esc(clip.hook)}">${link}${modeBadge}</td>
        <td>${_platformBadge(clip.platform)}</td>
        <td class="num">${_fmt(clip.views)}</td>
        <td class="num">${_fmt(clip.likes)}</td>`;
      tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    sec2.appendChild(table);
    body.appendChild(sec2);
  }

  if (channels.length === 0 && clips.length === 0) {
    body.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-floor" aria-hidden="true"></div>
        <h3>No analytics yet</h3>
        <p>Analytics are pulled weekly after clips are posted.</p>
      </div>`;
  }
}

// ── SVG bar chart ─────────────────────────────────────────────────────────────

function _buildChart(weekly) {
  const wrap = document.createElement('div');
  wrap.className = 'bar-chart-wrap';

  if (!weekly || weekly.length === 0) {
    wrap.innerHTML = '<p class="text-muted text-small" style="padding:8px 0">No data</p>';
    return wrap;
  }

  const W    = 340;
  const H    = 100;
  const barW = Math.floor((W - 20) / weekly.length) - 4;
  const maxViews = Math.max(...weekly.map((w) => w.views || 0), 1);

  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H + 28}`);
  svg.setAttribute('aria-label', 'Weekly views bar chart');
  svg.setAttribute('role', 'img');

  weekly.forEach((week, i) => {
    const barH = Math.max(2, Math.floor(((week.views || 0) / maxViews) * H));
    const x    = 10 + i * (barW + 4);
    const y    = H - barH;

    // Background track
    const bg = document.createElementNS(svgNS, 'rect');
    bg.setAttribute('x', x);
    bg.setAttribute('y', 0);
    bg.setAttribute('width', barW);
    bg.setAttribute('height', H);
    bg.setAttribute('class', 'bar-rect-bg');
    bg.setAttribute('rx', '2');
    svg.appendChild(bg);

    // Filled bar
    const bar = document.createElementNS(svgNS, 'rect');
    bar.setAttribute('x', x);
    bar.setAttribute('y', y);
    bar.setAttribute('width', barW);
    bar.setAttribute('height', barH);
    bar.setAttribute('class', 'bar-rect');
    bar.setAttribute('rx', '2');
    svg.appendChild(bar);

    // Date label
    const dateStr = week.week_start ? _shortDate(week.week_start) : `W${i + 1}`;
    const label = document.createElementNS(svgNS, 'text');
    label.setAttribute('x', x + barW / 2);
    label.setAttribute('y', H + 14);
    label.setAttribute('text-anchor', 'middle');
    label.setAttribute('class', 'bar-label');
    label.textContent = dateStr;
    svg.appendChild(label);

    // Value label above bar
    if (barH > 18 && (week.views || 0) > 0) {
      const vLabel = document.createElementNS(svgNS, 'text');
      vLabel.setAttribute('x', x + barW / 2);
      vLabel.setAttribute('y', y - 2);
      vLabel.setAttribute('text-anchor', 'middle');
      vLabel.setAttribute('class', 'bar-value');
      vLabel.textContent = _compact(week.views);
      svg.appendChild(vLabel);
    }
  });

  wrap.appendChild(svg);

  // Totals row
  const total      = weekly.reduce((s, w) => s + (w.views || 0), 0);
  const totalLikes = weekly.reduce((s, w) => s + (w.likes || 0), 0);
  const totalPosts = weekly.reduce((s, w) => s + (w.posts || 0), 0);

  const info = document.createElement('div');
  info.style.cssText = 'display:flex;gap:16px;margin-top:8px;font-size:12px;color:var(--text-2)';
  info.innerHTML = `
    <span><strong style="color:var(--text)">${_fmt(total)}</strong> views</span>
    <span><strong style="color:var(--text)">${_fmt(totalLikes)}</strong> likes</span>
    <span><strong style="color:var(--text)">${totalPosts}</strong> posts</span>`;
  wrap.appendChild(info);

  return wrap;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _platformBadge(platform) {
  const map = { tiktok: 'TikTok', instagram: 'IG', youtube: 'YT', x: 'X' };
  return `<span class="chip" style="font-size:10px">${_esc(map[platform] || platform)}</span>`;
}

function _shortDate(iso) {
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function _fmt(n) {
  if (n == null) return '—';
  return n.toLocaleString();
}

function _fmtUsd(n) {
  if (n == null) return '0.00';
  return Number(n).toFixed(2);
}

function _compact(n) {
  if (n == null)      return '';
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000)     return (n / 1_000).toFixed(0) + 'K';
  return String(n);
}

function _truncate(str, max) {
  if (!str) return '';
  return str.length > max ? str.slice(0, max) + '…' : str;
}

function _skeleton() {
  return `
    <div class="analytics-channel-card">
      <div class="skeleton" style="height:14px;width:120px;margin-bottom:12px;"></div>
      <div class="skeleton" style="height:128px;width:100%;border-radius:4px;"></div>
    </div>
    <div class="analytics-channel-card">
      <div class="skeleton" style="height:14px;width:100px;margin-bottom:12px;"></div>
      <div class="skeleton" style="height:128px;width:100%;border-radius:4px;"></div>
    </div>`;
}

function _esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
