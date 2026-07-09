/**
 * campaigns.js — Campaigns list + Campaign builder wizard.
 *
 * Fixes:
 *   - [object Object] bug: schedule is now {label, posts_per_day, times, timezone};
 *     renders schedule.label.
 *   - sources_summary: now [{platform, count, label}] array, renders as chips.
 *
 * New features (revamp v2):
 *   - Mode badge (DEMO / LIVE) on each campaign card.
 *   - Per-engine toggles (Clips / Memes) with PATCH /api/campaigns/{name}/engines.
 *   - Wizard step 1: mode + engines.
 *   - Wizard step 3: creative direction.
 *   - Wizard step 4: visual reference images + meme refs upload.
 *   - Wizard step 5: demo test channels.
 *
 * Wizard steps:
 *   1. Basics       — name, enabled, mode (demo/production), engines (Clips/Memes)
 *   2. Sources      — YouTube, TikTok, Instagram
 *   3. Ranking & Creative — clip rules + creative direction
 *   4. Look & Feel  — captions, hook, watermark, assets (logo/badge/outro/font/refs)
 *   5. Destinations — channels, schedule, caption template, hashtags, autopost,
 *                     demo test channels (if mode=demo)
 *   6. Review       — summary + submit
 *
 * Exported API:
 *   initCampaigns(container, ctx)
 */

// ── Platform icons (inline SVG) ───────────────────────────────────────────────

const PLATFORM_ICON_SM = {
  youtube: `<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M22.54 6.42a2.78 2.78 0 00-1.94-1.96C18.88 4 12 4 12 4s-6.88 0-8.59.46a2.78 2.78 0 00-1.95 1.96A29 29 0 001 12a29 29 0 00.46 5.58A2.78 2.78 0 003.41 19.58C5.12 20 12 20 12 20s6.88 0 8.59-.42a2.78 2.78 0 001.94-1.97A29 29 0 0023 12a29 29 0 00-.46-5.58zM9.75 15.02V8.98L15.5 12l-5.75 3.02z"/></svg>`,
  tiktok:    `<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M19.59 6.69a4.83 4.83 0 01-3.77-4.25V2h-3.45v13.67a2.89 2.89 0 01-2.88 2.5 2.89 2.89 0 01-2.89-2.89 2.89 2.89 0 012.89-2.89c.28 0 .54.04.79.1V9.01a6.34 6.34 0 00-.79-.05 6.34 6.34 0 00-6.34 6.34 6.34 6.34 0 006.34 6.34 6.34 6.34 0 006.33-6.34V8.94a8.17 8.17 0 004.77 1.52V7a4.85 4.85 0 01-1-.31z"/></svg>`,
  instagram: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="2" y="2" width="20" height="20" rx="5"/><circle cx="12" cy="12" r="4"/><circle cx="17.5" cy="6.5" r="1.2" fill="currentColor" stroke="none"/></svg>`,
};

const DEFAULT_RANKING_RULES = `Prefer moments that are genuinely useful or interesting on their own:
a clear explanation of a mechanism, a specific actionable tip, a
surprising-but-true fact, a vivid story, a strong opinion with reasoning.
Each clip must stand alone with a hook in the first 2 seconds.
EXCLUDE: unsafe or extreme advice, dangerous dieting/cutting, anything
promoting disordered eating, medical claims presented as fact, and
anything that would violate TikTok/Instagram community guidelines.
When in doubt, skip it.`;

const UPLOADED_WITHIN_OPTS = [
  { value: 'hour',  label: 'Past hour' },
  { value: 'day',   label: 'Past 24 hours' },
  { value: 'week',  label: 'Past week' },
  { value: 'month', label: 'Past month' },
  { value: 'year',  label: 'Past year' },
];

const POSITION_OPTS_CAPTION = [
  { value: 'upper_mid', label: 'Upper middle' },
  { value: 'center',    label: 'Center' },
  { value: 'lower_mid', label: 'Lower middle' },
  { value: 'bottom',    label: 'Bottom' },
];

const POSITION_OPTS_OVERLAY = [
  { value: 'center',       label: 'Center' },
  { value: 'top_left',     label: 'Top left' },
  { value: 'top_right',    label: 'Top right' },
  { value: 'bottom_left',  label: 'Bottom left' },
  { value: 'bottom_right', label: 'Bottom right' },
];

const TZ_OPTS = [
  'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
  'America/Toronto', 'Europe/London', 'Europe/Paris', 'Europe/Berlin',
  'Asia/Dubai', 'Asia/Tokyo', 'Asia/Singapore', 'Australia/Sydney',
  'Pacific/Auckland',
];

// ── Wizard state ──────────────────────────────────────────────────────────────

/** @type {Object} */
let _wiz = {};

/**
 * @type {{
 *   logo: File|null, corner_badge: File|null, outro: File|null, font: File|null,
 *   visual_refs: File[], meme_refs: File[]
 * }}
 */
let _files = { logo: null, corner_badge: null, outro: null, font: null, visual_refs: [], meme_refs: [] };

let _step     = 0;
let _editName = null;
const TOTAL_STEPS = 6;
let _ctx = null;

// ── Public ────────────────────────────────────────────────────────────────────

export function initCampaigns(container, ctx) {
  _ctx = ctx;
  _renderList(container);
}

// ── List view ─────────────────────────────────────────────────────────────────

async function _renderList(container) {
  container.innerHTML = `<div id="campaigns-list">${_skeletonList()}</div>`;

  let fab = document.getElementById('campaigns-fab');
  if (!fab) {
    fab = document.createElement('button');
    fab.id = 'campaigns-fab';
    fab.className = 'fab';
    fab.textContent = '+';
    fab.title = 'New campaign';
    fab.setAttribute('aria-label', 'New campaign');
    document.body.appendChild(fab);
  }
  fab.style.display = '';
  fab.onclick = () => _openWizard(null);

  try {
    const data = await _ctx.mockFetch(
      () => _ctx.api.getCampaigns(),
      () => _ctx.fixtures.campaigns
    );
    _paintList(data);
  } catch (err) {
    if (err.status === 401) { _ctx.onUnauthorized(); return; }
    document.getElementById('campaigns-list').innerHTML =
      `<div class="empty-state">
         <div class="empty-state-floor" aria-hidden="true"></div>
         <h3>Could not load campaigns</h3>
         <p>${_esc(err.message)}</p>
       </div>`;
  }
}

function _paintList(campaigns) {
  const list = document.getElementById('campaigns-list');
  if (!list) return;

  if (!campaigns || campaigns.length === 0) {
    list.innerHTML = `
      <div class="empty-state">
        <svg class="empty-state-lightstream" viewBox="0 0 320 80" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
          <defs>
            <linearGradient id="ls-grad-camp" x1="0%" y1="0%" x2="100%" y2="0%">
              <stop offset="0%"   stop-color="#00e5ff" stop-opacity="0"/>
              <stop offset="50%"  stop-color="#00e5ff" stop-opacity="0.65"/>
              <stop offset="100%" stop-color="#7c4ef0" stop-opacity="0"/>
            </linearGradient>
          </defs>
          <path d="M-10 55 Q80 15 160 45 Q230 70 340 20"
            stroke="url(#ls-grad-camp)" stroke-width="1.5" fill="none"/>
        </svg>
        <h3>No campaigns yet</h3>
        <p>Tap + to create your first campaign.</p>
      </div>`;
    return;
  }

  list.innerHTML = '';
  campaigns.forEach((c) => {
    list.appendChild(_buildCampaignCard(c));
  });
}

function _buildCampaignCard(c) {
  const card = document.createElement('div');
  card.className = 'campaign-card';

  const dot  = c.enabled ? 'on' : 'off';
  const mode = c.mode || 'production';
  const engines = c.engines || { clips: true, memes: false };

  // Fix [object Object]: schedule is now an object with .label
  const scheduleTxt = typeof c.schedule === 'object' && c.schedule !== null
    ? (c.schedule.label || '—')
    : (c.schedule || '—');

  // sources_summary: new shape is [{platform, count, label}]
  let sourceChipsHtml = '';
  if (Array.isArray(c.sources_summary)) {
    sourceChipsHtml = c.sources_summary.map((s) => {
      const icon = PLATFORM_ICON_SM[s.platform] || '';
      return `<span class="source-chip">${icon}${_esc(s.label)}</span>`;
    }).join('');
  } else if (typeof c.sources_summary === 'string') {
    sourceChipsHtml = `<span class="source-chip">${_esc(c.sources_summary)}</span>`;
  }

  const modeHtml = mode === 'demo'
    ? `<span class="badge-demo">DEMO</span>`
    : `<span class="badge-live">LIVE</span>`;

  card.innerHTML = `
    <div class="campaign-header">
      <div class="status-dot ${dot}" aria-label="${c.enabled ? 'Enabled' : 'Disabled'}"></div>
      <div class="campaign-name">${_esc(c.name)}</div>
      ${modeHtml}
      <span class="chip">${c.pending_count ?? 0} pending</span>
    </div>
    <div class="campaign-sources">${sourceChipsHtml || '<span class="chip">No sources</span>'}</div>
    <div class="campaign-schedule">${_esc(scheduleTxt)}</div>
    <div class="engine-toggles" id="eng-${_esc(c.name)}" role="group" aria-label="Engine toggles for ${_esc(c.name)}">
      <button class="engine-btn${engines.clips ? ' active' : ''}" data-engine="clips" data-campaign="${_esc(c.name)}" aria-pressed="${engines.clips}" title="Clips engine">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2"/>
        </svg>
        Clips
      </button>
      <button class="engine-btn${engines.memes ? ' active' : ''}" data-engine="memes" data-campaign="${_esc(c.name)}" aria-pressed="${engines.memes}" title="Memes engine">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/>
          <polyline points="21 15 16 10 5 21"/>
        </svg>
        Memes
      </button>
    </div>`;

  // Wire engine toggle buttons — stop propagation so card click doesn't fire
  const engWrap = card.querySelector(`#eng-${CSS.escape(c.name)}`);
  if (engWrap) {
    engWrap.addEventListener('click', (e) => {
      e.stopPropagation();
      const btn = e.target.closest('.engine-btn');
      if (!btn) return;
      _toggleEngine(c, btn, engines);
    });
  }

  card.addEventListener('click', () => _openWizard(c.name));
  return card;
}

