/* ============================================================
   login.js — sign-in page
   ============================================================ */
const $ = (id) => document.getElementById(id);
let busy = false;

function toggleEye() {
  const p = $('p'), ic = $('eye-ic');
  const show = p.type === 'password';
  p.type = show ? 'text' : 'password';
  ic.className = show ? 'fa-solid fa-eye-slash' : 'fa-solid fa-eye';
}

function showErr(msg) {
  $('err-t').textContent = msg;
  $('err').classList.add('on');
}

async function doLogin(e) {
  e.preventDefault();
  if (busy) return false;
  const username = $('u').value.trim();
  const password = $('p').value;
  if (!username || !password) { showErr('Enter your username and password'); return false; }

  busy = true;
  const btn = $('go');
  const old = btn.innerHTML;
  btn.innerHTML = '<span class="spinner" style="border-top-color:#1a1204;border-color:rgba(0,0,0,.25)"></i> Signing in…';
  btn.disabled = true;
  $('err').classList.remove('on');

  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, hp: ($('hp') || {}).value || '' }),
      credentials: 'same-origin',
    });
    const data = await res.json().catch(() => ({}));
    if (data.status === 'ok') {
      btn.innerHTML = '<i class="fa-solid fa-check"></i> Welcome';
      location.reload();
      return false;
    }
    if (data.code === 'expired' || data.code === 'token') {
      showErr(data.error || 'Session expired — refreshing…');
      setTimeout(() => location.reload(), 1500);
      return false;
    }
    showErr(data.error || 'Could not sign in');
  } catch (err) {
    showErr('Connection failed — is the bot running?');
  } finally {
    busy = false;
    btn.innerHTML = old;
    btn.disabled = false;
  }
  return false;
}
