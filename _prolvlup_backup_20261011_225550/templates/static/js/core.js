/* ============================================================
   core.js — shared helpers, API, modal plumbing, session
   ============================================================ */

/* ---------- tiny DOM/format helpers ---------- */
const $  = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = (n) => (Number(n) || 0).toLocaleString();
const TOUCH = matchMedia('(pointer:coarse)').matches;

/* ---------- toasts ---------- */
function toast(msg, kind = 'ok') {
  const ico = { ok: 'circle-check', err: 'circle-xmark', warn: 'triangle-exclamation', info: 'circle-info' }[kind] || 'circle-check';
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.innerHTML =
    `<i class="fa-solid fa-${ico}"></i><span>${esc(msg)}</span>`;
  const box = $('toasts');
  if (box) box.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateY(-8px)'; setTimeout(() => el.remove(), 260); }, 4000);
}

/* ---------- API ---------- */
async function post(path, payload) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  if (res.status === 401) { location.href = '/'; return {}; }
  try { return await res.json(); } catch (e) { return { status: 'error', error: 'Bad response' }; }
}

async function jget(path) {
  const res = await fetch(path);
  if (res.status === 401) { location.href = '/'; return {}; }
  try { return await res.json(); } catch (e) { return {}; }
}

/* ---------- choice pickers (.choices > .choice) ---------- */
function initPickers() {
  document.querySelectorAll('.choices').forEach(box => {
    if (box.dataset.ready) return;
    box.dataset.ready = '1';
    box.querySelectorAll('.choice').forEach(c => {
      c.addEventListener('click', () => {
        box.querySelectorAll('.choice').forEach(x => x.classList.toggle('on', x === c));
      });
    });
  });
}
function pickedValue(sel, attr) {
  const el = document.querySelector(sel + ' .choice.on');
  return el ? (el.dataset[attr] || '') : '';
}
function resetPicker(sel, value, attr) {
  document.querySelectorAll(sel + ' .choice').forEach(c => {
    c.classList.toggle('on', c.dataset[attr] === value);
  });
}

/* ---------- modal plumbing ---------- */
const OVERLAYS = ['add-overlay', 'tok-overlay', 'up-overlay', 'adm-overlay'];

/* iOS Safari: the visual viewport shrinks when the keyboard opens; expose the
   real height so a sheet's action bar never slips under the keyboard. */
function trackViewport() {
  const vv = window.visualViewport;
  const h = Math.round(vv ? vv.height : window.innerHeight);
  const t = Math.round(vv ? vv.offsetTop : 0);
  document.documentElement.style.setProperty('--vvh', h + 'px');
  document.documentElement.style.setProperty('--vvtop', t + 'px');
}
trackViewport();
window.addEventListener('resize', trackViewport);
window.addEventListener('orientationchange', trackViewport);
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', trackViewport);
  window.visualViewport.addEventListener('scroll', trackViewport);
}

function syncScrollLock() {
  document.body.classList.toggle('modal-open', !!document.querySelector('.overlay.on'));
}

function focusIn(el) { if (el && !TOUCH) setTimeout(() => el.focus(), 60); }

function closeOverlay(id) {
  const el = $(id);
  if (el) el.classList.remove('on');
  syncScrollLock();
}
function closeAllOverlays() { OVERLAYS.forEach(id => closeOverlay(id)); }

/* bring a focused field into view so the pinned action bar stays reachable */
document.addEventListener('focusin', (e) => {
  const t = e.target;
  if (!t || !t.closest || !t.closest('.modal')) return;
  if (!t.matches || !t.matches('input, select, textarea')) return;
  setTimeout(() => {
    try { t.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
    catch (_) { t.scrollIntoView(); }
  }, 350);
});

/* wire click-outside + scroll containment for every overlay */
OVERLAYS.forEach(id => {
  const el = $(id);
  if (!el) return;
  el.addEventListener('mousedown', (e) => { if (e.target === el) closeOverlay(id); });
  el.addEventListener('touchmove', (e) => {
    const t = e.target;
    if (t && t.closest && t.closest('.modal-body, .modal-head, .modal-foot')) return;
    e.preventDefault();
  }, { passive: false });
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeAllOverlays();
});

/* ---------- session ---------- */
let ME = null;

async function loadMe() {
  try {
    const d = await jget('/api/auth/me');
    ME = d.me || null;
    if (!ME) return;
    const name = $('user-name');
    if (name) name.textContent = ME.username;
    const initial = $('user-initial');
    if (initial) initial.textContent = (ME.username || '?').trim().charAt(0) || '?';
    const role = $('user-role');
    if (role) role.textContent = ME.role === 'admin' ? 'Administrator' : 'Operator';
    if (ME.role === 'admin') {
      document.querySelectorAll('.admin-only').forEach(el => el.style.display = '');
    } else {
      if (ME.days_left !== null && ME.days_left !== undefined && ME.days_left <= 2) {
        toast(`Your login expires in ${ME.days_left} day(s) — contact the admin`, 'err');
      }
    }
  } catch (e) { /* server restarting — next tick recovers */ }
}

async function logout() {
  try { await post('/api/auth/logout', {}); } catch (e) {}
  location.href = '/';
}