async function _toggleEngine(campaign, btn, currentEngines) {
  const engine = btn.dataset.engine;       // 'clips' | 'memes'
  const campaignName = btn.dataset.campaign;
  const newVal = !currentEngines[engine];

  // Optimistic update
  btn.classList.toggle('active', newVal);
  btn.setAttribute('aria-pressed', String(newVal));
  btn.classList.add('loading');

  const body = {};
  body[engine] = newVal;

  try {
    await _ctx.mockFetch(
      () => _ctx.api.patchCampaignEngines(campaignName, body),
      () => ({ engines: { ...currentEngines, [engine]: newVal } })
    );
    currentEngines[engine] = newVal;
    _ctx.toast(`${engine.charAt(0).toUpperCase() + engine.slice(1)} engine ${newVal ? 'enabled' : 'disabled'}`, 'success');
  } catch (err) {
    // Revert
    btn.classList.toggle('active', !newVal);
    btn.setAttribute('aria-pressed', String(!newVal));
    if (err.status === 401) { _ctx.onUnauthorized(); return; }
    _ctx.toast('Could not update engine: ' + err.message, 'error');
  } finally {
    btn.classList.remove('loading');
  }
}

// ── Wizard ─────────────────────────────────────────────────────────────────────

function _resetWiz(prefill) {
  _wiz = {
    // Step 1
    name:     '',
    enabled:  true,
    mode:     'demo',
    engines_clips: true,
    engines_memes: false,
    // Step 2
    yt_search_terms:    [],
    yt_channels:        [],
    yt_min_view_count:  20000,
    yt_uploaded_within: 'year',
    tt_profiles:  [],
    tt_hashtags:  [],
    ig_profiles:  [],
    // Step 3
    clip_len_min:         20,
    clip_len_max:         60,
    max_clips_per_source: 8,
    exhaust_source:       false,
    min_score:            0.6,
    ranking_rules:        DEFAULT_RANKING_RULES,
    creative_direction:   '',
    // Step 4
    caption_base_color:      '#FFFFFF',
    caption_highlight_color: '#00E5FF',
    caption_outline_color:   '#000000',
    caption_outline_px:      6,
    caption_max_words:       4,
    caption_position:        'upper_mid',
    hook_enabled:    true,
    hook_seconds:    8,
    watermark_opacity:  0.18,
    watermark_scale:    0.5,
    watermark_position: 'center',
    badge_opacity:   1.0,
    badge_scale:     0.12,
    badge_position:  'top_right',
    outro_enabled:   false,
    outro_audio:     'keep',
    // Step 5
    postiz_channels:  [],
    posts_per_day:    1,
    schedule_times:   ['17:00'],
    timezone:         'America/New_York',
    caption_template: '{hook}\n\nvia @{source_handle}\n{hashtags}',
    hashtags:         [],
    autopost:         false,
    demo_test_channels: [],
    ...prefill,
  };
  _files = { logo: null, corner_badge: null, outro: null, font: null, visual_refs: [], meme_refs: [] };
}

async function _openWizard(campaignName) {
  _editName = campaignName;
  _step = 0;
  _resetWiz({});

  if (campaignName) {
    try {
      const cfg = await _ctx.mockFetch(
        () => _ctx.api.getCampaign(campaignName),
        () => null
      );
      if (cfg) _prefillFromConfig(cfg);
    } catch (err) {
      if (err.status === 401) { _ctx.onUnauthorized(); return; }
      _ctx.toast('Could not load campaign config: ' + err.message, 'warning');
    }
  }

  _mountWizard();
}

