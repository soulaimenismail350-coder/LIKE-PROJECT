/* ============================================================
   dashboard.js — stats polling, account cards, all actions
   ============================================================ */

let ACC_TYPE = 'guest';
let busy = false;
let ACC_FILTER = 'all';
let ACC_QUERY = '';

/* Privacy of scale: the grid only ever holds a window of accounts, so an
   admin with thousands stays snappy. "Show more" grows the window and the
   server returns the next slice. */
const ACC_STEP = 60;
let ACC_LIMIT = ACC_STEP;
let LAST_TOTAL = 0;   // filtered total reported by the server
let LAST_OWNED = 0;   // every account this login can see

/* uid -> {key, el}: cards are patched in place so a 3s poll never
   rebuilds the grid (rebuilding cancels taps mid-press). */
const CARD_CACHE = new Map();

/* ============================================================
   Helpers / badges
   ============================================================ */
function expProgress(level, currentExp) {
  const base = 100 + Math.floor(level * 42);
  return Math.round(Math.max(4, Math.min(100, (currentExp / base) * 100)));
}

function statusClass(s) {
  const t = String(s || '').toUpperCase();
  if (t.includes('ERROR') || t.includes('FAIL') || t.includes('BAN')) return 'dead';
  if (t.includes('THROTTL') || t.includes('PAUSE') || t.includes('STOP')) return 'paused';
  if (t.includes('MATCH') || t.includes('ONLINE') || t.includes('IDLE') ||
      t.includes('SEARCH') || t.includes('QUEUE') || t.includes('CONNECT') ||
      t.includes('LOADING')) return 'live';
  return '';
}
function statusText(s, active) {
  const t = String(s || '').toUpperCase();
  if (t.includes('THROTTL')) return 'No matches';
  if (t.includes('MATCH') && active > 0) return `In match · ${active}`;
  return (s || 'IDLE').replace(/_/g, ' ');
}
function modeBadge(m) {
  const M = String(m || 'AUTO').toUpperCase();
  if (M === 'BR') return '<span class="badge badge-auto">BR</span>';
  if (M === 'LW') return '<span class="badge badge-auto">LW</span>';
  return '<span class="badge badge-auto">BR→LW</span>';
}
function speedBadge(s) {
  const S = String(s || 'SAFE').toUpperCase();
  if (S === 'RISK') return '<span class="badge badge-risk" title="Risk mode — fast queue, higher exposure">RISK</span>';
  return '<span class="badge badge-safe" title="Safe mode — normal client pace">SAFE</span>';
}

/* ============================================================
   Account card
   ============================================================ */
