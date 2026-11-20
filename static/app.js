const $ = id => document.getElementById(id);

function toast(m, ok = true) {
  const t = document.createElement('div');
  t.className = 'toast' + (ok ? '' : ' bad');
  t.textContent = m;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

async function api(url, body, method = 'POST') {
  try {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body) });
    const j = await r.json().catch(() => ({ success: false, error: 'Unexpected response from the server' }));
    if (r.status === 401) { location.href = '/login'; return { success: false, error: 'Please log in' }; }
    return j;
  } catch (e) {
    return { success: false, error: 'Network problem, please try again' };
  }
}

async function act(url, body, okMsg, reload = true, confirmMsg) {
  if (confirmMsg && !confirm(confirmMsg)) return null;
  const j = await api(url, body);
  if (j.success) { if (okMsg) toast(okMsg); if (reload) setTimeout(() => location.reload(), 450); }
  else toast(j.error || 'Something went wrong', false);
  return j;
}

function openM(id) { $(id).classList.add('on'); }
function closeM(id) { $(id).classList.remove('on'); }

function copyTxt(t) {
  const ok = () => toast('Link copied');
  if (navigator.clipboard) navigator.clipboard.writeText(t).then(ok).catch(() => prompt('Copy this link:', t));
  else prompt('Copy this link:', t);
}

/* Tabs: shows one section at a time and marks the active button */
function tabs(sel, attr, onchange) {
  const bs = [...document.querySelectorAll(sel)];
  const secs = [...document.querySelectorAll('[data-sec]')];
  function go(n) {
    bs.forEach(b => b.classList.toggle('on', b.dataset[attr] === n));
    secs.forEach(s => s.classList.toggle('hide', s.dataset.sec !== n));
    if (onchange) onchange(n);
  }
  bs.forEach(b => b.addEventListener('click', e => {
    if (b.tagName === 'BUTTON') {
      e.preventDefault();
      go(b.dataset[attr]);
      history.replaceState(null, '', '#' + b.dataset[attr]);
    }
  }));
  const h = location.hash.slice(1);
  go(bs.some(b => b.dataset[attr] === h) ? h : (bs[0] ? bs[0].dataset[attr] : ''));
}

function initStars(sel, start) {
  let r = start || 0;
  const s = [...document.querySelectorAll(sel + ' span')];
  const paint = () => s.forEach(e => e.classList.toggle('on', +e.dataset.v <= r));
  s.forEach(e => e.addEventListener('click', () => { r = +e.dataset.v; paint(); }));
  paint();
  return () => r;
}

async function sendRating(getR) {
  const r = getR();
  if (!r) return toast('Please select stars first', false);
  const body = { rating: r, text: $('rtext') ? $('rtext').value : '' };
  if ($('rname')) body.name = $('rname').value;
  const j = await api('/api/rating', body);
  if (j.success) { toast('Review saved ⭐'); setTimeout(() => location.reload(), 600); }
  else toast(j.error || 'Something went wrong', false);
}

async function sysStats() {
  const j = await fetch('/api/stats').then(r => r.json()).catch(() => null);
  if (!j || j.cpu === undefined) return;
  for (const k of ['cpu', 'ram', 'disk']) { const e = $('st-' + k); if (e) e.textContent = j[k] + '%'; }
}
if ($('st-cpu')) { sysStats(); setInterval(sysStats, 5000); }