function _prefillFromConfig(cfg) {
  _wiz.name    = cfg.name    || _wiz.name;
  _wiz.enabled = cfg.enabled ?? _wiz.enabled;
  _wiz.mode    = cfg.mode    || 'demo';

  const eng = cfg.engines || {};
  _wiz.engines_clips = eng.clips ?? true;
  _wiz.engines_memes = eng.memes ?? false;

  const src = cfg.sources || {};
  const yt  = src.youtube  || {};
  const tt  = src.tiktok   || {};
  const ig  = src.instagram || {};

  _wiz.yt_search_terms    = yt.search_terms   || [];
  _wiz.yt_channels        = yt.channels       || [];
  _wiz.yt_min_view_count  = yt.min_view_count ?? 20000;
  _wiz.yt_uploaded_within = yt.uploaded_within || 'year';
  _wiz.tt_profiles  = tt.profiles || [];
  _wiz.tt_hashtags  = tt.hashtags || [];
  _wiz.ig_profiles  = ig.profiles || [];

  const rank = cfg.ranking || {};
  _wiz.clip_len_min         = (rank.clip_length || [20, 60])[0];
  _wiz.clip_len_max         = (rank.clip_length || [20, 60])[1];
  _wiz.max_clips_per_source = rank.max_clips_per_source ?? 8;
  _wiz.exhaust_source       = rank.exhaust_source ?? false;
  _wiz.min_score            = rank.min_score ?? 0.6;
  _wiz.ranking_rules        = rank.ranking_rules || DEFAULT_RANKING_RULES;
  _wiz.creative_direction   = cfg.creative_direction || '';

  const tmpl  = cfg.template     || {};
  const capts = tmpl.captions    || {};
  const hook  = tmpl.hook        || {};
  const wm    = tmpl.watermark   || {};
  const badge = tmpl.corner_badge || {};
  const outro = tmpl.outro       || {};

  _wiz.caption_base_color      = capts.base_color      || '#FFFFFF';
  _wiz.caption_highlight_color = capts.highlight_color || '#00E5FF';
  _wiz.caption_outline_color   = capts.outline_color   || '#000000';
  _wiz.caption_outline_px      = capts.outline_px ?? 6;
  _wiz.caption_max_words       = capts.max_words_per_line ?? 4;
  _wiz.caption_position        = capts.position        || 'upper_mid';
  _wiz.hook_enabled            = hook.enabled ?? true;
  _wiz.hook_seconds            = (hook.show_seconds || [0, 8])[1];
  _wiz.watermark_opacity       = wm.opacity  ?? 0.18;
  _wiz.watermark_scale         = wm.scale    ?? 0.5;
  _wiz.watermark_position      = wm.position || 'center';
  _wiz.badge_opacity           = badge.opacity ?? 1.0;
  _wiz.badge_scale             = badge.scale   ?? 0.12;
  _wiz.badge_position          = badge.position || 'top_right';
  _wiz.outro_enabled           = outro.enabled ?? false;
  _wiz.outro_audio             = outro.audio   || 'keep';

  const dest  = cfg.destinations || {};
  const sched = typeof dest.schedule === 'object' ? dest.schedule : {};

  _wiz.postiz_channels    = dest.postiz_channels || [];
  _wiz.posts_per_day      = sched.posts_per_day  ?? 1;
  _wiz.schedule_times     = sched.times          || ['17:00'];
  _wiz.timezone           = sched.timezone       || 'America/New_York';
  _wiz.caption_template   = dest.caption_template || '{hook}\n\nvia @{source_handle}\n{hashtags}';
  _wiz.hashtags           = dest.hashtags         || [];
  _wiz.autopost           = dest.autopost         ?? false;
  _wiz.demo_test_channels = (cfg.demo || {}).test_channels || [];
}

function _mountWizard() {
  document.getElementById('wizard-overlay')?.remove();

  const overlay = document.createElement('div');
  overlay.id = 'wizard-overlay';
  overlay.className = 'wizard-overlay';
  document.body.appendChild(overlay);

  _renderWizardStep(overlay);
}

function _renderWizardStep(overlay) {
  overlay.innerHTML = `
    <div class="wizard-header">
      <div class="wizard-progress" id="wiz-progress"></div>
      <div class="wizard-step-title" id="wiz-title"></div>
      <div class="wizard-step-subtitle" id="wiz-subtitle"></div>
      <button class="wizard-close" id="wiz-close" aria-label="Close wizard">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
             stroke-linecap="round" aria-hidden="true">
          <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </button>
    </div>
    <div class="wizard-body" id="wiz-body"></div>
    <div class="wizard-footer" id="wiz-footer"></div>`;

  const prog = overlay.querySelector('#wiz-progress');
  for (let i = 0; i < TOTAL_STEPS; i++) {
    const dot = document.createElement('div');
    dot.className = 'wizard-progress-seg'
      + (i < _step ? ' done' : i === _step ? ' active' : '');
    prog.appendChild(dot);
  }

  const titles = [
    ['Basics',             'Name, mode, and engines'],
    ['Sources',            'Where do we find content?'],
    ['Ranking & Creative', 'Quality rules and creative direction'],
    ['Look & Feel',        'Captions, overlays, and assets'],
    ['Destinations',       'Where and when to post'],
    ['Review',             'Confirm and create'],
  ];

  overlay.querySelector('#wiz-title').textContent    = titles[_step][0];
  overlay.querySelector('#wiz-subtitle').textContent = titles[_step][1];

  overlay.querySelector('#wiz-close').addEventListener('click', _closeWizard);

  const body = overlay.querySelector('#wiz-body');
  _buildStepBody(_step, body);

  const footer = overlay.querySelector('#wiz-footer');
  if (_step > 0) {
    const back = document.createElement('button');
    back.className = 'btn btn-secondary';
    back.textContent = 'Back';
    back.addEventListener('click', () => { _saveStepToState(); _step--; _renderWizardStep(overlay); });
    footer.appendChild(back);
  }

  const next = document.createElement('button');
  next.className = 'btn btn-primary';
  next.id = 'wiz-next-btn';

  if (_step === TOTAL_STEPS - 1) {
    next.textContent = _editName ? 'Save changes' : 'Create campaign';
    next.addEventListener('click', () => _submitWizard());
  } else {
    next.textContent = 'Next';
    next.addEventListener('click', () => {
      if (!_validateStep(_step)) return;
      _saveStepToState();
      _step++;
      _renderWizardStep(overlay);
    });
  }
  footer.appendChild(next);
}

function _closeWizard() {
  document.getElementById('wizard-overlay')?.remove();
  const container = document.getElementById('view-campaigns');
  if (container) _renderList(container);
}

// ── Step body builders ────────────────────────────────────────────────────────

function _buildStepBody(step, body) {
  switch (step) {
    case 0: _buildStep1(body); break;
    case 1: _buildStep2(body); break;
    case 2: _buildStep3(body); break;
    case 3: _buildStep4(body); break;
    case 4: _buildStep5(body); break;
    case 5: _buildStep6(body); break;
  }
}

