/* Smart Heating Controller — web app.
 * Plain JavaScript, no build step. Talks to the Flask server's JSON API.
 */
(function () {
  'use strict';

  // ---- tiny helpers ----------------------------------------------------------------
  const $ = (sel, el) => (el || document).querySelector(sel);
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ESC[c]);

  // html`` escapes every value unless it is itself html`` output (or an array of them).
  class Safe { constructor(s) { this.s = s; } toString() { return this.s; } }
  const part = (v) => (v instanceof Safe ? v.s : v === false || v === null || v === undefined ? '' : esc(v));
  function html(strings, ...vals) {
    let out = '';
    strings.forEach((s, i) => {
      out += s;
      if (i < vals.length) {
        const v = vals[i];
        out += Array.isArray(v) ? v.map(part).join('') : part(v);
      }
    });
    return new Safe(out);
  }

  const fmtNum = (n, digits = 1) => (n === null || n === undefined ? '—' : Number(n).toFixed(digits).replace(/\.0+$/, ''));
  const fmtTemp = (n) => (n === null || n === undefined ? '—' : `${fmtNum(n)}°`);
  const DAY_LETTERS = ['M', 'T', 'W', 'T', 'F', 'S', 'S'];
  const DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

  const ICON = {
    minus: html`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14"/></svg>`,
    plus: html`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M12 5v14"/></svg>`,
    check: html`<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l5 5 9-10"/></svg>`,
    arrow: html`<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>`,
    trash: html`<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>`,
  };

  // ---- app state -------------------------------------------------------------------
  const S = {
    route: 'overview',
    tz: undefined,
    state: null,       // /api/state
    pending: null,     // target temperature staged with −/+ but not yet applied
    schedule: null,    // /api/schedule
    draft: null,       // periods being edited
    dirty: false,
    chartDay: 0,       // 0 = today, 1 = tomorrow
    energy: null,
    month: null,
    reportFmt: 'csv',
    devices: null,
    calDraft: null,
    calDirty: false,
    revealToken: false,
    pollTimer: null,
  };

  // ---- time in the home's timezone -------------------------------------------------------
  function timeFmt(iso) {
    if (!iso) return '—';
    return new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit', timeZone: S.tz }).format(new Date(iso));
  }
  function hourFmt(iso) {
    return new Intl.DateTimeFormat('en-US', { hour: 'numeric', timeZone: S.tz }).format(new Date(iso));
  }
  function dateFmt(iso) {
    return new Intl.DateTimeFormat('en-US', { weekday: 'short', month: 'short', day: 'numeric', timeZone: S.tz }).format(new Date(iso));
  }
  function relTime(iso) {
    const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins} min ago`;
    if (mins < 60 * 24) return timeFmt(iso);
    return dateFmt(iso);
  }
  // Local date/minute/weekday of an ISO time as seen in the home's timezone.
  function homeParts(iso) {
    const parts = {};
    new Intl.DateTimeFormat('en-CA', {
      timeZone: S.tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).formatToParts(new Date(iso)).forEach((p) => { parts[p.type] = p.value; });
    const date = `${parts.year}-${parts.month}-${parts.day}`;
    return { date, minute: Number(parts.hour) * 60 + Number(parts.minute), weekday: weekdayOf(date) };
  }
  function weekdayOf(date) {
    const [y, m, d] = date.split('-').map(Number);
    return (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7; // Monday = 0
  }
  function addDays(date, n) {
    const [y, m, d] = date.split('-').map(Number);
    return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
  }
  const hm = (s) => { const [h, m] = s.split(':').map(Number); return h * 60 + m; };

  // ---- server calls --------------------------------------------------------------------
  async function api(path, opts = {}) {
    const init = { method: opts.method || 'GET', credentials: 'same-origin', headers: { 'X-Requested-With': 'shc' } };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    if (opts.form) init.body = opts.form;
    let res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      setOffline(true);
      throw new Error("Can't reach the server.");
    }
    setOffline(false);
    if (res.status === 401 && path !== '/api/login') {
      showLogin();
      throw new Error('Please sign in.');
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || 'Something went wrong. Try again.');
    return data;
  }

  function setOffline(off) { $('#offline-banner').hidden = !off; }

  let toastTimer;
  function toast(msg, isErr) {
    const el = $('#toast');
    el.textContent = msg;
    el.className = isErr ? 'toast err' : 'toast';
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, isErr ? 6000 : 3000);
  }

  async function run(fn) {
    try { await fn(); } catch (e) { toast(e.message, true); }
  }

  // ---- sign in -------------------------------------------------------------------------
  function showLogin() {
    stopPolling();
    $('#topbar').hidden = true;
    $('#tabbar').hidden = true;
    $('#app').innerHTML = html`
      <form class="card login" id="login-form">
        <div class="brand"><span class="brand-mark" aria-hidden="true"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#F59E5B" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3c1 3.5 5 5.5 5 10a5 5 0 0 1-10 0c0-2.2 1-3.6 2.2-4.8.3 1.6 1.1 2.6 2.3 2.8C11 9 11 6 12 3z"/></svg></span>Smart Heating</div>
        <h1>Sign in</h1>
        <p class="muted">Control your heating from anywhere.</p>
        <label class="field">Password
          <input class="input" type="password" name="password" autocomplete="current-password" required autofocus>
        </label>
        <p class="note err" id="login-error" hidden></p>
        <button class="btn btn-dark" type="submit">Sign in</button>
      </form>`.s;
    $('#login-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const errEl = $('#login-error');
      try {
        await api('/api/login', { method: 'POST', body: { password: ev.target.password.value } });
        start();
      } catch (e) {
        errEl.textContent = e.message;
        errEl.hidden = false;
      }
    });
  }

  // ---- routing & polling -----------------------------------------------------------------
  const ROUTES = ['overview', 'schedule', 'energy', 'devices'];

  function currentRoute() {
    const r = location.hash.replace(/^#\/?/, '');
    return ROUTES.includes(r) ? r : 'overview';
  }

  async function onRoute() {
    if (S.route === 'schedule' && S.dirty && currentRoute() !== 'schedule') {
      if (!confirm('You have unsaved schedule changes. Leave without saving?')) {
        location.hash = '#/schedule';
        return;
      }
      S.dirty = false;
    }
    S.route = currentRoute();
    document.querySelectorAll('[data-nav]').forEach((a) => {
      if (a.dataset.nav === S.route) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
    render();
    await refresh(true);
    $('#app').focus({ preventScroll: true });
  }

  async function refresh(full) {
    try {
      S.state = await api('/api/state');
      S.tz = S.state.tz;
      renderStatus();
      if (S.route === 'overview') render();
      if (S.route === 'schedule' && (full || !S.dirty)) await loadSchedule(full);
      if (S.route === 'energy' && full) await loadEnergy();
      if (S.route === 'devices') await loadDevices(full);
    } catch (e) {
      if (full && e.message !== 'Please sign in.') toast(e.message, true);
    }
  }

  function startPolling() {
    stopPolling();
    S.pollTimer = setInterval(() => { if (!document.hidden) refresh(false); }, 10000);
  }
  function stopPolling() { if (S.pollTimer) clearInterval(S.pollTimer); S.pollTimer = null; }

  async function start() {
    $('#topbar').hidden = false;
    $('#tabbar').hidden = false;
    startPolling();
    await onRoute();
  }

  function render() {
    const views = { overview: viewOverview, schedule: viewSchedule, energy: viewEnergy, devices: viewDevices };
    $('#app').innerHTML = views[S.route]().s;
  }

  function renderStatus() {
    const d = S.state && S.state.device;
    const el = $('#status-pill');
    if (!d) { el.innerHTML = ''; return; }
    if (d.online) {
      el.innerHTML = html`<span class="dot"></span><b>Controller online</b><span class="muted detail">· seen ${d.seen_seconds_ago}s ago</span>`.s;
    } else if (d.seen_seconds_ago === null) {
      el.innerHTML = html`<span class="dot off"></span><b>Controller not connected yet</b>`.s;
    } else {
      el.innerHTML = html`<span class="dot warn"></span><b>Controller offline</b><span class="muted detail">· last seen ${relTime(new Date(Date.now() - d.seen_seconds_ago * 1000).toISOString())}</span>`.s;
    }
  }

  const loading = () => html`<div class="card"><p class="muted">Loading…</p></div>`;

  // =====================================================================================
  // Overview
  // =====================================================================================
  function angleFor(temp, cal) {
    const pts = cal.points.slice().sort((a, b) => a.temp - b.temp);
    let a = pts[0].angle;
    if (temp >= pts[pts.length - 1].temp) a = pts[pts.length - 1].angle;
    else {
      for (let i = 0; i < pts.length - 1; i++) {
        if (temp >= pts[i].temp && temp <= pts[i + 1].temp) {
          const f = (temp - pts[i].temp) / ((pts[i + 1].temp - pts[i].temp) || 1);
          a = pts[i].angle + f * (pts[i + 1].angle - pts[i].angle);
          break;
        }
      }
    }
    return Math.round(Math.max(cal.min_angle, Math.min(cal.max_angle, a)));
  }

  function viewOverview() {
    const s = S.state;
    if (!s) return loading();
    return html`
      <h1 class="sr-only">Overview</h1>
      <div class="row wrap-md">
        ${targetCard(s)}
        ${suggestionCard(s)}
        ${sideCards(s)}
      </div>
      ${forecastCard(s)}
      <div class="row">
        ${upcomingCard(s)}
        ${activityCard(s)}
        ${energyCard(s)}
      </div>`;
  }

  function targetCard(s) {
    const t = s.target;
    const [lo, hi] = s.safe_range;
    const shown = S.pending !== null ? S.pending : t.temp;
    const rot = ((shown - lo) / (hi - lo || 1) - 0.5) * 180;
    const angle = angleFor(shown, s.calibration);
    const modes = [['auto', 'Auto'], ['manual', 'Manual'], ['away', 'Away']];
    const d = s.device;

    let reason;
    if (s.mode === 'manual') reason = html`Manual · stays until you change it`;
    else if (s.mode === 'away') reason = html`Away · kept low until you're back`;
    else if (t.reason === 'Hold') reason = html`Holding until ${timeFmt(t.until)} · <button class="btn-ghost" data-action="resume">Resume schedule</button>`;
    else if (t.reason === 'Preheat') reason = html`Preheating until ${timeFmt(t.until)}`;
    else reason = html`Schedule: ${t.reason}${t.until ? html` until ${timeFmt(t.until)}` : ''}`;

    let footer;
    if (S.pending !== null && S.pending !== t.temp) {
      footer = html`<div class="actions">
        <button class="btn btn-primary grow" data-action="apply">Apply ${fmtNum(S.pending)} °C</button>
        <button class="btn-ghost" data-action="cancel-pending">Cancel</button></div>`;
    } else if (d.online && d.angle === t.angle) {
      footer = html`<div class="sent">${ICON.check} Dial set to ${fmtNum(t.temp)} °C · servo ${t.angle}°</div>`;
    } else if (d.online) {
      footer = html`<div class="sent wait">Sending ${fmtNum(t.temp)} °C to the controller…</div>`;
    } else {
      footer = html`<div class="sent wait">Controller offline — it will move to ${fmtNum(t.temp)} °C when it reconnects</div>`;
    }

    return html`
      <section class="card target-card" aria-labelledby="target-h">
        <div class="card-head">
          <h2 id="target-h">Target temperature</h2>
          <div class="seg" role="group" aria-label="Mode">
            ${modes.map(([id, label]) => html`<button aria-pressed="${s.mode === id}" data-action="mode" data-mode="${id}">${label}</button>`)}
          </div>
        </div>
        <div class="dial">
          <svg class="gauge" viewBox="0 0 280 160" aria-hidden="true">
            <path d="M26 140 A114 114 0 0 1 254 140" fill="none" stroke="#EFECE6" stroke-width="18" stroke-linecap="round"/>
            <path d="M26 140 A114 114 0 0 1 254 140" fill="none" stroke="#F3C6A5" stroke-width="18" stroke-linecap="round" stroke-dasharray="4 10"/>
            <line x1="140" y1="140" x2="140" y2="44" stroke="#C2410C" stroke-width="4" stroke-linecap="round" transform="rotate(${rot.toFixed(1)} 140 140)"/>
            <circle cx="140" cy="140" r="9" fill="#16181B"/>
            <text x="18" y="158" font-size="12" fill="#5B6068" font-family="IBM Plex Mono">${lo}°</text>
            <text x="240" y="158" font-size="12" fill="#5B6068" font-family="IBM Plex Mono">${hi}°</text>
          </svg>
          <div class="setpoint">
            <button class="btn round" aria-label="Lower target by half a degree" data-action="step" data-delta="-0.5" ${shown <= lo ? 'disabled' : ''}>${ICON.minus}</button>
            <div class="big" aria-live="polite">${fmtNum(shown)}<small>°C</small></div>
            <button class="btn round" aria-label="Raise target by half a degree" data-action="step" data-delta="0.5" ${shown >= hi ? 'disabled' : ''}>${ICON.plus}</button>
          </div>
          <div class="small muted">${reason}</div>
        </div>
        <div class="row" style="gap:8px">
          <div class="mini"><div class="label">Old dial · SG90 servo</div>
            <div class="value mono">${angle}° ${s.calibrated ? '' : html`<span class="small" style="color:#B45309">uncalibrated</span>`}</div></div>
          <div class="mini"><div class="label">Wi-Fi thermostat</div>
            <div class="value">${s.homeassistant.enabled ? 'Connected' : 'Not connected'}</div></div>
        </div>
        ${footer}
      </section>`;
  }

  function suggestionCard(s) {
    const a = s.analysis;
    const sg = s.suggestion;
    const facts = a ? html`
      <div class="facts">
        <div class="fact"><div class="label">Outside now</div><div class="value">${fmtTemp(a.now.temp)}</div></div>
        <span style="color:#A8ADB4">${ICON.arrow}</span>
        <div class="fact"><div class="label">In ${a.window} hours</div><div class="value" style="color:#9DB8F5">${fmtTemp(a.ahead.temp)}</div></div>
        <div class="fact"><div class="label">Decision</div><div class="value" style="color:${s.decision === 'PREPARE' ? '#F8B98C' : '#A7E3B8'}">${s.decision}</div></div>
      </div>` : '';

    if (!a) {
      return html`<section class="card card-dark suggest grow" aria-labelledby="sg-h">
        <span class="chip heat" style="align-self:flex-start">FORECAST</span>
        <h2 id="sg-h">Forecast unavailable right now</h2>
        <p>The schedule keeps running as normal. We'll try again in a few minutes.</p></section>`;
    }
    if (!sg) {
      return html`<section class="card card-dark suggest grow" aria-labelledby="sg-h">
        <div class="actions"><span class="chip heat">FORECAST</span><span class="small" style="color:#A8ADB4">next ${a.window} hours</span></div>
        <h2 id="sg-h">No preheat needed right now.</h2>
        <p>The temperature changes by ${a.change > 0 ? '+' : ''}${fmtNum(a.change)} °C over the next ${a.window} hours, so your normal schedule is fine.</p>
        ${facts}</section>`;
    }
    let actions;
    if (sg.status === 'accepted') {
      actions = html`<div class="actions"><span style="display:flex;gap:8px;align-items:center;font-weight:600;color:#A7E3B8">${ICON.check} Scheduled: ${fmtNum(sg.temp)} °C at ${timeFmt(sg.start)}</span>
        <button class="btn sm btn-on-dark" data-action="suggestion" data-id="${sg.id}" data-do="undo">Undo</button></div>`;
    } else if (sg.status === 'dismissed') {
      actions = html`<div class="actions"><span style="color:#C9CDD2">Skipped. Your normal schedule stays.</span>
        <button class="btn sm btn-on-dark" data-action="suggestion" data-id="${sg.id}" data-do="undo">Undo</button></div>`;
    } else {
      actions = html`<div class="actions">
        <button class="btn btn-accent-dark" data-action="suggestion" data-id="${sg.id}" data-do="accept">Accept</button>
        <a class="btn btn-on-dark" href="#/schedule">Change schedule</a>
        <button class="btn-ghost" style="color:#C9CDD2" data-action="suggestion" data-id="${sg.id}" data-do="dismiss">Not this time</button></div>`;
    }
    return html`
      <section class="card card-dark suggest grow" aria-labelledby="sg-h">
        <div class="actions"><span class="chip heat">SUGGESTION</span><span class="small" style="color:#A8ADB4">from the live forecast</span></div>
        <h2 id="sg-h">It drops ${fmtNum(sg.drop)} °C in the next ${sg.hours} hours.<br>Preheat to ${fmtNum(sg.temp)} °C at ${timeFmt(sg.start)}?</h2>
        <p>Starting early lets the heating warm the house gradually before the coldest hour, instead of running hard when it's already cold.</p>
        ${facts}
        <div style="flex-grow:1"></div>
        ${actions}
      </section>`;
  }

  function sideCards(s) {
    const a = s.analysis;
    const d = s.device;
    return html`
      <div class="side">
        <section class="card" style="gap:6px">
          <div class="small muted">Outside now · ${s.location}</div>
          <div class="big-temp">${a ? `${fmtNum(a.now.temp)}°C` : '—'}</div>
          <div>${a ? `Feels like ${fmtNum(a.now.feels)} °C` : 'Waiting for forecast'}</div>
          <div class="small muted" style="margin-top:6px">Open-Meteo · ${s.forecast.fetched_at ? `updated ${timeFmt(s.forecast.fetched_at)}` : 'not loaded'}${s.forecast.stale ? ' · offline copy' : ''}</div>
        </section>
        <section class="card" style="gap:6px">
          <div class="small muted">Indoor</div>
          ${d.indoor_temp !== null && d.indoor_temp !== undefined
            ? html`<div class="big-temp" style="color:var(--heat)">${fmtNum(d.indoor_temp)}°C</div><div class="small muted">From the controller's sensor</div>`
            : html`<div class="big-temp" style="color:#8A8F96;font-size:32px">—</div>
                <p class="small" style="color:var(--ink-2);line-height:1.45">Add a temperature sensor to the controller to see the real room temperature here.</p>
                <a class="link" href="#/devices">Set up sensor →</a>`}
        </section>
      </div>`;
  }

  function forecastCard(s) {
    const hours = s.forecast.hours;
    if (!hours.length) {
      return html`<section class="card"><h2>Next 8 hours</h2><p class="muted">The forecast couldn't be loaded. Your schedule keeps running.</p></section>`;
    }
    const temps = hours.map((h) => h.temp);
    const max = Math.max(...temps), min = Math.min(...temps);
    const span = max - min || 1;
    const colW = 800 / hours.length;
    const pts = hours.map((h, i) => `${(colW / 2 + i * colW).toFixed(1)},${(12 + ((max - h.temp) / span) * 66).toFixed(1)}`);
    const w = s.analysis ? s.analysis.window : -1;
    const markX = colW / 2 + w * colW;
    return html`
      <section class="card" aria-labelledby="fc-h">
        <div class="card-head"><h2 id="fc-h">Next ${hours.length} hours</h2><span class="small muted">Temperature · feels like</span></div>
        <svg class="chart forecast-chart" viewBox="0 0 800 90" preserveAspectRatio="none" height="90" aria-hidden="true">
          <polygon points="${pts.join(' ')} ${pts[pts.length - 1].split(',')[0]},90 ${pts[0].split(',')[0]},90" fill="#E3EBFB"/>
          <polyline points="${pts.join(' ')}" fill="none" stroke="#1E4FBF" stroke-width="2.5" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
          ${w > 0 && w < hours.length ? html`<line x1="${markX}" y1="0" x2="${markX}" y2="90" stroke="#C2410C" stroke-width="1.5" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>` : ''}
        </svg>
        <div class="forecast-cols">
          ${hours.map((h, i) => html`<div class="${i === w ? 'hl' : ''}">
            <div class="h">${i === 0 ? 'Now' : hourFmt(h.time)}${i === w ? ` · +${w} h` : ''}</div>
            <div class="t">${fmtTemp(h.temp)}</div><div class="f">${fmtTemp(h.feels)}</div></div>`)}
        </div>
      </section>`;
  }

  function upcomingCard(s) {
    return html`
      <section class="card grow" aria-labelledby="up-h">
        <div class="card-head"><h2 id="up-h">Coming up</h2><a class="link" href="#/schedule">Edit schedule</a></div>
        <div>${s.upcoming.length ? s.upcoming.map((u) => html`
          <div class="list-row"><span class="mono small" style="width:86px">${timeFmt(u.at)}</span>
          <span class="grow">${u.name} ${u.suggested ? html`<span class="small" style="color:var(--heat-ink);font-weight:600">preheat</span>` : ''}</span>
          <b>${fmtTemp(u.temp)}</b></div>`) : html`<p class="muted">No changes in the next 24 hours.</p>`}
        </div>
      </section>`;
  }

  function activityCard(s) {
    const arrow = { out: '→', in: '←', user: '•', info: '·', error: '!' };
    return html`
      <section class="card grow" aria-labelledby="log-h">
        <div class="card-head"><h2 id="log-h">Controller activity</h2><a class="link" href="#/devices">Devices</a></div>
        <div class="log">${s.log.length ? s.log.map((e) => html`
          <div><span class="when">${timeFmt(e.ts)}</span><span class="${e.kind}">${arrow[e.kind] || '·'} ${e.text}</span></div>`)
          : html`<p class="muted">Nothing yet.</p>`}</div>
      </section>`;
  }

  function energyCard(s) {
    const e = s.energy;
    return html`
      <section class="card grow" aria-labelledby="en-h">
        <div class="card-head"><h2 id="en-h">Energy this month</h2></div>
        ${e.month_kwh !== null
          ? html`<div class="kpi"><div class="label">Used so far (${e.days} days of data)</div><div class="value">${fmtNum(e.month_kwh)} <small>kWh</small></div></div>`
          : html`<p class="muted">No usage data yet. Import the usage file from your NB Power account, or type in a meter reading.</p>`}
        <p class="small" style="color:var(--ink-2)">From your NB Power usage data. The meter itself is never touched.</p>
        <a class="btn sm" href="#/energy" style="margin-top:auto">Energy &amp; NB Power report</a>
      </section>`;
  }

  // =====================================================================================
  // Schedule
  // =====================================================================================
  async function loadSchedule(full) {
    S.schedule = await api('/api/schedule');
    if (full || !S.dirty) {
      S.draft = JSON.parse(JSON.stringify(S.schedule.periods));
      S.dirty = false;
    }
    if (S.route === 'schedule') render();
  }

  // Target temperature at a given home-local date & minute, from the draft schedule.
  function periodAt(periods, date, minute) {
    for (let back = 0; back < 8; back++) {
      const d = addDays(date, -back);
      const wd = weekdayOf(d);
      let cands = periods.filter((p) => p.days.includes(wd));
      if (back === 0) cands = cands.filter((p) => hm(p.start) <= minute);
      if (cands.length) return cands.reduce((a, b) => (hm(b.start) > hm(a.start) ? b : a));
    }
    return null;
  }

  function dayTimeline(date) {
    // Breakpoints: every period start on that day + event starts/ends on that day.
    const wd = weekdayOf(date);
    const marks = new Set([0, 1440]);
    S.draft.filter((p) => p.days.includes(wd)).forEach((p) => marks.add(hm(p.start)));
    const events = (S.schedule.events || []).map((ev) => ({ ...ev, s: homeParts(ev.start), e: homeParts(ev.end) }));
    const evRects = [];
    events.forEach((ev) => {
      const from = ev.s.date === date ? ev.s.minute : ev.s.date < date ? 0 : null;
      const to = ev.e.date === date ? ev.e.minute : ev.e.date > date ? 1440 : null;
      if (from !== null && to !== null && from < to) { marks.add(from); marks.add(to); evRects.push({ from, to, temp: ev.temp }); }
    });
    const sorted = [...marks].sort((a, b) => a - b);
    const segs = [];
    for (let i = 0; i < sorted.length - 1; i++) {
      const m = sorted[i];
      const ev = evRects.find((r) => r.from <= m && m < r.to);
      const p = periodAt(S.draft, date, m);
      const base = p ? p.temp : 20;
      segs.push({ from: m, to: sorted[i + 1], temp: ev ? Math.max(ev.temp, base) : base });
    }
    return { segs, evRects };
  }

  function scheduleChart() {
    const today = homeParts(S.schedule.now);
    const date = addDays(today.date, S.chartDay);
    const { segs, evRects } = dayTimeline(date);
    const [lo, hi] = S.schedule.safe_range;
    const yMin = lo - 1, yMax = hi + 1;
    const X = (m) => 40 + (m / 1440) * 1232;
    const Y = (t) => 20 + ((yMax - t) / (yMax - yMin)) * 160;
    let d = '';
    segs.forEach((s, i) => {
      d += i === 0 ? `M${X(s.from)} ${Y(s.temp)}` : ` V${Y(s.temp)}`;
      d += ` H${X(s.to)}`;
    });
    const ticks = [];
    for (let t = Math.ceil(yMin); t <= yMax; t += 2) ticks.push(t);
    const nowX = S.chartDay === 0 ? X(today.minute) : null;
    const label = segs.map((s) => `${fmtNum(s.temp)}° from ${String(Math.floor(s.from / 60)).padStart(2, '0')}:${String(s.from % 60).padStart(2, '0')}`).join(', ');
    return html`
      <svg class="chart" viewBox="0 0 1312 210" role="img" aria-label="Target temperature on ${DAY_NAMES[weekdayOf(date)]}: ${label}">
        ${ticks.map((t) => html`<line x1="40" y1="${Y(t)}" x2="1272" y2="${Y(t)}" stroke="#EFECE6"/><text x="0" y="${Y(t) + 4}" font-size="12" fill="#5B6068" font-family="IBM Plex Mono">${t}°</text>`)}
        ${evRects.map((r) => html`<rect x="${X(r.from)}" y="20" width="${X(r.to) - X(r.from)}" height="160" fill="#FDEBDD" stroke="#C2410C" stroke-dasharray="4 4"/>`)}
        <path d="${d}" fill="none" stroke="#16181B" stroke-width="3" stroke-linejoin="round"/>
        ${nowX !== null ? html`<line x1="${nowX}" y1="10" x2="${nowX}" y2="180" stroke="#1E4FBF" stroke-width="1.5"/><text x="${nowX + 6}" y="16" font-size="12" fill="#1E4FBF" font-weight="600">now</text>` : ''}
        ${[0, 3, 6, 9, 12, 15, 18, 21, 24].map((h) => html`<text x="${X(h * 60) - 10}" y="202" font-size="12" fill="#5B6068" font-family="IBM Plex Mono">${h === 0 || h === 24 ? '12a' : h === 12 ? '12p' : h < 12 ? `${h}a` : `${h - 12}p`}</text>`)}
      </svg>`;
  }

  function viewSchedule() {
    if (!S.schedule || !S.draft) return html`<h1>Schedule</h1>${loading()}`;
    const [lo, hi] = S.schedule.safe_range;
    const sg = S.schedule.suggestion;
    const r = S.schedule.rules;
    return html`
      <div class="page-head">
        <div><h1>Schedule</h1><p>Set the temperature you want for each part of the day. Forecast suggestions show in orange.</p></div>
        <div class="seg" role="tablist" aria-label="Day to show">
          <button role="tab" aria-selected="${S.chartDay === 0}" data-action="chart-day" data-day="0">Today</button>
          <button role="tab" aria-selected="${S.chartDay === 1}" data-action="chart-day" data-day="1">Tomorrow</button>
        </div>
      </div>
      <section class="card">
        <div class="actions small" style="color:var(--ink-2)">
          <span style="display:flex;align-items:center;gap:6px"><span style="width:18px;height:3px;background:#16181B"></span>Your target</span>
          <span style="display:flex;align-items:center;gap:6px"><span style="width:14px;height:14px;background:#FDEBDD;border:1px dashed #C2410C"></span>Accepted preheat</span>
        </div>
        ${scheduleChart()}
      </section>
      <div class="row">
        <section class="card grow" aria-labelledby="per-h" style="gap:4px">
          <div class="card-head" style="margin-bottom:8px"><h2 id="per-h">Periods</h2>
            <button class="btn sm" data-action="add-period">+ Add period</button></div>
          ${sg && sg.status === 'pending' ? html`
            <div class="period suggested">
              <div><b>Preheat</b><div class="small" style="color:#9A3412;font-weight:600">Suggested · ${fmtNum(sg.drop)} °C drop ahead</div></div>
              <div class="mono small time">${timeFmt(sg.start)} – ${timeFmt(sg.end)}</div>
              <div class="stepper"><span class="val">${fmtTemp(sg.temp)}</span></div>
              <div class="actions">
                <button class="btn sm btn-primary" data-action="suggestion" data-id="${sg.id}" data-do="accept">Accept</button>
                <button class="btn-ghost" data-action="suggestion" data-id="${sg.id}" data-do="dismiss">Dismiss</button>
              </div><div></div>
            </div>` : ''}
          ${S.draft.map((p, i) => html`
            <div class="period">
              <label><span class="sr-only">Name</span><input class="input" value="${p.name}" data-edit="name" data-i="${i}" maxlength="40"></label>
              <label class="time"><span class="sr-only">Starts at</span><input class="input mono" type="time" value="${p.start}" data-edit="start" data-i="${i}" required></label>
              <div class="stepper">
                <button class="btn round sm" aria-label="Lower ${p.name} target" data-action="period-step" data-i="${i}" data-delta="-0.5" ${p.temp <= lo ? 'disabled' : ''}>${ICON.minus}</button>
                <span class="val">${fmtTemp(p.temp)}</span>
                <button class="btn round sm" aria-label="Raise ${p.name} target" data-action="period-step" data-i="${i}" data-delta="0.5" ${p.temp >= hi ? 'disabled' : ''}>${ICON.plus}</button>
              </div>
              <div class="days" role="group" aria-label="Days for ${p.name}">
                ${DAY_LETTERS.map((l, d) => html`<button aria-pressed="${p.days.includes(d)}" aria-label="${DAY_NAMES[d]}" data-action="toggle-day" data-i="${i}" data-d="${d}">${l}</button>`)}
              </div>
              <button class="btn-ghost" aria-label="Delete ${p.name}" data-action="delete-period" data-i="${i}" ${S.draft.length <= 1 ? 'disabled' : ''}>${ICON.trash}</button>
            </div>`)}
          <div class="save-bar" style="margin-top:12px">
            ${S.dirty ? html`<span class="small muted">Unsaved changes</span>` : ''}
            <button class="btn sm" data-action="discard-schedule" ${S.dirty ? '' : 'disabled'}>Discard</button>
            <button class="btn sm btn-dark" data-action="save-schedule" ${S.dirty ? '' : 'disabled'}>Save schedule</button>
          </div>
        </section>
        <section class="card" style="width:400px;flex-shrink:0" aria-labelledby="rules-h">
          <h2 id="rules-h">Smart rules</h2>
          <label class="check"><input type="checkbox" data-rule="preheat_suggestions" ${r.preheat_suggestions ? 'checked' : ''}>
            <span><b>Suggest a preheat before cold drops</b><span>When the forecast falls ${fmtNum(r.drop_threshold)} °C or more within ${r.window_hours} hours</span></span></label>
          <label class="check"><input type="checkbox" data-rule="auto_apply" ${r.auto_apply ? 'checked' : ''}>
            <span><b>Accept suggestions automatically</b><span>Off: you approve every preheat first</span></span></label>
          <div class="row" style="gap:12px">
            <label class="field grow">Drop of (°C)<input class="input" type="number" min="0.5" max="10" step="0.5" value="${r.drop_threshold}" data-rule="drop_threshold"></label>
            <label class="field grow">Within (hours)<input class="input" type="number" min="1" max="24" step="1" value="${r.window_hours}" data-rule="window_hours"></label>
            <label class="field grow">Preheat by (°C)<input class="input" type="number" min="0.5" max="4" step="0.5" value="${r.preheat_boost}" data-rule="preheat_boost"></label>
          </div>
          <div class="note" style="margin-top:auto">The controller keeps a copy of this schedule, so it carries on even if your Wi-Fi or this website is down.</div>
        </section>
      </div>`;
  }

  // =====================================================================================
  // Energy & reports
  // =====================================================================================
  function monthOptions() {
    const now = S.state ? homeParts(S.state.now).date : new Date().toISOString().slice(0, 10);
    let [y, m] = now.split('-').map(Number);
    const out = [];
    for (let i = 0; i < 18; i++) {
      const key = `${y}-${String(m).padStart(2, '0')}`;
      const label = new Date(Date.UTC(y, m - 1, 1)).toLocaleString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' });
      out.push([key, label]);
      m -= 1; if (m === 0) { m = 12; y -= 1; }
    }
    return out;
  }

  async function loadEnergy() {
    const q = S.month ? `?month=${encodeURIComponent(S.month)}` : '';
    S.energy = await api(`/api/energy${q}`);
    S.month = S.energy.month;
    if (S.route === 'energy') render();
  }

  function usageChart(sum, month) {
    const [y, m] = month.split('-').map(Number);
    const days = new Date(Date.UTC(y, m, 0)).getUTCDate();
    const byDay = {};
    sum.days.forEach((d) => { byDay[d.day] = d; });
    const max = Math.max(1, ...sum.days.map((d) => d.kwh));
    const step = 780 / days;
    const bw = Math.max(4, step * 0.66);
    const bars = [];
    for (let i = 1; i <= days; i++) {
      const key = `${month}-${String(i).padStart(2, '0')}`;
      const d = byDay[key];
      const x = 40 + (i - 1) * step + (step - bw) / 2;
      if (d) {
        const h = (d.kwh / max) * 160;
        bars.push(html`<rect x="${x.toFixed(1)}" y="${(190 - h).toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="3" fill="${d.source === 'meter' ? '#E08A55' : '#C2410C'}"><title>${key}: ${fmtNum(d.kwh, 2)} kWh${d.mean_temp !== null ? ` · outside ${fmtNum(d.mean_temp)} °C` : ''}</title></rect>`);
      }
      if (i === 1 || i % 5 === 0) bars.push(html`<text x="${(x + bw / 2 - 6).toFixed(1)}" y="208" font-size="12" fill="#5B6068" font-family="IBM Plex Mono">${i}</text>`);
    }
    return html`
      <svg class="chart" viewBox="0 0 820 215" role="img" aria-label="Daily electricity use in kWh, highest day ${fmtNum(max)} kWh">
        <line x1="40" y1="190" x2="820" y2="190" stroke="#D6D2CA"/>
        <line x1="40" y1="30" x2="820" y2="30" stroke="#EFECE6"/>
        <text x="0" y="194" font-size="12" fill="#5B6068" font-family="IBM Plex Mono">0</text>
        <text x="0" y="34" font-size="12" fill="#5B6068" font-family="IBM Plex Mono">${fmtNum(max, 0)}</text>
        ${bars}
      </svg>`;
  }

  function viewEnergy() {
    const e = S.energy;
    if (!e) return html`<h1>Energy &amp; reports</h1>${loading()}`;
    const sum = e.summary;
    const has = sum.days.length > 0;
    const fmtOpt = (f, label) => html`<label class="check" style="align-items:center"><input type="radio" name="fmt" value="${f}" data-action="report-fmt" ${S.reportFmt === f ? 'checked' : ''}><span>${label}</span></label>`;
    return html`
      <div class="page-head">
        <div><h1>Energy &amp; reports</h1><p>Uses your own NB Power usage data. Your meter stays exactly as it is — nothing is installed on it.</p></div>
        <label class="field" style="min-width:220px">Month
          <select class="input" data-action="month">${monthOptions().map(([k, l]) => html`<option value="${k}" ${k === S.month ? 'selected' : ''}>${l}</option>`)}</select>
        </label>
      </div>
      ${has ? '' : html`<div class="note warn">No usage data for this month yet. Import the usage file from your NB Power account below, or start entering meter readings.</div>`}
      <div class="grid-4">
        <div class="card kpi" style="gap:4px"><div class="label">Used</div><div class="value">${has ? fmtNum(sum.total_kwh) : '—'} <small>kWh</small></div><div class="small muted">${sum.days.length} days of data</div></div>
        <div class="card kpi" style="gap:4px"><div class="label">Estimated energy cost</div><div class="value">${sum.estimated_cost !== null ? `$${sum.estimated_cost.toFixed(2)}` : '—'}</div><div class="small muted">${e.rate_cents_per_kwh ? `at ${fmtNum(e.rate_cents_per_kwh, 2)} ¢/kWh · estimate` : 'Add your rate below'}</div></div>
        <div class="card kpi" style="gap:4px"><div class="label">Average per day</div><div class="value">${sum.avg_kwh_per_day !== null ? fmtNum(sum.avg_kwh_per_day) : '—'} <small>kWh</small></div></div>
        <div class="card kpi" style="gap:4px"><div class="label">kWh per degree-day</div><div class="value">${sum.kwh_per_hdd !== null ? fmtNum(sum.kwh_per_hdd, 2) : '—'}</div><div class="small muted">Weather-adjusted use · lower is better</div></div>
      </div>
      <div class="row">
        <div class="stack grow">
          <section class="card" aria-labelledby="use-h">
            <div class="card-head"><h2 id="use-h">Daily use</h2>
              <div class="actions small" style="color:var(--ink-2)">
                <span style="display:flex;align-items:center;gap:6px"><span style="width:12px;height:12px;border-radius:3px;background:#C2410C"></span>From NB Power file</span>
                <span style="display:flex;align-items:center;gap:6px"><span style="width:12px;height:12px;border-radius:3px;background:#E08A55"></span>From meter readings</span>
              </div></div>
            ${has ? usageChart(sum, S.month) : html`<p class="muted">Nothing to show yet.</p>`}
          </section>
          <section class="card" aria-labelledby="src-h">
            <h2 id="src-h">Add usage data</h2>
            <div class="row" style="gap:12px">
              <form class="grow stack" id="upload-form" style="gap:8px;padding:14px 16px;border-radius:14px;border:2px solid var(--ink)">
                <b>Import NB Power usage file</b>
                <span class="small" style="color:var(--ink-2)">Download your usage (CSV) from your NB Power online account, then upload it here.</span>
                <input class="input" type="file" name="file" accept=".csv,text/csv" required style="padding-top:9px">
                <button class="btn sm btn-dark" type="submit">Upload</button>
              </form>
              <form class="grow stack" id="reading-form" style="gap:8px;padding:14px 16px;border-radius:14px;border:1px solid var(--line)">
                <b>Enter a meter reading</b>
                <label class="field" style="font-weight:400">Reading on the meter display (kWh)
                  <input class="input mono" name="kwh" inputmode="decimal" placeholder="e.g. 48213" required></label>
                <span class="small muted">${e.last_reading ? `Last: ${fmtNum(e.last_reading.kwh, 1)} kWh on ${dateFmt(new Date(e.last_reading.ts * 1000).toISOString())}` : 'Enter one now and another in a few days.'}</span>
                <button class="btn sm" type="submit">Save reading</button>
              </form>
              <form class="grow stack" id="rate-form" style="gap:8px;padding:14px 16px;border-radius:14px;border:1px solid var(--line)">
                <b>Your electricity rate</b>
                <label class="field" style="font-weight:400">Energy charge (¢ per kWh)
                  <input class="input mono" name="rate" inputmode="decimal" value="${e.rate_cents_per_kwh ?? ''}" placeholder="from your NB Power bill"></label>
                <span class="small muted">Used only for the cost estimate.</span>
                <button class="btn sm" type="submit">Save rate</button>
              </form>
            </div>
          </section>
        </div>
        <section class="card" style="width:440px;flex-shrink:0;gap:16px" aria-labelledby="rep-h">
          <div><h2 id="rep-h" style="font-size:20px">Share a report with NB Power</h2>
            <p class="small" style="color:var(--ink-2);margin-top:6px">You choose what goes in it and who gets it.</p></div>
          <fieldset style="border:0;margin:0;padding:0;display:flex;flex-direction:column;gap:10px">
            <legend class="small" style="font-weight:600;margin-bottom:8px">Include</legend>
            <label class="check"><input type="checkbox" name="inc" value="usage" checked><span>Electricity use by day</span></label>
            <label class="check"><input type="checkbox" name="inc" value="weather" checked><span>Outdoor temperature &amp; degree-days</span></label>
            <label class="check"><input type="checkbox" name="inc" value="schedule" checked><span>Heating schedule and targets</span></label>
          </fieldset>
          <fieldset style="border:0;margin:0;padding:0;display:flex;gap:20px">
            <legend class="small" style="font-weight:600;margin-bottom:8px">Download as</legend>
            ${fmtOpt('csv', 'CSV')}${fmtOpt('html', 'Printable (save as PDF)')}
          </fieldset>
          <button class="btn" data-action="download-report" ${has ? '' : 'disabled'}>Download report</button>
          <form id="send-form" class="stack" style="gap:8px">
            <label class="field">Email it to
              <input class="input" type="email" name="to" placeholder="[NB Power contact email]" required ${e.email_configured ? '' : 'disabled'}></label>
            <button class="btn btn-primary" type="submit" ${e.email_configured && has ? '' : 'disabled'}>Send report</button>
            ${e.email_configured ? '' : html`<span class="small muted">Email sending isn't set up on the server yet. Download the report and attach it to your own email.</span>`}
          </form>
          <div class="note">Sending a report doesn't change your bill or sign you up for any program.</div>
          <div>
            <div class="small" style="font-weight:600;margin-bottom:4px">Past reports</div>
            ${e.reports.length ? e.reports.map((r) => html`<div class="list-row small"><span class="grow">${r.period} · ${r.fmt}</span><span class="muted">${r.action}</span></div>`)
              : html`<p class="small muted">None yet.</p>`}
          </div>
        </section>
      </div>`;
  }

  function reportIncludes() {
    return [...document.querySelectorAll('input[name="inc"]:checked')].map((el) => el.value).join(',');
  }

  // =====================================================================================
  // Devices
  // =====================================================================================
  async function loadDevices(full) {
    S.devices = await api('/api/devices');
    if (full || !S.calDirty) {
      S.calDraft = JSON.parse(JSON.stringify(S.devices.calibration));
      S.calDirty = false;
    }
    if (S.route !== 'devices') return;
    if (full || !$('#device-live')) render();
    else {
      $('#device-live').innerHTML = deviceLive().s;
      const cur = $('#servo-current');
      if (cur) cur.textContent = S.devices.device.angle !== null ? `${S.devices.device.angle}°` : '—';
    }
  }

  function deviceLive() {
    const d = S.devices.device;
    const s = S.state;
    const lcd1 = s ? `${fmtNum(s.target.temp)}C ${s.mode.toUpperCase()}`.slice(0, 16) : '';
    const lcd2 = s ? `MODE: ${s.decision}`.slice(0, 16) : '';
    let status;
    if (d.online) status = html`<span style="display:flex;align-items:center;gap:6px;font-weight:600;color:var(--ok-ink)"><span class="dot"></span>Online${d.via ? ` · ${d.via}` : ''}</span>`;
    else if (d.seen_seconds_ago === null) status = html`<span style="display:flex;align-items:center;gap:6px;font-weight:600"><span class="dot off"></span>Waiting for first connection</span>`;
    else status = html`<span style="display:flex;align-items:center;gap:6px;font-weight:600;color:var(--warn-ink)"><span class="dot warn"></span>Offline</span>`;
    return html`
      <div class="card-head"><h2>Arduino controller</h2>${status}</div>
      <div class="facts-grid">
        <div><div class="k">Last check-in</div><div>${d.seen_seconds_ago === null ? 'Never' : d.seen_seconds_ago < 90 ? `${d.seen_seconds_ago} seconds ago` : relTime(new Date(Date.now() - d.seen_seconds_ago * 1000).toISOString())}</div></div>
        <div><div class="k">Servo angle</div><div class="mono">${d.angle !== null ? `${d.angle}°` : '—'}</div></div>
        <div><div class="k">Wi-Fi signal</div><div>${d.rssi !== null ? `${d.rssi} dBm (${d.rssi > -60 ? 'good' : d.rssi > -75 ? 'fair' : 'weak'})` : '—'}</div></div>
        <div><div class="k">Pins in use</div><div class="mono small">LCD D4–D7, D11, D12 · Servo D9 · D2 taken</div></div>
      </div>
      <div class="actions" style="gap:16px">
        <div class="lcd" role="img" aria-label="LCD shows: ${lcd1}, ${lcd2}">${lcd1.padEnd(16)}
${lcd2.padEnd(16)}</div>
        <span class="small" style="color:var(--ink-2)">What the 16×2 LCD should be showing.</span>
      </div>`;
  }

  function viewDevices() {
    const v = S.devices;
    if (!v) return html`<h1>Devices</h1>${loading()}`;
    const cal = S.calDraft;
    const d = v.device;
    const ha = v.homeassistant;
    const cmd = d.command;
    const token = S.revealToken ? v.device_token : '•'.repeat(24);
    return html`
      <h1>Devices</h1>
      <div class="row">
        <div class="stack" style="width:560px;flex-shrink:0;gap:20px;max-width:100%">
          <section class="card" id="device-live" aria-live="polite">${deviceLive()}</section>
          <div class="note"><b style="color:var(--ink)">If Wi-Fi drops:</b> the controller keeps following the last schedule it received. Changes you make here are picked up as soon as it reconnects.</div>
          <section class="card" aria-labelledby="conn-h">
            <h2 id="conn-h">Connect the controller</h2>
            <p class="small" style="color:var(--ink-2)">Put these two values in the firmware (Wi-Fi Arduino) or the USB bridge script. Keep the key private.</p>
            <div class="field">Server address<div class="token">${location.origin}</div></div>
            <div class="field">Device key<div class="actions"><div class="token grow">${token}</div>
              <button class="btn sm" data-action="reveal-token">${S.revealToken ? 'Hide' : 'Show'}</button>
              <button class="btn sm" data-action="copy-token">Copy</button></div></div>
          </section>
          <section class="card" aria-labelledby="ha-h">
            <div class="card-head"><h2 id="ha-h">Wi-Fi thermostat</h2><span class="chip ${ha.enabled ? 'ok' : ''}">${ha.enabled ? 'Connected' : 'Not connected'}</span></div>
            <p class="small" style="color:var(--ink-2)">Control a modern thermostat (ecobee, Nest, Honeywell, Mysa…) through Home Assistant. The server must be able to reach your Home Assistant.</p>
            <form id="ha-form" class="stack" style="gap:10px">
              <label class="field">Home Assistant address<input class="input" name="url" type="url" value="${ha.url}" placeholder="https://your-home.ui.nabu.casa"></label>
              <label class="field">Thermostat entity<input class="input mono" name="entity_id" value="${ha.entity_id}" placeholder="climate.living_room"></label>
              <label class="field">Long-lived access token<input class="input" name="token" type="password" autocomplete="off" placeholder="${ha.has_token ? 'Saved — leave blank to keep' : 'Create one in your Home Assistant profile'}"></label>
              ${ha.last_error ? html`<div class="note err small">${ha.last_error}</div>` : ''}
              <div class="actions">
                <button class="btn sm btn-dark" type="submit" name="enable" value="1">${ha.enabled ? 'Save' : 'Connect'}</button>
                ${ha.enabled ? html`<button class="btn sm" type="button" data-action="ha-disconnect">Disconnect</button>` : ''}
              </div>
            </form>
          </section>
          <section class="card" aria-labelledby="sensor-h">
            <div class="card-head"><h2 id="sensor-h">Indoor temperature sensor</h2>
              <span class="small muted">${d.indoor_temp !== null ? `${fmtNum(d.indoor_temp)} °C` : 'None reported'}</span></div>
            <p class="small" style="color:var(--ink-2)">Optional. A DS18B20 or DHT22 on a free pin (e.g. D3 or D8) lets the controller report the real room temperature. The firmware sends it automatically once connected.</p>
          </section>
        </div>

        <section class="card grow" aria-labelledby="servo-h" style="gap:18px">
          <div class="card-head" style="align-items:flex-start">
            <div><h2 id="servo-h" style="font-size:20px">Old thermostat · SG90 servo on D9</h2>
              <p class="small" style="color:var(--ink-2);margin-top:6px">The servo turns the dial for you. Calibrate once so each temperature lands in the right spot.</p></div>
            <span class="chip ${v.calibrated ? 'ok' : 'warn'}">${v.calibrated ? 'Calibrated' : 'Not calibrated'}</span>
          </div>
          <div class="row" style="gap:16px">
            <div class="grow stack" style="padding:16px;border-radius:14px;background:var(--surface-2);gap:12px">
              <b>1 · Test movement</b>
              <div class="small" style="color:var(--ink-2)">Current angle <span class="mono" id="servo-current" style="color:var(--ink);font-weight:500">${d.angle !== null ? `${d.angle}°` : '—'}</span></div>
              <div class="actions" style="gap:8px">
                ${[70, 90, 110].filter((a) => a >= cal.min_angle && a <= cal.max_angle).map((a) => html`<button class="btn sm mono grow" data-action="servo" data-angle="${a}">${a}°</button>`)}
              </div>
              <form id="servo-form" class="actions" style="gap:8px">
                <label class="sr-only" for="servo-angle">Angle</label>
                <input id="servo-angle" class="input mono grow" name="angle" type="number" min="${cal.min_angle}" max="${cal.max_angle}" placeholder="${cal.min_angle}–${cal.max_angle}" required style="width:auto">
                <button class="btn sm btn-dark" type="submit">Move</button>
              </form>
              ${cmd ? html`<div class="small" style="color:var(--ink-2)">Waiting for the controller to move to ${cmd.angle}°… <button class="btn-ghost" data-action="servo-cancel">Cancel</button></div>` : ''}
            </div>
            <form id="limits-form" class="grow stack" style="padding:16px;border-radius:14px;background:var(--surface-2);gap:12px">
              <b>2 · Safe limits</b>
              <div class="actions" style="gap:10px;flex-wrap:nowrap">
                <label class="field grow" style="font-weight:400">Min angle<input class="input mono" name="min" type="number" min="0" max="180" value="${cal.min_angle}" required></label>
                <label class="field grow" style="font-weight:400">Max angle<input class="input mono" name="max" type="number" min="0" max="180" value="${cal.max_angle}" required></label>
              </div>
              <button class="btn sm" type="submit">Save limits</button>
            </form>
          </div>
          <div class="note warn">Keep the servo detached from the dial until you know where the dial's end stops are. The controller never moves past the safe limits.</div>
          <div>
            <b>3 · Temperature → angle</b>
            <p class="small" style="color:var(--ink-2);margin:4px 0 8px">Move the servo until the dial reads each temperature, then press “Use current angle”.</p>
            <div class="cal-row head"><span>Dial temp</span><span>Servo angle</span><span class="status">Status</span><span></span></div>
            ${cal.points.map((p, i) => html`
              <div class="cal-row">
                <b>${fmtNum(p.temp)} °C</b>
                <label><span class="sr-only">Angle for ${fmtNum(p.temp)} °C</span><input class="input mono" type="number" min="${cal.min_angle}" max="${cal.max_angle}" value="${p.angle}" data-cal="${i}"></label>
                <span class="small status" style="color:${p.measured ? 'var(--ok-ink)' : 'var(--warn-ink)'}">${p.measured ? 'Measured' : 'Example'}</span>
                <button class="btn sm" data-action="cal-use" data-i="${i}" ${d.angle === null ? 'disabled' : ''}>Use current angle</button>
              </div>`)}
            <div class="save-bar" style="margin-top:12px">
              ${S.calDirty ? html`<span class="small muted">Unsaved changes</span>` : ''}
              <button class="btn sm" data-action="cal-discard" ${S.calDirty ? '' : 'disabled'}>Discard</button>
              <button class="btn sm btn-dark" data-action="cal-save" ${S.calDirty ? '' : 'disabled'}>Save calibration</button>
            </div>
          </div>
        </section>
      </div>`;
  }

  // =====================================================================================
  // Events
  // =====================================================================================
  const actions = {
    logout: () => run(async () => { await api('/api/logout', { method: 'POST' }); showLogin(); }),

    // overview
    mode: (el) => run(async () => {
      S.state = await api('/api/control', { method: 'POST', body: { mode: el.dataset.mode } });
      S.pending = null;
      render(); renderStatus();
    }),
    step: (el) => {
      const [lo, hi] = S.state.safe_range;
      const base = S.pending !== null ? S.pending : S.state.target.temp;
      S.pending = Math.max(lo, Math.min(hi, base + Number(el.dataset.delta)));
      render();
    },
    'cancel-pending': () => { S.pending = null; render(); },
    apply: () => run(async () => {
      S.state = await api('/api/control', { method: 'POST', body: { temp: S.pending } });
      S.pending = null;
      render();
      toast(S.state.mode === 'auto' ? `Set to ${fmtNum(S.state.target.temp)} °C until the next scheduled change` : `Set to ${fmtNum(S.state.target.temp)} °C`);
    }),
    resume: () => run(async () => {
      S.state = await api('/api/control', { method: 'POST', body: { resume: true } });
      S.pending = null; render();
    }),
    suggestion: (el) => run(async () => {
      S.state = await api(`/api/suggestion/${encodeURIComponent(el.dataset.id)}/${el.dataset.do}`, { method: 'POST' });
      if (S.route === 'schedule') await loadSchedule(false); else render();
      if (el.dataset.do === 'accept') toast('Preheat scheduled');
    }),

    // schedule
    'chart-day': (el) => { S.chartDay = Number(el.dataset.day); render(); },
    'period-step': (el) => {
      const p = S.draft[Number(el.dataset.i)];
      const [lo, hi] = S.schedule.safe_range;
      p.temp = Math.max(lo, Math.min(hi, p.temp + Number(el.dataset.delta)));
      S.dirty = true; render();
    },
    'toggle-day': (el) => {
      const p = S.draft[Number(el.dataset.i)];
      const d = Number(el.dataset.d);
      p.days = p.days.includes(d) ? p.days.filter((x) => x !== d) : [...p.days, d].sort();
      S.dirty = true; render();
    },
    'add-period': () => {
      S.draft.push({ id: `p${Date.now().toString(36)}`, name: 'New period', start: '12:00', temp: 20, days: [0, 1, 2, 3, 4, 5, 6] });
      S.dirty = true; render();
    },
    'delete-period': (el) => { S.draft.splice(Number(el.dataset.i), 1); S.dirty = true; render(); },
    'discard-schedule': () => { S.draft = JSON.parse(JSON.stringify(S.schedule.periods)); S.dirty = false; render(); },
    'save-schedule': () => run(async () => {
      S.schedule = await api('/api/schedule', { method: 'PUT', body: { periods: S.draft } });
      S.draft = JSON.parse(JSON.stringify(S.schedule.periods));
      S.dirty = false; render();
      toast('Schedule saved — the controller picks it up within seconds');
    }),

    // energy
    'download-report': () => {
      const q = new URLSearchParams({ month: S.month, fmt: S.reportFmt, include: reportIncludes() });
      window.open(`/api/reports/export?${q}`, '_blank', 'noopener');
      setTimeout(() => run(loadEnergy), 1500);
    },

    // devices
    'reveal-token': () => { S.revealToken = !S.revealToken; render(); },
    'copy-token': () => run(async () => { await navigator.clipboard.writeText(S.devices.device_token); toast('Device key copied'); }),
    servo: (el) => run(async () => { S.devices = await api('/api/servo/test', { method: 'POST', body: { angle: Number(el.dataset.angle) } }); render(); }),
    'servo-cancel': () => run(async () => { S.devices = await api('/api/servo/cancel', { method: 'POST' }); render(); }),
    'cal-use': (el) => {
      const p = S.calDraft.points[Number(el.dataset.i)];
      p.angle = S.devices.device.angle;
      p.measured = true;
      S.calDirty = true; render();
    },
    'cal-discard': () => { S.calDraft = JSON.parse(JSON.stringify(S.devices.calibration)); S.calDirty = false; render(); },
    'cal-save': () => run(async () => {
      S.devices = await api('/api/calibration', { method: 'PUT', body: S.calDraft });
      S.calDraft = JSON.parse(JSON.stringify(S.devices.calibration));
      S.calDirty = false; render(); toast('Calibration saved');
    }),
    'ha-disconnect': () => run(async () => {
      S.devices = await api('/api/homeassistant', { method: 'PUT', body: { enabled: false } });
      render(); toast('Wi-Fi thermostat disconnected');
    }),
  };

  document.addEventListener('click', (ev) => {
    const el = ev.target.closest('[data-action]');
    if (!el || el.tagName === 'SELECT' || (el.tagName === 'INPUT' && el.type !== 'button')) return;
    const fn = actions[el.dataset.action];
    if (fn) { ev.preventDefault(); fn(el, ev); }
  });

  document.addEventListener('change', (ev) => {
    const el = ev.target;
    if (el.dataset.action === 'month') { S.month = el.value; run(loadEnergy); return; }
    if (el.dataset.action === 'report-fmt') { S.reportFmt = el.value; return; }
    if (el.dataset.rule) {
      const value = el.type === 'checkbox' ? el.checked : el.value;
      run(async () => {
        const data = await api('/api/rules', { method: 'PUT', body: { [el.dataset.rule]: value } });
        S.schedule = { ...S.schedule, ...data, periods: data.periods };
        render();
      });
      return;
    }
    if (el.dataset.cal !== undefined) {
      const p = S.calDraft.points[Number(el.dataset.cal)];
      p.angle = Number(el.value);
      p.measured = true;
      S.calDirty = true;
      render();
    }
  });

  document.addEventListener('input', (ev) => {
    const el = ev.target;
    if (el.dataset.edit && S.draft) {
      S.draft[Number(el.dataset.i)][el.dataset.edit] = el.value;
      if (!S.dirty) {
        S.dirty = true;
        // Update only the save bar so typing isn't interrupted.
        const bar = document.querySelector('.save-bar');
        if (bar) bar.querySelectorAll('button').forEach((b) => { b.disabled = false; });
      }
    }
  });

  document.addEventListener('submit', (ev) => {
    const form = ev.target;
    const handlers = {
      'upload-form': () => run(async () => {
        const fd = new FormData(form);
        const r = await api('/api/energy/upload', { method: 'POST', form: fd });
        toast(`Imported ${r.days} days (${r.first} to ${r.last})${r.skipped ? ` · ${r.skipped} rows skipped` : ''}`);
        S.month = r.last.slice(0, 7);
        await loadEnergy();
      }),
      'reading-form': () => run(async () => {
        const r = await api('/api/energy/reading', { method: 'POST', body: { kwh: form.kwh.value } });
        toast(r.first ? 'First reading saved. Add another in a few days to see usage.' : 'Reading saved');
        await loadEnergy();
      }),
      'rate-form': () => run(async () => {
        await api('/api/settings', { method: 'PUT', body: { rate_cents_per_kwh: form.rate.value } });
        toast('Rate saved');
        await loadEnergy();
      }),
      'send-form': () => run(async () => {
        await api('/api/reports/send', { method: 'POST', body: { month: S.month, to: form.to.value, include: reportIncludes() } });
        toast('Report sent');
        await loadEnergy();
      }),
      'servo-form': () => run(async () => {
        S.devices = await api('/api/servo/test', { method: 'POST', body: { angle: Number(form.angle.value) } });
        render();
      }),
      'limits-form': () => run(async () => {
        S.devices = await api('/api/calibration', { method: 'PUT', body: { ...S.devices.calibration, min_angle: Number(form.min.value), max_angle: Number(form.max.value) } });
        S.calDraft = JSON.parse(JSON.stringify(S.devices.calibration));
        S.calDirty = false; render(); toast('Safe limits saved');
      }),
      'ha-form': () => run(async () => {
        S.devices = await api('/api/homeassistant', { method: 'PUT', body: { url: form.url.value, entity_id: form.entity_id.value, token: form.token.value, enabled: true } });
        render(); toast('Wi-Fi thermostat connected');
      }),
    };
    if (handlers[form.id]) { ev.preventDefault(); handlers[form.id](); }
  });

  window.addEventListener('hashchange', onRoute);
  window.addEventListener('beforeunload', (ev) => {
    if (S.dirty || S.calDirty) { ev.preventDefault(); ev.returnValue = ''; }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && S.pollTimer) refresh(false); });

  // ---- boot --------------------------------------------------------------------------
  (async function boot() {
    try {
      const s = await api('/api/session');
      if (s.signed_in) start(); else showLogin();
    } catch (e) {
      $('#app').innerHTML = html`<div class="card"><h2>Can't reach the server</h2><p class="muted">Check your internet connection and reload.</p></div>`.s;
    }
  })();
})();
