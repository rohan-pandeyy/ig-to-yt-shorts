'use strict';

const $ = (id) => document.getElementById(id);
const PRESETS = [15, 30, 60, 120, 180, 360, 720, 1440];
const ACTIVE = ['downloading', 'downloaded', 'uploading'];
const STATUS_LABEL = {
  pending: 'Pending', downloading: 'Downloading', downloaded: 'Downloaded',
  uploading: 'Uploading', uploaded: 'Uploaded', failed: 'Failed', needs_review: 'Check channel',
};

let state = null;
let clockOffset = 0;        // server time - phone time, in ms
let pollTimer = null;
let lastRendered = '';
let dialogReelId = null;
let offline = false;

// ---------- helpers ----------

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) el.append(c);
  return el;
}

async function api(method, path, body) {
  const opts = { method, credentials: 'same-origin', headers: {} };
  if (method !== 'GET') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body || {});
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    location.href = '/login';
    throw new Error('Signed out');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

let toastTimer = null;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
}

function serverNow() { return Date.now() + clockOffset; }

function fmtMinutes(m) {
  if (m < 60) return `${m} min`;
  const hrs = Math.floor(m / 60), rest = m % 60;
  return rest ? `${hrs} h ${rest} min` : `${hrs} h`;
}

function fmtWhen(iso) {
  const d = new Date(iso);
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const today = new Date(serverNow()).toDateString() === d.toDateString();
  return today ? time : `${d.toLocaleDateString([], { weekday: 'short' })} ${time}`;
}

function fmtRelative(iso) {
  const mins = Math.round((Date.parse(iso) - serverNow()) / 60000);
  if (mins <= 0) return 'due now';
  return `in ${fmtMinutes(mins)}`;
}

function reelTitle(r) {
  return r.title_override || r.title || `Reel ${r.shortcode}`;
}

async function act(promise, okMsg) {
  try {
    const data = await promise;
    if (okMsg) toast(typeof okMsg === 'function' ? okMsg(data) : okMsg);
    await refresh();
  } catch (e) {
    toast(e.message);
  }
}

// ---------- rendering ----------

function renderSchedule() {
  const s = state.schedule;
  const alertEl = $('alert');
  const alertText = offline ? "Can't reach the PC. Is the dashboard still running?" : s.alert;
  alertEl.hidden = !alertText;
  alertEl.textContent = alertText || '';

  const hasPending = state.reels.some((r) => r.status === 'pending');
  let next;
  if (s.pause_reason) next = 'Paused';
  else if (!s.next_scheduled_at) next = 'Starting…';
  else next = `${fmtWhen(s.next_scheduled_at)} · ${fmtRelative(s.next_scheduled_at)}`;
  if (!s.pause_reason && !hasPending) next += ' · queue empty';
  $('next-run').textContent = next;

  const select = $('interval');
  if (document.activeElement !== select) {
    const current = s.interval_minutes;
    const values = PRESETS.includes(current) ? PRESETS : [...PRESETS, current].sort((a, b) => a - b);
    select.replaceChildren(
      ...values.map((m) => h('option', { value: m, selected: m === current }, fmtMinutes(m))),
      h('option', { value: 'custom' }, 'Custom…'),
    );
  }

  const working = $('now-working');
  const cur = state.reels.find((r) => r.id === state.current_reel_id);
  working.hidden = !cur;
  if (cur) {
    const pct = cur.status === 'uploading' && cur.progress != null ? ` ${Math.round(cur.progress * 100)}%` : '';
    working.textContent = `Working on: ${reelTitle(cur)} (${STATUS_LABEL[cur.status] || cur.status}${pct})`;
  }
}