// Step 1 — Basics: name, enabled, mode, engines
function _buildStep1(body) {
  body.innerHTML = `
    <div class="form-group">
      <label class="form-label" for="wiz-name">Campaign name *</label>
      <input id="wiz-name" type="text" class="form-control"
        value="${_esc(_wiz.name)}" placeholder="e.g. fitness"
        autocapitalize="none" autocorrect="off">
      <p class="form-hint">Lowercase, no spaces. This becomes the YAML filename.</p>
    </div>

    ${_toggleHtml('wiz-enabled', 'Enabled', 'Process sources and queue clips automatically', _wiz.enabled)}

    <div class="wizard-sub-section" style="margin-top:16px">
      <div class="wizard-sub-section-title">Mode</div>
      <div class="form-group mb-0">
        <label class="form-label" for="wiz-mode">Pipeline mode</label>
        <select id="wiz-mode" class="form-control">
          <option value="demo"       ${_wiz.mode === 'demo'       ? 'selected' : ''}>Demo — everything tagged, optional test destination</option>
          <option value="production" ${_wiz.mode === 'production' ? 'selected' : ''}>Production — posts to live channels</option>
        </select>
        <p class="form-hint">Demo mode runs the full pipeline but tags all output. The label is dashboard-only and never burned into video.</p>
      </div>
    </div>

    <div class="wizard-sub-section">
      <div class="wizard-sub-section-title">Engines</div>
      ${_toggleHtml('wiz-eng-clips', 'Clips engine', 'Discover, rank, and render 9:16 video clips', _wiz.engines_clips)}
      ${_toggleHtml('wiz-eng-memes', 'Memes engine', 'Generate on-brand meme images from style references', _wiz.engines_memes)}
    </div>`;
}

// Step 2 — Sources
function _buildStep2(body) {
  body.innerHTML = `
    <div class="wizard-sub-section">
      <div class="wizard-sub-section-title">YouTube</div>
      <div class="form-group">
        <label class="form-label">Search terms</label>
        <div id="yt-search-tags" class="tag-input-wrap"></div>
        <p class="form-hint">Press Enter or comma to add. E.g. "hypertrophy science explained"</p>
      </div>
      <div class="form-group">
        <label class="form-label">Channel URLs</label>
        <div id="yt-channel-tags" class="tag-input-wrap"></div>
        <p class="form-hint">Full YouTube channel URLs (optional)</p>
      </div>
      <div class="form-row">
        <div class="form-group mb-0">
          <label class="form-label">Min views</label>
          <input id="yt-min-views" type="number" class="form-control"
            value="${_wiz.yt_min_view_count}" min="0" step="1000">
        </div>
        <div class="form-group mb-0">
          <label class="form-label">Uploaded within</label>
          <select id="yt-uploaded" class="form-control">
            ${UPLOADED_WITHIN_OPTS.map((o) =>
              `<option value="${o.value}" ${_wiz.yt_uploaded_within === o.value ? 'selected' : ''}>${o.label}</option>`
            ).join('')}
          </select>
        </div>
      </div>
    </div>

    <div class="wizard-sub-section">
      <div class="wizard-sub-section-title">TikTok</div>
      <div class="form-group">
        <label class="form-label">Profiles</label>
        <div id="tt-profile-tags" class="tag-input-wrap"></div>
        <p class="form-hint">TikTok @handles (without @)</p>
      </div>
      <div class="form-group mb-0">
        <label class="form-label">Hashtags</label>
        <div id="tt-hashtag-tags" class="tag-input-wrap"></div>
        <p class="form-hint">Without #. E.g. fitnesstips</p>
      </div>
    </div>

    <div class="wizard-sub-section">
      <div class="wizard-sub-section-title">Instagram</div>
      <div class="form-group mb-0">
        <label class="form-label">Profiles</label>
        <div id="ig-profile-tags" class="tag-input-wrap"></div>
        <p class="form-hint">Instagram handles (without @)</p>
      </div>
    </div>`;

  _initTagInput('yt-search-tags',  _wiz.yt_search_terms);
  _initTagInput('yt-channel-tags', _wiz.yt_channels);
  _initTagInput('tt-profile-tags', _wiz.tt_profiles);
  _initTagInput('tt-hashtag-tags', _wiz.tt_hashtags);
  _initTagInput('ig-profile-tags', _wiz.ig_profiles);
}

// Step 3 — Ranking & Creative Direction
function _buildStep3(body) {
  body.innerHTML = `
    <div class="wizard-sub-section">
      <div class="wizard-sub-section-title">Clip rules</div>
      <div class="form-group">
        <label class="form-label">Clip length (seconds)</label>
        <div class="form-row">
          <div>
            <label class="form-label">Min</label>
            <input id="wiz-clip-min" type="number" class="form-control"
              value="${_wiz.clip_len_min}" min="5" max="300">
          </div>
          <div>
            <label class="form-label">Max</label>
            <input id="wiz-clip-max" type="number" class="form-control"
              value="${_wiz.clip_len_max}" min="5" max="300">
          </div>
        </div>
      </div>
      <div class="form-group">
        <label class="form-label">Max clips per source</label>
        <input id="wiz-max-clips" type="number" class="form-control"
          value="${_wiz.max_clips_per_source}" min="1" max="100">
        <p class="form-hint">Conservative copyright default: 8. Raise only if you have permission.</p>
      </div>
      <div class="form-group">
        ${_toggleHtml('wiz-exhaust', 'Exhaust source', 'Extract ALL possible clips from each video', _wiz.exhaust_source)}
        <div class="warning-note">
          ⚠ Only use with permission from the original creator.
        </div>
      </div>
      <div class="form-group">
        <label class="form-label">Minimum score (0–1)</label>
        <div style="display:flex;align-items:center;gap:12px;">
          <input id="wiz-min-score" type="range" min="0" max="1" step="0.05"
            value="${_wiz.min_score}" style="flex:1">
          <span id="wiz-min-score-val" style="min-width:32px;font-size:14px;color:var(--accent)">
            ${_wiz.min_score.toFixed(2)}
          </span>
        </div>
      </div>
      <div class="form-group mb-0">
        <label class="form-label">Ranking rules</label>
        <textarea id="wiz-ranking-rules" class="form-control" rows="7"
          style="min-height:140px;font-size:13px">${_esc(_wiz.ranking_rules)}</textarea>
        <p class="form-hint">Passed verbatim to the LLM. Defines what makes a good clip and what to exclude.</p>
      </div>
    </div>

    <div class="wizard-sub-section">
      <div class="wizard-sub-section-title">Creative direction</div>
      <div class="form-group mb-0">
        <label class="form-label">Free-text creative brief</label>
        <textarea id="wiz-creative" class="form-control" rows="5"
          placeholder="Describe the look, feel, and voice. E.g. 'Science-forward, calm and authoritative, no hype. Show the mechanism, not the result.'">${_esc(_wiz.creative_direction)}</textarea>
        <p class="form-hint">Fed to both the ranker (what to look for) and the render pipeline (visual guidance). Ignored if empty.</p>
      </div>
    </div>`;

  body.querySelector('#wiz-min-score').addEventListener('input', (e) => {
    body.querySelector('#wiz-min-score-val').textContent = parseFloat(e.target.value).toFixed(2);
  });
}