function accountCard(a) {
  const uid      = esc(a.uid);
  const nick     = esc(a.nickname || 'Player');
  const level    = Number(a.level) || 1;
  const curExp   = Number(a.current_exp) || 0;
  const gained   = Number(a.gained_exp) || 0;
  const matches  = Number(a.matches_played) || 0;
  const active   = Number(a.active_matches) || 0;
  const pct      = expProgress(level, curExp);

  const cap      = Number(a.daily_exp_cap) || 0;
  const capToday = Number(a.daily_exp_today) || 0;
  const capPct   = cap ? Math.min(100, (capToday / cap) * 100) : 0;
  const capMod   = capPct >= 100 ? 'b-hot' : capPct >= 75 ? 'b-warn' : 'b-ok';

  const isPaused = /pause|stop|throttl/i.test(String(a.status || ''));
  const isLive   = Number(a.active_matches) > 0 || /online|search|match/i.test(String(a.status || ''));

  return `
  <div class="acct ${isLive ? 'live' : ''} ${isPaused ? 'paused' : ''}" data-uid="${esc(a.uid)}">
    <div class="acct-top">
      <div class="avatar">${esc(String(Number(a.level) || 1).padStart(2, '0'))}</div>
      <div class="acct-id">
        <h4>${esc(a.nickname || 'Player')}</h4>
        <div class="acct-uid">
          <span class="u">${esc(a.uid)}</span>
          <span class="badge badge-region">${esc(a.region || '—')}</span>
          ${modeBadge(a.match_mode)}
          ${speedBadge(a.speed_mode)}
          ${(ME && ME.role === 'admin' && a.owner) ? `<span class="badge badge-owner"><i class="fa-solid fa-user"></i>${esc(a.owner)}</span>` : ''}
        </div>
      </div>
      <span class="status ${statusClass(a.status)}">${esc(statusText(a.status, Number(a.active_matches) || 0))}</span>
    </div>

    <div class="acct-body">
      <div>
        <div class="bar-row"><span>Level ${esc(String(Number(a.level) || 1))}</span>
          <b>${num(a.current_exp)} EXP${Number(a.gained_exp) > 0 ? `<span class="gained">+${num(a.gained_exp)}</span>` : ''}</b></div>
        <div class="bar b-ok"><span style="width:${expProgress(Number(a.level) || 1, Number(a.current_exp) || 0)}%"></span></div>
      </div>

      <div class="metrics">
        <div class="metric"><b>${num(a.matches_played)}</b><span>Matches</span></div>
        <div class="metric"><b>${num(a.likes || 0)}</b><span>Likes</span></div>
        <div class="metric"><b>${num(a.active_matches)}</b><span>Active</span></div>
      </div>

      ${Number(a.daily_exp_cap) ? `
      <div>
        <div class="bar-row"><span>Daily EXP cap</span><b>${num(a.daily_exp_today)} / ${num(a.daily_exp_cap)}</b></div>
        <div class="bar ${capMod}"><span style="width:${Math.min(100, ((Number(a.daily_exp_today) || 0) / (Number(a.daily_exp_cap) || 1)) * 100)}%"></span></div>
      </div>` : ''}
    </div>

    <div class="acct-foot">
      <span class="when">${a.last_match_time ? 'Last ' + esc(a.last_match_time) : 'No match yet'}</span>
      <div class="grow"></div>
      <button class="ibtn" title="Restart worker" onclick="restartOne('${esc(a.uid)}')"><i class="fa-solid fa-power-off"></i></button>
      <button class="ibtn" title="Pause / resume" onclick="togglePause('${esc(a.uid)}')"><i class="fa-solid fa-${isPaused ? 'play' : 'pause'}"></i></button>
      <button class="ibtn danger" title="Delete account" onclick="deleteAccount('${esc(a.uid)}')"><i class="fa-solid fa-trash"></i></button>
    </div>
  </div>`;
}

/* The server already filters + pages the accounts; the client renders the
   window it was handed and offers "show more". This keeps a 10k-account
   panel as snappy as a 10-account one. */
function renderAccounts(accounts, total, owned) {
  const grid = $('acc-grid');
  const empty = $('acc-empty');
  const more = $('acc-more');

  $('acc-count').textContent = (total || 0) + ((total || 0) === 1 ? ' account' : ' accounts');

  if (!accounts.length) {
    CARD_CACHE.clear();
    grid.innerHTML = '';
    if (more) more.innerHTML = '';
    empty.style.display = 'block';
    empty.querySelector('h4').textContent = owned ? 'No matching accounts' : 'No accounts yet';
    empty.querySelector('p').textContent = owned
      ? 'Try a different filter or search term.'
      : 'Add a guest account with its UID and password, or paste a platform-4 access token. Each account keeps a persistent device fingerprint.';
    return;
  }
  empty.style.display = 'none';

  const ids = accounts.map(a => String(a.uid ?? a.token ?? ''));

  for (const [uid, hit] of CARD_CACHE) {
    if (!ids.includes(uid)) {
      if (hit.el && hit.el.parentNode) hit.el.remove();
      CARD_CACHE.delete(uid);
    }
  }

  accounts.forEach((a, i) => {
    const uid = ids[i];
    const html = accountCard(a);
    const hit = CARD_CACHE.get(uid);
    if (hit && hit.key === html) return;
    const holder = document.createElement('div');
    holder.innerHTML = html;
    const node = holder.firstElementChild;
    if (hit && hit.el && hit.el.parentNode === grid) {
      grid.replaceChild(node, hit.el);
    } else {
      grid.appendChild(node);
    }
    CARD_CACHE.set(uid, { key: html, el: node });
  });

  const want = ids.map(uid => (CARD_CACHE.get(uid) || {}).el).filter(Boolean);
  const have = Array.from(grid.children);
  if (have.length !== want.length || have.some((el, i) => el !== want[i])) {
    want.forEach(el => grid.appendChild(el));
  }

  if (more) {
    const remaining = Math.max(0, (total || 0) - accounts.length);
    more.innerHTML = remaining
      ? `<button class="btn btn-ghost" onclick="showMore()"><i class="fa-solid fa-chevron-down"></i> Show ${Math.min(ACC_STEP, remaining)} more <span class="muted">(${accounts.length} of ${total})</span></button>`
      : '';
  }
}