function reelCard(r, tag) {
  const thumb = r.has_thumbnail
    ? h('img', { class: 'thumb', src: `/thumb/${r.id}`, alt: '', loading: 'lazy' })
    : h('div', { class: 'thumb', 'aria-hidden': 'true' });

  const isActive = ACTIVE.includes(r.status);
  const badgeClass = isActive ? 'active' : r.status;
  const priority = r.status === 'pending' && r.priority_seq != null;

  const meta = [h('a', { href: r.url, target: '_blank', rel: 'noopener noreferrer' }, `instagram.com/…/${r.shortcode}`)];
  if (r.youtube_id) {
    meta.push(' · ', h('a', { href: `https://youtube.com/shorts/${r.youtube_id}`, target: '_blank', rel: 'noopener noreferrer' }, 'Open on YouTube'));
  }

  const buttons = [];
  if (r.status === 'pending' || r.status === 'failed') {
    buttons.push(h('button', { type: 'button', class: priority ? '' : 'primary', onclick: () => openNowDialog(r) },
      priority ? 'Edit title' : 'Upload now'));
  }
  if (r.status === 'failed') {
    buttons.push(h('button', { type: 'button', onclick: () => act(api('POST', `/api/reels/${r.id}/retry`), 'Back in the queue') }, 'Retry'));
  }
  if (r.status === 'needs_review') {
    buttons.push(
      h('button', { type: 'button', class: 'primary', onclick: () => act(api('POST', `/api/reels/${r.id}/done`), 'Marked as uploaded') }, "It's on YouTube"),
      h('button', {
        type: 'button',
        onclick: () => {
          if (confirm('Only retry if the video is NOT on your channel, or it will be uploaded twice. Retry?')) {
            act(api('POST', `/api/reels/${r.id}/retry`), 'Back in the queue');
          }
        },
      }, 'Not there, retry'),
    );
  }
  if (['pending', 'failed', 'needs_review'].includes(r.status)) {
    buttons.push(h('button', {
      type: 'button', class: 'danger',
      onclick: () => {
        if (confirm(`Remove "${reelTitle(r)}" from the queue?`)) {
          act(api('DELETE', `/api/reels/${r.id}`), 'Removed');
        }
      },
    }, 'Remove'));
  }

  return h('li', { class: 'reel' },
    thumb,
    h('div', { class: 'reel-body' },
      h('div', { class: 'reel-top' },
        h('span', { class: `badge ${badgeClass}` }, priority ? 'Upload now' : (STATUS_LABEL[r.status] || r.status)),
        Boolean(tag) && h('span', { class: 'tag' }, tag),
        Boolean(r.title_override) && h('span', { class: 'tag' }, 'custom title'),
      ),
      h('div', { class: 'reel-title' }, reelTitle(r)),
      h('div', { class: 'reel-meta' }, ...meta),
      r.status === 'uploading' && h('progress', { max: '1', value: r.progress ?? 0 }),
      Boolean(r.error) && h('div', { class: 'reel-error' }, r.error),
      r.status === 'needs_review' && h('div', { class: 'reel-meta' },
        'Check ', h('a', { href: 'https://studio.youtube.com', target: '_blank', rel: 'noopener noreferrer' }, 'YouTube Studio'), ' first.'),
      buttons.length > 0 && h('div', { class: 'actions' }, ...buttons),
    ),
  );
}

function renderLists() {
  const reels = state.reels;
  const active = reels.filter((r) => ACTIVE.includes(r.status));
  const pending = reels.filter((r) => r.status === 'pending');
  const prio = pending.filter((r) => r.priority_seq != null).sort((a, b) => a.priority_seq - b.priority_seq);
  const fifo = pending.filter((r) => r.priority_seq == null).sort((a, b) => a.id - b.id);
  const attention = reels.filter((r) => r.status === 'failed' || r.status === 'needs_review');
  const done = reels.filter((r) => r.status === 'uploaded')
    .sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''));

  const queueItems = [
    ...active.map((r) => reelCard(r)),
    ...prio.map((r, i) => reelCard(r, i === 0 && !active.length ? 'Next' : null)),
    ...fifo.map((r, i) => reelCard(r, i === 0 ? 'Next scheduled' : null)),
  ];
  $('queue').replaceChildren(...queueItems);
  $('queue-count').textContent = queueItems.length ? `(${queueItems.length})` : '';
  $('queue-empty').hidden = queueItems.length > 0;

  $('attention').replaceChildren(...attention.map((r) => reelCard(r)));
  $('attention-count').textContent = `(${attention.length})`;
  $('attention-section').hidden = attention.length === 0;

  $('done').replaceChildren(...done.map((r) => reelCard(r)));
  $('done-count').textContent = `(${done.length})`;
}

function render() {
  renderSchedule();
  // Rebuild the lists only when something changed, so taps and scrolling aren't disturbed.
  const key = JSON.stringify([state.reels, state.current_reel_id]);
  if (key !== lastRendered) {
    lastRendered = key;
    renderLists();
  }
}

// ---------- polling ----------

async function refresh() {
  clearTimeout(pollTimer);
  try {
    const data = await api('GET', '/api/state');
    clockOffset = Date.parse(data.server_time) - Date.now();
    state = data;
    offline = false;
  } catch (e) {
    offline = true;
  }
  if (state) render();
  if (!document.hidden) {
    const busy = state && state.current_reel_id != null;
    pollTimer = setTimeout(refresh, busy ? 2000 : 5000);
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) clearTimeout(pollTimer);
  else refresh();
});