// Step 4 — Look & Feel: captions, hook, watermark, assets (incl. meme refs)
function _buildStep4(body) {
  const memesOn = _wiz.engines_memes;

  body.innerHTML = `
    <div class="wizard-sub-section">
      <div class="wizard-sub-section-title">Captions</div>
      <div class="form-row">
        <div class="form-group mb-0">
          <label class="form-label">Base color</label>
          <input id="wiz-cap-base" type="color" class="form-control" value="${_wiz.caption_base_color}">
        </div>
        <div class="form-group mb-0">
          <label class="form-label">Highlight color</label>
          <input id="wiz-cap-hi" type="color" class="form-control" value="${_wiz.caption_highlight_color}">
        </div>
      </div>
      <div class="form-row" style="margin-top:12px">
        <div class="form-group mb-0">
          <label class="form-label">Outline color</label>
          <input id="wiz-cap-outline" type="color" class="form-control" value="${_wiz.caption_outline_color}">
        </div>
        <div class="form-group mb-0">
          <label class="form-label">Outline px</label>
          <input id="wiz-cap-outline-px" type="number" class="form-control"
            value="${_wiz.caption_outline_px}" min="0" max="20">
        </div>
      </div>
      <div class="form-row" style="margin-top:12px">
        <div class="form-group mb-0">
          <label class="form-label">Max words / line</label>
          <input id="wiz-cap-maxwords" type="number" class="form-control"
            value="${_wiz.caption_max_words}" min="1" max="10">
        </div>
        <div class="form-group mb-0">
          <label class="form-label">Position</label>
          <select id="wiz-cap-pos" class="form-control">
            ${POSITION_OPTS_CAPTION.map((o) =>
              `<option value="${o.value}" ${_wiz.caption_position === o.value ? 'selected' : ''}>${o.label}</option>`
            ).join('')}
          </select>
        </div>
      </div>
    </div>

    <div class="wizard-sub-section">
      <div class="wizard-sub-section-title">Hook overlay</div>
      ${_toggleHtml('wiz-hook-enabled', 'Show hook text', 'Display the first-2s hook as a bold overlay', _wiz.hook_enabled)}
      <div class="form-group mt-12 mb-0">
        <label class="form-label">Show for (seconds)</label>
        <input id="wiz-hook-secs" type="number" class="form-control"
          value="${_wiz.hook_seconds}" min="1" max="30">
        <p class="form-hint">Hook overlay appears from 0 s to this many seconds.</p>
      </div>
    </div>

    <div class="wizard-sub-section">
      <div class="wizard-sub-section-title">Watermark</div>
      <div class="form-row">
        <div class="form-group mb-0">
          <label class="form-label">Opacity (0–1)</label>
          <input id="wiz-wm-opacity" type="number" class="form-control"
            value="${_wiz.watermark_opacity}" min="0" max="1" step="0.01">
        </div>
        <div class="form-group mb-0">
          <label class="form-label">Scale (0–1)</label>
          <input id="wiz-wm-scale" type="number" class="form-control"
            value="${_wiz.watermark_scale}" min="0.01" max="1" step="0.01">
        </div>
      </div>
      <div class="form-group mt-12 mb-0">
        <label class="form-label">Position</label>
        <select id="wiz-wm-pos" class="form-control">
          ${POSITION_OPTS_OVERLAY.map((o) =>
            `<option value="${o.value}" ${_wiz.watermark_position === o.value ? 'selected' : ''}>${o.label}</option>`
          ).join('')}
        </select>
      </div>
    </div>

    <div class="wizard-sub-section">
      <div class="wizard-sub-section-title">Corner badge</div>
      <div class="form-row">
        <div class="form-group mb-0">
          <label class="form-label">Opacity</label>
          <input id="wiz-badge-opacity" type="number" class="form-control"
            value="${_wiz.badge_opacity}" min="0" max="1" step="0.01">
        </div>
        <div class="form-group mb-0">
          <label class="form-label">Scale</label>
          <input id="wiz-badge-scale" type="number" class="form-control"
            value="${_wiz.badge_scale}" min="0.01" max="1" step="0.01">
        </div>
      </div>
      <div class="form-group mt-12 mb-0">
        <label class="form-label">Position</label>
        <select id="wiz-badge-pos" class="form-control">
          ${POSITION_OPTS_OVERLAY.map((o) =>
            `<option value="${o.value}" ${_wiz.badge_position === o.value ? 'selected' : ''}>${o.label}</option>`
          ).join('')}
        </select>
      </div>
    </div>

    <div class="wizard-sub-section">
      <div class="wizard-sub-section-title">Outro</div>
      ${_toggleHtml('wiz-outro-enabled', 'Append outro clip', 'Concat a branded outro after every clip', _wiz.outro_enabled)}
      <div class="form-group mt-12 mb-0">
        <label class="form-label">Outro audio</label>
        <select id="wiz-outro-audio" class="form-control">
          <option value="keep" ${_wiz.outro_audio === 'keep' ? 'selected' : ''}>Keep original audio</option>
          <option value="mute" ${_wiz.outro_audio === 'mute' ? 'selected' : ''}>Mute outro audio</option>
        </select>
      </div>
    </div>

    <div class="wizard-sub-section">
      <div class="wizard-sub-section-title">Assets</div>
      <div class="form-group">
        <label class="form-label">Logo (watermark) *</label>
        ${_fileUploadHtml('file-logo', 'logo', ['image/png','image/jpeg','image/webp'], _files.logo?.name)}
      </div>
      <div class="form-group">
        <label class="form-label">Corner badge (opt)</label>
        ${_fileUploadHtml('file-badge', 'corner_badge', ['image/png','image/jpeg','image/webp'], _files.corner_badge?.name)}
      </div>
      <div class="form-group">
        <label class="form-label">Outro video (opt)</label>
        ${_fileUploadHtml('file-outro', 'outro', ['video/mp4','video/quicktime','video/webm'], _files.outro?.name)}
      </div>
      <div class="form-group">
        <label class="form-label">Font file (opt)</label>
        ${_fileUploadHtml('file-font', 'font', ['.ttf','.otf','font/ttf','font/otf'], _files.font?.name)}
        <p class="form-hint">If omitted, the system default font is used.</p>
      </div>
      <div class="form-group">
        <label class="form-label">Visual reference images (opt)</label>
        ${_multiFileUploadHtml('file-visual-refs', 'visual_refs', ['image/png','image/jpeg','image/webp'], _files.visual_refs)}
        <p class="form-hint">Images showing your desired visual look — passed to the ranker and renderer as guidance. Up to 5.</p>
      </div>
      ${memesOn ? `
      <div class="form-group mb-0">
        <label class="form-label">Meme reference images *</label>
        ${_multiFileUploadHtml('file-meme-refs', 'meme_refs', ['image/png','image/jpeg','image/webp'], _files.meme_refs)}
        <p class="form-hint">Example memes in your desired style. Required when Memes engine is on. Up to 10.</p>
      </div>` : ''}
    </div>`;

  // Wire single-file inputs
  [
    ['file-logo',  'logo'],
    ['file-badge', 'corner_badge'],
    ['file-outro', 'outro'],
    ['file-font',  'font'],
  ].forEach(([domId, key]) => {
    const inp = body.querySelector(`#${domId}`);
    if (!inp) return;
    inp.addEventListener('change', (e) => {
      const file = e.target.files[0];
      if (!file) return;
      _files[key] = file;
      const preview = inp.closest('.file-upload')?.querySelector('.file-upload-preview');
      if (preview) {
        preview.textContent = file.name;
        if (file.type.startsWith('image/')) {
          const img = document.createElement('img');
          img.src = URL.createObjectURL(file);
          img.onload = () => URL.revokeObjectURL(img.src);
          preview.appendChild(img);
        }
      }
    });
  });

  // Wire multi-file inputs
  [
    ['file-visual-refs', 'visual_refs', 5],
    ['file-meme-refs',   'meme_refs',   10],
  ].forEach(([domId, key, maxCount]) => {
    const inp = body.querySelector(`#${domId}`);
    if (!inp) return;
    inp.addEventListener('change', (e) => {
      const newFiles = Array.from(e.target.files).slice(0, maxCount);
      _files[key] = newFiles;
      const preview = inp.closest('.file-upload')?.querySelector('.file-upload-preview');
      if (preview) {
        preview.textContent = newFiles.length ? `${newFiles.length} file(s): ${newFiles.map(f => f.name).join(', ')}` : '';
      }
    });
  });
}