function showMore() {
  ACC_LIMIT += ACC_STEP;
  fetchStats();
}

/* ============================================================
   Poll
   ============================================================ */
async function fetchStats() {
  try {
    const url = '/api/stats?limit=' + ACC_LIMIT + '&offset=0'
      + '&f=' + encodeURIComponent(ACC_FILTER)
      + '&q=' + encodeURIComponent(ACC_QUERY);
    const data = await jget(url);
    $('st-accounts').textContent = num(data.total_accounts);
    $('st-max').textContent = num(data.max_accounts);
    $('st-matches').textContent = num(data.total_matches);
    $('st-exp').textContent = num(data.total_gained_exp);
    $('st-cap').textContent = num(data.daily_exp_cap);
    $('slot-used').textContent = num(data.total_accounts);
    $('slot-max').textContent = num(data.max_accounts);
    LAST_TOTAL = Number(data.accounts_total ?? (data.accounts || []).length);
    LAST_OWNED = Number(data.total_accounts ?? 0);
    renderAccounts(data.accounts || [], LAST_TOTAL, LAST_OWNED);
  } catch (e) { /* server restarting — next tick recovers */ }
}

function tickClock() {
  const c = $('clock');
  if (c) c.textContent = new Date().toLocaleTimeString('en-GB');
}

/* ============================================================
   Add account modal
   ============================================================ */
function openAdd() { $('add-overlay').classList.add('on'); syncScrollLock(); focusIn($('in-uid')); }
function closeAdd() { closeOverlay('add-overlay'); }

function pickType(t) {
  ACC_TYPE = t;
  $('tab-guest').classList.toggle('on', t === 'guest');
  $('tab-token').classList.toggle('on', t === 'token');
  $('f-guest').style.display = t === 'guest' ? 'grid' : 'none';
  $('f-token').style.display = t === 'token' ? 'grid' : 'none';
  focusIn(t === 'guest' ? $('in-uid') : $('in-token'));
}

async function submitAdd(e) {
  e.preventDefault();
  if (busy) return;

  const payload = {
    target_level: parseInt($('in-tlvl').value, 10) || 0,
    target_matches: parseInt($('in-tmatch').value, 10) || 0,
    match_mode: pickedValue('#modes', 'm') || 'AUTO',
    speed_mode: pickedValue('#speeds', 's') || 'SAFE',
  };

  if (ACC_TYPE === 'guest') {
    payload.uid = $('in-uid').value.trim();
    payload.password = $('in-pass').value.trim();
    if (!payload.uid || !payload.password) return toast('UID and password are both required', 'err');
  } else {
    payload.token = $('in-token').value.trim();
    if (!payload.token) return toast('Token is required', 'err');
  }

  busy = true;
  const btn = $('add-submit');
  const old = btn.innerHTML;
  btn.innerHTML = '<span class="spinner"></span> Logging in…';
  btn.disabled = true;

  try {
    const data = await post('/api/account/add', payload);
    if (data.status === 'ok') {
      toast('Account added — worker started');
      closeAdd();
      $('add-form').reset();
      resetPicker('#modes', 'AUTO', 'm');
      resetPicker('#speeds', 'SAFE', 's');
      fetchStats();
    } else {
      toast(data.error || 'Could not add account', 'err');
    }
  } catch (e) {
    toast('Request failed', 'err');
  } finally {
    busy = false;
    btn.innerHTML = old;
    btn.disabled = false;
  }
}