// ---------- add reels ----------

$('add-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const box = $('urls');
  const button = ev.submitter || ev.target.querySelector('button');
  button.disabled = true;
  try {
    const { results } = await api('POST', '/api/reels', { urls: box.value });
    $('add-results').replaceChildren(...results.map((r) => h('li', { class: r.ok ? 'ok' : 'bad' },
      r.ok ? `Added: ${r.url}` : r.error)));
    const added = results.filter((r) => r.ok).length;
    if (added) box.value = results.filter((r) => !r.ok).map((r) => r.url).join('\n');
    toast(added ? `Added ${added} reel${added > 1 ? 's' : ''}` : 'Nothing added');
    await refresh();
  } catch (e) {
    toast(e.message);
  } finally {
    button.disabled = false;
  }
});

// ---------- paste from clipboard ----------

const MAX_PASTE_RESULTS = 5;

function instagramLinks(text) {
  return (text.match(/https?:\/\/\S+/gi) || []).filter((u) => /^https?:\/\/([a-z0-9-]+\.)*instagram\.com\//i.test(u));
}

function showPasteResult(ok, text) {
  const list = $('paste-results');
  list.prepend(h('li', { class: ok ? 'ok' : 'bad' }, text));
  while (list.children.length > MAX_PASTE_RESULTS) list.lastChild.remove();
}

async function addPasted(text) {
  const links = instagramLinks(text || '');
  if (!links.length) {
    showPasteResult(false, text && text.trim()
      ? "The copied text isn't an Instagram link. Copy the reel's link and try again."
      : 'Nothing is copied. Copy a reel link first.');
    return;
  }
  const { results } = await api('POST', '/api/reels', { urls: links });
  for (const r of results) showPasteResult(r.ok, r.ok ? `Added: ${r.url}` : r.error);
  if (results.some((r) => r.ok)) await refresh();
}

function showPasteFallback() {
  $('paste-fallback').hidden = false;
  $('paste-box').focus();
}

$('paste-btn').addEventListener('click', async () => {
  // Reading the clipboard directly needs HTTPS or localhost; over the Wi-Fi address
  // (http://192.168...) browsers don't allow it, so fall back to a paste box.
  if (!(window.isSecureContext && navigator.clipboard && navigator.clipboard.readText)) {
    showPasteFallback();
    return;
  }
  const button = $('paste-btn');
  let text;
  try {
    text = await navigator.clipboard.readText();
  } catch (e) {
    showPasteFallback();              // permission denied or not supported
    return;
  }
  button.disabled = true;
  try {
    await addPasted(text);
  } catch (e) {
    toast(e.message);
  } finally {
    button.disabled = false;
  }
});

$('paste-box').addEventListener('paste', async (ev) => {
  ev.preventDefault();
  const text = (ev.clipboardData || window.clipboardData).getData('text');
  const box = ev.target;
  box.disabled = true;
  try {
    await addPasted(text);
  } catch (e) {
    toast(e.message);
  } finally {
    box.value = '';
    box.disabled = false;
  }
});

// ---------- interval ----------

$('interval').addEventListener('change', async (ev) => {
  let value = ev.target.value;
  if (value === 'custom') {
    const [min, max] = state.interval_limits;
    const input = prompt(`Upload one reel every how many minutes? (${min}–${max})`, state.schedule.interval_minutes);
    if (input === null) { lastRendered = ''; renderSchedule(); return; }
    value = input.trim();
  }
  ev.target.blur();
  await act(api('POST', '/api/settings', { interval_minutes: Number(value) }),
    (d) => `One upload every ${fmtMinutes(d.schedule.interval_minutes)}`);
});

// ---------- Upload Now dialog ----------

function openNowDialog(r) {
  dialogReelId = r.id;
  $('now-reel').textContent = reelTitle(r);
  $('now-title').value = r.title_override || '';
  $('now-auto-title').textContent = r.title ? `"${r.title}"` : 'the Instagram caption (read when the reel downloads)';
  $('now-dialog').showModal();
}

$('now-cancel').addEventListener('click', () => $('now-dialog').close());

$('now-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const title = $('now-title').value;
  $('now-dialog').close();
  await act(api('POST', `/api/reels/${dialogReelId}/upload-now`, { title }),
    (d) => (d.worker_busy ? 'Queued: starts right after the current upload' : 'Starting now'));
});

refresh();