// Step 5 — Destinations
function _buildStep5(body) {
  const isDemo = _wiz.mode === 'demo';

  body.innerHTML = `
    <div class="form-group">
      <label class="form-label">Postiz channel names *</label>
      <div id="dest-channels-tags" class="tag-input-wrap"></div>
      <p class="form-hint">Channel names as configured in Postiz Settings → Providers.</p>
    </div>

    <div class="form-row">
      <div class="form-group">
        <label class="form-label">Posts per day</label>
        <input id="wiz-ppd" type="number" class="form-control"
          value="${_wiz.posts_per_day}" min="1" max="24">
      </div>
      <div class="form-group">
        <label class="form-label">Timezone</label>
        <select id="wiz-tz" class="form-control">
          ${TZ_OPTS.map((tz) =>
            `<option value="${tz}" ${_wiz.timezone === tz ? 'selected' : ''}>${tz}</option>`
          ).join('')}
        </select>
      </div>
    </div>

    <div class="form-group">
      <label class="form-label">Post times (HH:MM)</label>
      <div id="dest-times-tags" class="tag-input-wrap"></div>
      <p class="form-hint">24-hour format. Press Enter to add. E.g. 17:00</p>
    </div>

    <div class="form-group">
      <label class="form-label">Caption template</label>
      <textarea id="wiz-caption-tmpl" class="form-control" rows="5">${_esc(_wiz.caption_template)}</textarea>
      <p class="form-hint">Variables: {hook} {source_handle} {hashtags}</p>
    </div>

    <div class="form-group">
      <label class="form-label">Hashtags</label>
      <div id="dest-hashtag-tags" class="tag-input-wrap"></div>
      <p class="form-hint">Include the # prefix. E.g. #fitness</p>
    </div>

    <div class="form-group">
      ${_toggleHtml('wiz-autopost', 'Autopost', 'Post automatically without manual approval in Postiz', _wiz.autopost)}
      <div class="warning-note" style="margin-top:6px">
        <strong>Drafts only — recommended.</strong> When off, clips land as Postiz drafts.
      </div>
    </div>

    ${isDemo ? `
    <div class="wizard-sub-section">
      <div class="wizard-sub-section-title">Demo test channels</div>
      <div class="form-group mb-0">
        <label class="form-label">Test destination channels (opt)</label>
        <div id="dest-demo-channels-tags" class="tag-input-wrap"></div>
        <p class="form-hint">Postiz channel IDs used when posting demo items. Leave empty to use the live channels above.</p>
      </div>
    </div>` : ''}`;

  _initTagInput('dest-channels-tags',    _wiz.postiz_channels);
  _initTagInput('dest-times-tags',       _wiz.schedule_times);
  _initTagInput('dest-hashtag-tags',     _wiz.hashtags);
  if (isDemo) _initTagInput('dest-demo-channels-tags', _wiz.demo_test_channels);
}

// Step 6 — Review
function _buildStep6(body) {
  body.innerHTML = `
    <div class="review-block">
      <div class="review-block-title">Basics</div>
      <div class="review-kv">
        <div><span class="k">Name:</span><span class="v">${_esc(_wiz.name)}</span></div>
        <div><span class="k">Enabled:</span><span class="v">${_wiz.enabled ? 'Yes' : 'No'}</span></div>
        <div><span class="k">Mode:</span><span class="v">${_wiz.mode === 'demo' ? '🟡 Demo' : '🟢 Production'}</span></div>
        <div><span class="k">Engines:</span><span class="v">${[_wiz.engines_clips ? 'Clips' : null, _wiz.engines_memes ? 'Memes' : null].filter(Boolean).join(', ') || 'None'}</span></div>
      </div>
    </div>

    <div class="review-block">
      <div class="review-block-title">Sources</div>
      <div class="review-kv">
        <div><span class="k">YouTube terms:</span><span class="v">${_esc(_wiz.yt_search_terms.join(', ') || '—')}</span></div>
        <div><span class="k">YT channels:</span><span class="v">${_esc(_wiz.yt_channels.join(', ') || '—')}</span></div>
        <div><span class="k">TikTok profiles:</span><span class="v">${_esc(_wiz.tt_profiles.join(', ') || '—')}</span></div>
        <div><span class="k">Instagram:</span><span class="v">${_esc(_wiz.ig_profiles.join(', ') || '—')}</span></div>
      </div>
    </div>

    <div class="review-block">
      <div class="review-block-title">Ranking</div>
      <div class="review-kv">
        <div><span class="k">Clip length:</span><span class="v">${_wiz.clip_len_min}–${_wiz.clip_len_max} s</span></div>
        <div><span class="k">Max clips/source:</span><span class="v">${_wiz.max_clips_per_source}</span></div>
        <div><span class="k">Exhaust source:</span><span class="v">${_wiz.exhaust_source ? '⚠ Yes' : 'No'}</span></div>
        <div><span class="k">Min score:</span><span class="v">${_wiz.min_score}</span></div>
        ${_wiz.creative_direction ? `<div><span class="k">Creative direction:</span><span class="v">${_esc(_wiz.creative_direction.slice(0, 80))}${_wiz.creative_direction.length > 80 ? '…' : ''}</span></div>` : ''}
      </div>
    </div>

    <div class="review-block">
      <div class="review-block-title">Look & Feel</div>
      <div class="review-kv">
        <div><span class="k">Caption base:</span><span class="v">${_esc(_wiz.caption_base_color)}</span></div>
        <div><span class="k">Highlight:</span><span class="v">${_esc(_wiz.caption_highlight_color)}</span></div>
        <div><span class="k">Position:</span><span class="v">${_esc(_wiz.caption_position)}</span></div>
        <div><span class="k">Hook:</span><span class="v">${_wiz.hook_enabled ? `On, ${_wiz.hook_seconds}s` : 'Off'}</span></div>
        <div><span class="k">Watermark opacity:</span><span class="v">${_wiz.watermark_opacity}</span></div>
        <div><span class="k">Outro:</span><span class="v">${_wiz.outro_enabled ? 'On' : 'Off'}</span></div>
        <div><span class="k">Logo:</span><span class="v">${_files.logo ? _esc(_files.logo.name) : '(existing or not set)'}</span></div>
        ${_files.visual_refs.length > 0 ? `<div><span class="k">Visual refs:</span><span class="v">${_files.visual_refs.length} file(s)</span></div>` : ''}
        ${_files.meme_refs.length > 0 ? `<div><span class="k">Meme refs:</span><span class="v">${_files.meme_refs.length} file(s)</span></div>` : ''}
      </div>
    </div>

    <div class="review-block">
      <div class="review-block-title">Destinations</div>
      <div class="review-kv">
        <div><span class="k">Channels:</span><span class="v">${_esc(_wiz.postiz_channels.join(', ') || '—')}</span></div>
        <div><span class="k">Schedule:</span><span class="v">${_wiz.posts_per_day}/day at ${_wiz.schedule_times.join(', ')} (${_esc(_wiz.timezone)})</span></div>
        <div><span class="k">Autopost:</span><span class="v">${_wiz.autopost ? '⚠ Yes' : 'No — drafts only'}</span></div>
        <div><span class="k">Hashtags:</span><span class="v">${_esc(_wiz.hashtags.join(' ') || '—')}</span></div>
        ${_wiz.demo_test_channels.length ? `<div><span class="k">Demo channels:</span><span class="v">${_esc(_wiz.demo_test_channels.join(', '))}</span></div>` : ''}
      </div>
    </div>`;
}