/* ============================================================
   Token generator
   ============================================================ */
const OAUTH_BASE =
  'https://auth.garena.com/universal/oauth?platform=__P__&response_type=code' +
  '&locale=en-SG&client_id=100067' +
  '&redirect_uri=https://api.ff.garena.co.id/auth/auth/callback_n' +
  '?site=https://api-discountstore.kiosgamer.gameid.garena.co.id/oauth/callback_redirect/';

let TOK_VALUE = '';

function wireProviders() {
  document.querySelectorAll('.prov').forEach(a => {
    a.href = OAUTH_BASE.replace('__P__', a.dataset.p);
    a.target = '_blank';
    a.rel = 'noopener';
  });
}

function openTok() { $('tok-overlay').classList.add('on'); syncScrollLock(); focusIn($('in-eat')); }
function closeTok() { closeOverlay('tok-overlay'); }

async function genToken() {
  const raw = $('in-eat').value.trim();
  if (!raw) return toast('Paste the callback URL or the eat value first', 'err');

  const btn = $('tok-go');
  const old = btn.innerHTML;
  btn.innerHTML = '<span class="spinner"></span> Generating…';
  btn.disabled = true;

  try {
    const d = await post('/api/token/generate', { token: raw });
    if (d.status !== 'ok') { toast(d.error || 'Generation failed', 'err'); return; }
    TOK_VALUE = d.access_token;
    $('tk-nick').textContent = d.nickname || '—';
    $('tk-uid').textContent = d.account_id || '—';
    $('tk-reg').textContent = d.region || '—';
    $('tk-tok').textContent = TOK_VALUE;
    $('tok-out').classList.add('on');
    toast('Token generated');
  } catch (e) {
    toast('Request failed', 'err');
  } finally {
    btn.innerHTML = old;
    btn.disabled = false;
  }
}

async function copyTok() {
  if (!TOK_VALUE) return;
  try { await navigator.clipboard.writeText(TOK_VALUE); toast('Token copied'); }
  catch (e) { toast('Copy failed — select it manually', 'err'); }
}

function useTok() {
  if (!TOK_VALUE) return;
  closeTok();
  openAdd();
  pickType('token');
  $('in-token').value = TOK_VALUE;
  toast('Token pasted — set targets and start');
}

/* ============================================================
   Upload modal
   ============================================================ */
function openUpload() { $('up-overlay').classList.add('on'); syncScrollLock(); }
function closeUpload() { closeOverlay('up-overlay'); }

function pickFile(ev) {
  const f = ev.target.files && ev.target.files[0];
  if (!f) return;
  const r = new FileReader();
  r.onload = () => { $('in-lines').value = r.result; };
  r.readAsText(f);
}

function parseLines(text) {
  const out = [];
  for (const line of String(text || '').split(/\r?\n/)) {
    const m = line.trim().match(/^(\S+)[:\s]+(\S+)$/);
    if (m) out.push({ uid: m[1], password: m[2] });
  }
  return out;
}

