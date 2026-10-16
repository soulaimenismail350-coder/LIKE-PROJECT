/* ============================================================
   admin.js — logins & limits (admin only)
   ============================================================ */

function openAdmin() {
  $('adm-overlay').classList.add('on');
  syncScrollLock();
  loadUsers();
}
function closeAdmin() { closeOverlay('adm-overlay'); }

function genPass() {
  const chars = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  let p = '';
  for (let i = 0; i < 12; i++) p += chars[Math.floor(Math.random() * chars.length)];
  $('in-apass').value = p;
}

async function loadUsers() {
  const list = $('userlist');
  const d = await jget('/api/admin/users');
  if (d.status !== 'ok') {
    list.innerHTML = '<p class="msg" style="color:var(--bad)">Could not load logins.</p>';
    return;
  }
  const users = d.users || [];
  if (!users.length) {
    list.innerHTML = '<p class="msg">No logins yet — create the first one above.</p>';
    return;
  }
  list.innerHTML = users.map(u => {
    const when = u.expires ? 'expires ' + new Date(u.expires * 1000).toLocaleDateString() : 'no expiry';
    const st = u.expired ? 'expired' : (u.enabled ? 'active' : 'disabled');
    const stCls = (u.expired || !u.enabled) ? 'bad' : 'ok';
    const days = (u.days_left !== null && u.days_left !== undefined) ? ` · ${u.days_left}d left` : '';
    const initial = (u.username || '?').charAt(0).toUpperCase();
    return `
    <div class="arow">
      <div class="uav">${esc(initial)}</div>
      <div class="uinfo">
        <b>${esc(u.username)}</b>
        <div class="meta">${esc(when)} · ${u.used}/${u.max_accounts} accounts${esc(days)}</div>
      </div>
      <span class="ustat ${stCls}">${st}</span>
      <div class="uacts">
        <button class="mini-btn" onclick="uDays('${esc(u.username)}')"><i class="fa-regular fa-calendar"></i>Days</button>
        <button class="mini-btn" onclick="uPass('${esc(u.username)}')"><i class="fa-solid fa-key"></i>Pass</button>
        <button class="mini-btn" onclick="uQuota('${esc(u.username)}')"><i class="fa-solid fa-layer-group"></i>Limit</button>
        <button class="mini-btn ${u.enabled ? '' : 'on'}" onclick="uToggle('${esc(u.username)}', ${u.enabled ? 'false' : 'true'})">
          ${u.enabled ? 'Disable' : 'Enable'}
        </button>
        <button class="mini-btn" style="color:var(--bad)" onclick="uDelete('${esc(u.username)}')"><i class="fa-solid fa-trash"></i></button>
      </div>
    </div>`;
  }).join('');
}

async function createUser() {
  const username = $('in-auser').value.trim();
  const password = $('in-apass').value.trim();
  if (!username) return toast('Username is required', 'err');
  if (password.length < 4) return toast('Password must be at least 4 characters', 'err');

  const btn = $('adm-go');
  const old = btn.innerHTML;
  btn.innerHTML = '<span class="spinner"></span> Creating…';
  btn.disabled = true;

  try {
    const d = await post('/api/admin/user/create', {
      username,
      password,
      days: parseInt($('in-adays').value, 10) || 0,
      max_accounts: parseInt($('in-aacc').value, 10) || 1,
    });
    if (d.status !== 'ok') { toast(d.error || 'Could not create', 'err'); return; }
    toast(`Login created for ${username}`);
    $('in-auser').value = '';
    $('in-apass').value = '';
    loadUsers();
  } catch (e) {
    toast('Request failed', 'err');
  } finally {
    btn.innerHTML = old;
    btn.disabled = false;
  }
}

async function uAct(username, payload, okMsg) {
  const d = await post('/api/admin/user/update', Object.assign({ username }, payload));
  toast(d.status === 'ok' ? okMsg : (d.error || 'Failed'), d.status === 'ok' ? 'ok' : 'err');
  loadUsers();
}
function uDays(username) {
  const v = prompt(`How many days from now for "${username}"? (0 = never expires)`, '30');
  if (v === null) return;
  const days = parseInt(v, 10);
  if (isNaN(days) || days < 0) return toast('Enter 0 or more', 'err');
  uAct(username, { days }, 'Validity updated');
}
function uPass(username) {
  const v = prompt(`New password for "${username}" (min 4 chars):`);
  if (v === null) return;
  if (!v || v.length < 4) return toast('Password must be at least 4 characters', 'err');
  uAct(username, { password: v }, 'Password changed');
}
function uQuota(username) {
  const v = prompt(`Max accounts allowed for "${username}":`, '5');
  if (v === null) return;
  const n = parseInt(v, 10);
  if (isNaN(n) || n < 1) return toast('Enter 1 or more', 'err');
  uAct(username, { max_accounts: n }, 'Account limit updated');
}
function uToggle(username, enabled) {
  uAct(username, { enabled: enabled === true || enabled === 'true' },
    enabled ? 'Login enabled' : 'Login disabled — sessions revoked');
}
async function uDelete(username) {
  if (!confirm(`Delete the login "${username}"?\n\nTheir bot accounts stay in the system — you keep full control of them.`)) return;
  const d = await post('/api/admin/user/delete', { username });
  toast(d.status === 'ok' ? `Login ${username} deleted` : (d.error || 'Failed'), d.status === 'ok' ? 'ok' : 'err');
  loadUsers();
}