// ── State save / validate ─────────────────────────────────────────────────────

function _saveStepToState() {
  const $ = (id) => document.getElementById(id);
  const val = (id) => $(`${id}`)?.value ?? '';
  const tog = (id) => !!$(`${id}`)?.checked;
  const tags = (id) => _readTagInput(id);

  switch (_step) {
    case 0:
      _wiz.name          = val('wiz-name').trim().toLowerCase().replace(/\s+/g, '-');
      _wiz.enabled       = tog('wiz-enabled');
      _wiz.mode          = val('wiz-mode') || 'demo';
      _wiz.engines_clips = tog('wiz-eng-clips');
      _wiz.engines_memes = tog('wiz-eng-memes');
      break;
    case 1:
      _wiz.yt_search_terms    = tags('yt-search-tags');
      _wiz.yt_channels        = tags('yt-channel-tags');
      _wiz.yt_min_view_count  = parseInt(val('yt-min-views'), 10) || 0;
      _wiz.yt_uploaded_within = val('yt-uploaded');
      _wiz.tt_profiles        = tags('tt-profile-tags');
      _wiz.tt_hashtags        = tags('tt-hashtag-tags');
      _wiz.ig_profiles        = tags('ig-profile-tags');
      break;
    case 2:
      _wiz.clip_len_min         = parseInt(val('wiz-clip-min'), 10) || 20;
      _wiz.clip_len_max         = parseInt(val('wiz-clip-max'), 10) || 60;
      _wiz.max_clips_per_source = parseInt(val('wiz-max-clips'), 10) || 8;
      _wiz.exhaust_source       = tog('wiz-exhaust');
      _wiz.min_score            = parseFloat(val('wiz-min-score')) || 0.6;
      _wiz.ranking_rules        = val('wiz-ranking-rules');
      _wiz.creative_direction   = val('wiz-creative');
      break;
    case 3:
      _wiz.caption_base_color      = val('wiz-cap-base');
      _wiz.caption_highlight_color = val('wiz-cap-hi');
      _wiz.caption_outline_color   = val('wiz-cap-outline');
      _wiz.caption_outline_px      = parseInt(val('wiz-cap-outline-px'), 10) || 6;
      _wiz.caption_max_words       = parseInt(val('wiz-cap-maxwords'), 10) || 4;
      _wiz.caption_position        = val('wiz-cap-pos');
      _wiz.hook_enabled            = tog('wiz-hook-enabled');
      _wiz.hook_seconds            = parseInt(val('wiz-hook-secs'), 10) || 8;
      _wiz.watermark_opacity       = parseFloat(val('wiz-wm-opacity'))    || 0.18;
      _wiz.watermark_scale         = parseFloat(val('wiz-wm-scale'))      || 0.5;
      _wiz.watermark_position      = val('wiz-wm-pos');
      _wiz.badge_opacity           = parseFloat(val('wiz-badge-opacity')) || 1.0;
      _wiz.badge_scale             = parseFloat(val('wiz-badge-scale'))   || 0.12;
      _wiz.badge_position          = val('wiz-badge-pos');
      _wiz.outro_enabled           = tog('wiz-outro-enabled');
      _wiz.outro_audio             = val('wiz-outro-audio');
      break;
    case 4:
      _wiz.postiz_channels    = tags('dest-channels-tags');
      _wiz.posts_per_day      = parseInt(val('wiz-ppd'), 10) || 1;
      _wiz.schedule_times     = tags('dest-times-tags');
      _wiz.timezone           = val('wiz-tz');
      _wiz.caption_template   = val('wiz-caption-tmpl');
      _wiz.hashtags           = tags('dest-hashtag-tags');
      _wiz.autopost           = tog('wiz-autopost');
      _wiz.demo_test_channels = tags('dest-demo-channels-tags');
      break;
  }
}

function _validateStep(step) {
  if (step === 0) {
    const name = document.getElementById('wiz-name')?.value?.trim();
    if (!name) { _ctx.toast('Campaign name is required', 'error'); return false; }
    if (!/^[a-z0-9_-]+$/i.test(name)) {
      _ctx.toast('Name must be lowercase letters, numbers, hyphens, or underscores', 'error');
      return false;
    }
  }
  if (step === 4) {
    const channels = _readTagInput('dest-channels-tags');
    if (channels.length === 0) { _ctx.toast('Add at least one Postiz channel', 'error'); return false; }
  }
  return true;
}

// ── Submit ────────────────────────────────────────────────────────────────────