async function submitUpload() {
  const items = parseLines($('in-lines').value);
  if (!items.length) return toast('No valid lines found (UID:Password)', 'err');

  const btn = $('up-go');
  const old = btn.innerHTML;
  btn.innerHTML = '<span class="spinner"></span> Uploading…';
  btn.disabled = true;

  try {
    const d = await post('/api/account/upload', {
      accounts: items,
      target_level: parseInt($('in-up-tlvl').value, 10) || 0,
      target_matches: parseInt($('in-up-tmatch').value, 10) || 0,
      match_mode: 'AUTO',
      speed_mode: pickedValue('#up-speeds', 's') || 'SAFE',
    });
    if (d.status !== 'ok') { toast(d.error || 'Upload failed', 'err'); return; }
    const details = (d.skipped_details || []).slice(0, 3).join('  •  ');
    toast(`Uploaded — ${d.added} added, ${d.skipped} skipped` + (details ? `  •  ${details}` : ''), d.added ? 'ok' : 'err');
    if (d.added) {
      closeUpload();
      $('in-lines').value = '';
      $('in-file').value = '';
      resetPicker('#up-speeds', 'SAFE', 's');
    }
    fetchStats();
  } catch (e) {
    toast('Request failed', 'err');
  } finally {
    btn.innerHTML = old;
    btn.disabled = false;
  }
}

/* ============================================================
   Account actions
   ============================================================ */
async function deleteAccount(uid) {
  if (!confirm(`Remove account ${uid}?\n\nIts device profile is kept for next time.`)) return;
  const d = await post('/api/account/delete', { uid });
  toast(d.status === 'ok' ? 'Account removed' : (d.error || 'Failed'), d.status === 'ok' ? 'ok' : 'err');
  fetchStats();
}
async function togglePause(uid) {
  const d = await post('/api/account/toggle_pause', { uid });
  toast(d.status === 'ok' ? (d.paused ? 'Paused' : 'Resumed') : (d.error || 'Failed'), d.status === 'ok' ? 'ok' : 'err');
  fetchStats();
}
async function restartOne(uid) {
  const d = await post('/api/account/restart', { uid });
  toast(d.status === 'ok' ? 'Worker restarted' : (d.error || 'Failed'), d.status === 'ok' ? 'ok' : 'err');
}
async function deleteAll() {
  if (!confirm('Remove ALL accounts? This cannot be undone.')) return;
  const d = await post('/api/account/delete_all', {});
  toast(d.status === 'ok' ? 'All accounts removed' : (d.error || 'Failed'), d.status === 'ok' ? 'ok' : 'err');
  fetchStats();
}
async function restartAll() {
  const d = await post('/api/account/restart_all', {});
  toast(d.status === 'ok' ? 'All workers restarted' : (d.error || 'Failed'), d.status === 'ok' ? 'ok' : 'err');
  fetchStats();
}

/* ============================================================
   Nav + filters
   ============================================================ */
function wireNav() {
  document.querySelectorAll('[data-scroll]').forEach(el => {
    el.addEventListener('click', (e) => {
      e.preventDefault();
      const target = document.querySelector(el.dataset.scroll);
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      document.querySelectorAll('.side-nav a, .mobile-nav button').forEach(a =>
        a.classList.toggle('on', a.dataset.scroll === el.dataset.scroll));
    });
  });
  document.querySelectorAll('[data-open]').forEach(el => {
    el.addEventListener('click', () => {
      const open = { add: openAdd, token: openTok, upload: openUpload,
                     admin: () => window.openAdmin && openAdmin() }[el.dataset.open];
      if (open) open();
    });
  });
  document.querySelectorAll('.filters button').forEach(b => {
    b.addEventListener('click', () => {
      ACC_FILTER = b.dataset.f;
      ACC_LIMIT = ACC_STEP;          // new filter → start the window over
      document.querySelectorAll('.filters button').forEach(x => x.classList.toggle('on', x === b));
      fetchStats();
    });
  });
  const search = $('acc-search');
  if (search) {
    let searchTimer = null;
    search.addEventListener('input', () => {
      ACC_QUERY = search.value;
      ACC_LIMIT = ACC_STEP;          // new search → start the window over
      clearTimeout(searchTimer);
      searchTimer = setTimeout(fetchStats, 220);
    });
  }
}

/* ============================================================
   Boot
   ============================================================ */
if (document.body.dataset.ready !== '1') {
  document.body.dataset.ready = '1';
  initPickers();
  wireProviders();
  wireNav();
  tickClock();
  setInterval(tickClock, 1000);
  loadMe();
  fetchStats();
  setInterval(fetchStats, 3000);
}
