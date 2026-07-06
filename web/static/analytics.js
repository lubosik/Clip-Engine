/**
 * analytics.js — Analytics view.
 *
 * Renders:
 *   - Per-channel weekly bar charts (inline SVG, no chart library)
 *   - Top clips table
 *
 * Exported API:
 *   initAnalytics(container, ctx)
 */

export function initAnalytics(container, ctx) {
  _render(container, ctx);
}

async function _render(container, ctx) {
  container.innerHTML = `
    <div id="analytics-inner">
      <div id="analytics-header-row" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
        <div class="section-title" style="margin-bottom:0">Weekly performance</div>
        <select id="analytics-weeks" class="form-control" style="width:auto;min-height:36px;font-size:13px;padding:6px 28px 6px 10px;">
          <option value="6">6 weeks</option>
          <option value="8" selected>8 weeks</option>
          <option value="12">12 weeks</option>
        </select>
      </div>
      <div id="analytics-body">
        ${_skeleton()}
      </div>
    </div>`;

  const weeksEl = container.querySelector('#analytics-weeks');
  weeksEl.addEventListener('change', () => _load(container, ctx, parseInt(weeksEl.value, 10)));

  _load(container, ctx, 8);
}

async function _load(container, ctx, weeks) {
  const body = document.getElementById('analytics-body');
  if (!body) return;

  body.innerHTML = _skeleton();

  try {
    const data = await ctx.mockFetch(
      () => ctx.api.getAnalytics({ weeks }),
      () => ctx.fixtures.analytics
    );
    _paint(body, data);
  } catch (err) {
    if (err.status === 401) { ctx.onUnauthorized(); return; }
    body.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">📊</div>
        <h3>No analytics yet</h3>
        <p>Analytics are pulled weekly after clips are posted.</p>
      </div>`;
    if (err.message && !err.message.includes('network')) {
      ctx.toast('Analytics: ' + err.message, 'error');
    }
  }
}

function _paint(body, data) {
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
      const platformBadge = _platformBadge(clip.platform);
      const link = clip.permalink
        ? `<a href="${_esc(clip.permalink)}" target="_blank" rel="noopener">${_esc(_truncate(clip.hook, 60))}</a>`
        : _esc(_truncate(clip.hook, 60));

      tr.innerHTML = `
        <td class="hook-cell" title="${_esc(clip.hook)}">${link}</td>
        <td>${platformBadge}</td>
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
        <div class="empty-state-icon">📊</div>
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

  const W = 340;  // logical SVG width; scales with CSS
  const H = 100;
  const barW = Math.floor((W - 20) / weekly.length) - 4;
  const maxViews = Math.max(...weekly.map((w) => w.views || 0), 1);

  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H + 28}`);
  svg.setAttribute('aria-label', 'Weekly views bar chart');
  svg.setAttribute('role', 'img');

  weekly.forEach((week, i) => {
    const barH = Math.max(2, Math.floor(((week.views || 0) / maxViews) * H));
    const x = 10 + i * (barW + 4);
    const y = H - barH;

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

    // Date label (Mon DD)
    const dateStr = week.week_start
      ? _shortDate(week.week_start)
      : `W${i + 1}`;
    const label = document.createElementNS(svgNS, 'text');
    label.setAttribute('x', x + barW / 2);
    label.setAttribute('y', H + 14);
    label.setAttribute('text-anchor', 'middle');
    label.setAttribute('class', 'bar-label');
    label.textContent = dateStr;
    svg.appendChild(label);

    // Value label above bar (only if bar is tall enough)
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
  const total = weekly.reduce((sum, w) => sum + (w.views || 0), 0);
  const totalLikes = weekly.reduce((sum, w) => sum + (w.likes || 0), 0);
  const info = document.createElement('div');
  info.style.cssText = 'display:flex;gap:16px;margin-top:8px;font-size:12px;color:var(--text-2)';
  info.innerHTML = `
    <span><strong style="color:var(--text)">${_fmt(total)}</strong> views</span>
    <span><strong style="color:var(--text)">${_fmt(totalLikes)}</strong> likes</span>
    <span><strong style="color:var(--text)">${weekly.reduce((s,w) => s + (w.posts||0), 0)}</strong> posts</span>`;
  wrap.appendChild(info);

  return wrap;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _platformBadge(platform) {
  const map = { tiktok: 'TikTok', instagram: 'IG', youtube: 'YT', x: 'X' };
  return `<span class="chip" style="font-size:10px">${map[platform] || _esc(platform)}</span>`;
}

function _shortDate(iso) {
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function _fmt(n) {
  if (n == null) return '—';
  return n.toLocaleString();
}

function _compact(n) {
  if (n == null) return '';
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