async function _submitWizard() {
  _saveStepToState();

  const config = {
    name:    _wiz.name,
    enabled: _wiz.enabled,
    mode:    _wiz.mode,
    engines: {
      clips: _wiz.engines_clips,
      memes: _wiz.engines_memes,
    },
    creative_direction: _wiz.creative_direction || '',
    sources: {
      youtube: {
        search_terms:    _wiz.yt_search_terms,
        channels:        _wiz.yt_channels,
        min_view_count:  _wiz.yt_min_view_count,
        uploaded_within: _wiz.yt_uploaded_within,
      },
      tiktok:    { profiles: _wiz.tt_profiles, hashtags: _wiz.tt_hashtags },
      instagram: { profiles: _wiz.ig_profiles },
    },
    ranking: {
      clip_length:          [_wiz.clip_len_min, _wiz.clip_len_max],
      max_clips_per_source: _wiz.max_clips_per_source,
      exhaust_source:       _wiz.exhaust_source,
      min_score:            _wiz.min_score,
      ranking_rules:        _wiz.ranking_rules,
    },
    template: {
      aspect:     '9:16',
      resolution: [1080, 1920],
      captions: {
        style:              'word_by_word',
        base_color:         _wiz.caption_base_color,
        highlight_color:    _wiz.caption_highlight_color,
        outline_color:      _wiz.caption_outline_color,
        outline_px:         _wiz.caption_outline_px,
        position:           _wiz.caption_position,
        max_words_per_line: _wiz.caption_max_words,
      },
      hook: {
        enabled:      _wiz.hook_enabled,
        show_seconds: [0, _wiz.hook_seconds],
        source:       'ranking',
      },
      lower_third: { show_source_handle: true, format: 'via @{source_handle}' },
      watermark:    { position: _wiz.watermark_position, opacity: _wiz.watermark_opacity, scale: _wiz.watermark_scale },
      corner_badge: { position: _wiz.badge_position, opacity: _wiz.badge_opacity, scale: _wiz.badge_scale },
      outro:        { enabled: _wiz.outro_enabled, audio: _wiz.outro_audio },
    },
    destinations: {
      postiz_channels:  _wiz.postiz_channels,
      schedule: {
        posts_per_day: _wiz.posts_per_day,
        times:         _wiz.schedule_times,
        timezone:      _wiz.timezone,
      },
      caption_template: _wiz.caption_template,
      hashtags:         _wiz.hashtags,
      autopost:         _wiz.autopost,
    },
    demo: {
      test_channels: _wiz.demo_test_channels,
    },
  };

  const fd = new FormData();
  fd.append('config', JSON.stringify(config));
  if (_files.logo)         fd.append('logo',         _files.logo,         _files.logo.name);
  if (_files.corner_badge) fd.append('corner_badge', _files.corner_badge, _files.corner_badge.name);
  if (_files.outro)        fd.append('outro',        _files.outro,        _files.outro.name);
  if (_files.font)         fd.append('font',         _files.font,         _files.font.name);

  // Multiple visual refs (repeated field name — FastAPI collects list[UploadFile])
  _files.visual_refs.forEach((f) => fd.append('visual_refs', f, f.name));
  // Multiple meme refs (repeated field name — FastAPI collects list[UploadFile])
  _files.meme_refs.forEach((f) => fd.append('meme_refs', f, f.name));

  const btn = document.getElementById('wiz-next-btn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>'; }

  try {
    await _ctx.mockFetch(
      () => _editName
        ? _ctx.api.updateCampaign(_editName, fd)
        : _ctx.api.createCampaign(fd),
      () => config
    );
    _ctx.toast(_editName ? 'Campaign updated' : 'Campaign created', 'success');
    _closeWizard();
  } catch (err) {
    if (btn) { btn.disabled = false; btn.textContent = _editName ? 'Save changes' : 'Create campaign'; }
    if (err.status === 401) { _ctx.onUnauthorized(); return; }
    _ctx.toast('Failed: ' + err.message, 'error');
  }
}

// ── Tag input helpers ─────────────────────────────────────────────────────────

function _initTagInput(containerId, initialValues) {
  const wrap = document.getElementById(containerId);
  if (!wrap) return;

  wrap.dataset.tags = JSON.stringify(initialValues || []);
  wrap.innerHTML = '';

  const inp = document.createElement('input');
  inp.type = 'text';
  inp.className = 'tag-text-input';
  inp.placeholder = 'Type and press Enter…';
  inp.setAttribute('aria-label', 'Add tag');
  wrap.appendChild(inp);

  const addPills = (tags) => {
    wrap.querySelectorAll('.tag-pill').forEach((p) => p.remove());
    tags.forEach((tag, idx) => {
      const pill = document.createElement('span');
      pill.className = 'tag-pill';
      pill.innerHTML = `
        <span class="tag-pill-label" title="${_esc(tag)}">${_esc(tag)}</span>
        <button class="tag-pill-remove" data-idx="${idx}" aria-label="Remove ${_esc(tag)}">×</button>`;
      pill.querySelector('.tag-pill-remove').addEventListener('click', (e) => {
        const i = parseInt(e.currentTarget.dataset.idx, 10);
        const cur = JSON.parse(wrap.dataset.tags);
        cur.splice(i, 1);
        wrap.dataset.tags = JSON.stringify(cur);
        addPills(cur);
        inp.focus();
      });
      wrap.insertBefore(pill, inp);
    });
  };

  const commit = () => {
    const raw = inp.value.trim().replace(/,$/, '').trim();
    if (!raw) return;
    const cur = JSON.parse(wrap.dataset.tags || '[]');
    if (!cur.includes(raw)) {
      cur.push(raw);
      wrap.dataset.tags = JSON.stringify(cur);
      addPills(cur);
    }
    inp.value = '';
  };

  inp.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); commit(); }
    if (e.key === 'Backspace' && inp.value === '') {
      const cur = JSON.parse(wrap.dataset.tags || '[]');
      if (cur.length > 0) {
        cur.pop();
        wrap.dataset.tags = JSON.stringify(cur);
        addPills(cur);
      }
    }
  });
  inp.addEventListener('blur', commit);
  wrap.addEventListener('click', (e) => { if (e.target === wrap) inp.focus(); });

  addPills(initialValues || []);
}

function _readTagInput(containerId) {
  const wrap = document.getElementById(containerId);
  if (!wrap) return [];
  const inp = wrap.querySelector('.tag-text-input');
  if (inp?.value?.trim()) {
    const raw = inp.value.trim().replace(/,$/, '').trim();
    if (raw) {
      const cur = JSON.parse(wrap.dataset.tags || '[]');
      if (!cur.includes(raw)) cur.push(raw);
      wrap.dataset.tags = JSON.stringify(cur);
      inp.value = '';
    }
  }
  try { return JSON.parse(wrap.dataset.tags || '[]'); }
  catch { return []; }
}

// ── Component helpers ─────────────────────────────────────────────────────────

function _toggleHtml(id, label, sublabel, checked) {
  return `
    <label class="toggle-row" for="${id}">
      <div class="toggle-text">
        <div class="toggle-label-text">${label}</div>
        ${sublabel ? `<div class="toggle-sublabel">${sublabel}</div>` : ''}
      </div>
      <div class="toggle">
        <input type="checkbox" id="${id}" ${checked ? 'checked' : ''}>
        <div class="toggle-track"></div>
        <div class="toggle-knob"></div>
      </div>
    </label>`;
}

function _fileUploadHtml(domId, key, accept, existingName) {
  return `
    <div class="file-upload">
      <input type="file" id="${domId}" accept="${accept.join(',')}" aria-label="Upload ${key}">
      <div class="file-upload-label"><strong>Choose file</strong> or drag here</div>
      <div class="file-upload-preview">${existingName ? _esc(existingName) : ''}</div>
    </div>`;
}

function _multiFileUploadHtml(domId, key, accept, existingFiles) {
  const existingLabel = existingFiles && existingFiles.length
    ? `${existingFiles.length} file(s) selected`
    : '';
  return `
    <div class="file-upload">
      <input type="file" id="${domId}" accept="${accept.join(',')}" multiple aria-label="Upload ${key}">
      <div class="file-upload-label"><strong>Choose files</strong> or drag here (multiple)</div>
      <div class="file-upload-preview">${_esc(existingLabel)}</div>
    </div>`;
}

function _skeletonList() {
  const s = () => `
    <div class="campaign-card" style="pointer-events:none" aria-hidden="true">
      <div class="campaign-header">
        <div class="skeleton" style="width:8px;height:8px;border-radius:50%;flex-shrink:0"></div>
        <div class="skeleton" style="height:18px;width:130px;"></div>
      </div>
      <div class="skeleton" style="height:14px;width:70%;margin-top:8px;"></div>
      <div class="skeleton" style="height:12px;width:50%;margin-top:6px;"></div>
    </div>`;
  return s() + s();
}

function _esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
