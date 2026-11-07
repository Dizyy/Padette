// Padette controller: draws the layout, turns touches into a controller state and sends it
// to the PC (protocol "pad" message) on every change and at least every 250 ms.
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const RADIUS_PX = 60;      // how far the stick knob travels
  const DEAD_ZONE = 0.1;     // stick movement ignored near the centre
  const KEEPALIVE_MS = 250;  // resend even without changes (the PC releases after 1 s silence)
  const PLAYER_COLORS = { 1: '#9cc5a1', 2: '#7fb8e0', 3: '#e3b866', 4: '#b5a3e0' };
  const PLAYER_INK = { 1: '#16241a', 2: '#10202c', 3: '#2a200c', 4: '#1f1830' };
  // Button bits (PROTOCOL.md "Button bits").
  const BITS = {
    a: 0x1000, b: 0x2000, x: 0x4000, y: 0x8000, lb: 0x0100, rb: 0x0200,
    view: 0x0020, menu: 0x0010, guide: 0x0400, l3: 0x0040, r3: 0x0080,
  };
  const DPAD = { up: 0x0001, down: 0x0002, left: 0x0004, right: 0x0008 };

  const padEl = $('pad');
  const badge = $('player');
  const overlay = $('overlay');
  const overlayText = $('overlay-text');
  const debugEl = $('debug');

  let layout = Layout.load();
  let editing = false;
  let connected = false;
  // touch id -> { ctl, el, ... } for every finger on a control
  const touches = new Map();
  const sticks = { stick_left: [0, 0], stick_right: [0, 0] };
  let lastSent = null;
  let lastSentAt = 0;

  // ---------- drawing ----------

  function render(next) {
    if (next) layout = next;
    padEl.textContent = '';
    for (const ctl of layout.controls) {
      if (!ctl.on) continue;
      const el = document.createElement('div');
      el.className = `ctl ctl-${ctl.kind}`;
      el.dataset.id = ctl.id;
      el.style.left = `${ctl.x * 100}%`;
      el.style.top = `${ctl.y * 100}%`;
      el.style.width = `${ctl.w * 100}%`;
      el.style.height = `${ctl.h * 100}%`;
      if (ctl.kind === 'dpad') {
        el.innerHTML = '<i data-dir="up">▲</i><i data-dir="left">◀</i><i data-dir="right">▶</i><i data-dir="down">▼</i>';
      } else if (ctl.kind === 'stickzone') {
        el.innerHTML = `<span class="zone-label">${ctl.label}</span><span class="base"></span><span class="knob"></span>`;
      } else {
        el.innerHTML = `<span class="cap">${ctl.label}</span>`;
        if (ctl.id === 'a') el.classList.add('primary');
      }
      padEl.append(el);
    }
  }

  // ---------- touches -> state ----------

  function stickMove(t, x, y) {
    let dx = x - t.cx;
    let dy = y - t.cy;
    const dist = Math.hypot(dx, dy);
    if (dist > RADIUS_PX) { dx *= RADIUS_PX / dist; dy *= RADIUS_PX / dist; }
    t.knob.style.transform = `translate(${dx}px, ${dy}px)`;
    const mag = Math.min(1, dist / RADIUS_PX);
    const scaled = mag < DEAD_ZONE ? 0 : (mag - DEAD_ZONE) / (1 - DEAD_ZONE);
    const len = Math.hypot(dx, dy) || 1;
    // Up on the screen is +y for the controller.
    sticks[t.ctl.id] = [round((dx / len) * scaled), round((-dy / len) * scaled)];
  }

  const round = (v) => Math.round(v * 1000) / 1000;

  function dpadDirs(t, x, y) {
    const r = t.el.getBoundingClientRect();
    const dx = x - (r.left + r.width / 2);
    const dy = y - (r.top + r.height / 2);
    const dirs = new Set();
    if (Math.hypot(dx, dy) < Math.min(r.width, r.height) * 0.12) return dirs; // centre: nothing
    const angle = Math.atan2(-dy, dx) * 180 / Math.PI; // 0 = right, 90 = up
    if (angle > 22.5 && angle < 157.5) dirs.add('up');
    if (angle < -22.5 && angle > -157.5) dirs.add('down');
    if (Math.abs(angle) < 67.5) dirs.add('right');
    if (Math.abs(angle) > 112.5) dirs.add('left');
    return dirs;
  }

  // A touch the browser never ended (lost touchend) would keep its button held, and the
  // keepalive would repeat it forever: drop every finger the browser no longer reports.
  function dropLostTouches(e) {
    const live = new Set([...e.touches].map((t) => t.identifier));
    for (const id of [...touches.keys()]) if (!live.has(id)) release(id);
  }

  function onStart(e) {
    if (editing) return;
    e.preventDefault();
    dropLostTouches(e);
    goFullscreen();
    for (const touch of e.changedTouches) {
      const el = document.elementFromPoint(touch.clientX, touch.clientY)?.closest('.ctl');
      if (!el) continue;
      const ctl = layout.controls.find((c) => c.id === el.dataset.id);
      const t = { ctl, el };
      if (ctl.kind === 'stickzone') {
        // Floating stick: its centre is wherever the thumb lands.
        const r = el.getBoundingClientRect();
        t.cx = touch.clientX; t.cy = touch.clientY;
        t.knob = el.querySelector('.knob');
        const base = el.querySelector('.base');
        for (const part of [base, t.knob]) {
          part.style.left = `${touch.clientX - r.left}px`;
          part.style.top = `${touch.clientY - r.top}px`;
        }
        el.classList.add('active');
        stickMove(t, touch.clientX, touch.clientY);
      } else if (ctl.kind === 'dpad') {
        t.dirs = dpadDirs(t, touch.clientX, touch.clientY);
        showDpad(el);
      }
      el.classList.add('held');
      touches.set(touch.identifier, t);
    }
    flush(); // presses go out at once: a tap shorter than a frame must not get lost
  }

  function onMove(e) {
    if (editing) return;
    e.preventDefault();
    dropLostTouches(e);
    for (const touch of e.changedTouches) {
      const t = touches.get(touch.identifier);
      if (!t) continue;
      if (t.ctl.kind === 'stickzone') stickMove(t, touch.clientX, touch.clientY);
      else if (t.ctl.kind === 'dpad') { t.dirs = dpadDirs(t, touch.clientX, touch.clientY); showDpad(t.el); }
    }
    pump();
  }

  // touchcancel (a notification banner, a system swipe) releases exactly like touchend.
  function onEnd(e) {
    for (const touch of e.changedTouches) release(touch.identifier);
    flush();
  }

  function release(id) {
    const t = touches.get(id);
    if (!t) return;
    touches.delete(id);
    if (t.ctl.kind === 'stickzone') {
      sticks[t.ctl.id] = [0, 0];
      t.knob.style.transform = '';
      t.el.classList.remove('active');
    }
    if (![...touches.values()].some((o) => o.el === t.el)) t.el.classList.remove('held');
    if (t.ctl.kind === 'dpad') showDpad(t.el);
  }

  function releaseAll() {
    for (const id of [...touches.keys()]) release(id);
    sticks.stick_left = [0, 0];
    sticks.stick_right = [0, 0];
  }

  function showDpad(el) {
    const dirs = new Set();
    for (const t of touches.values()) if (t.el === el && t.dirs) for (const d of t.dirs) dirs.add(d);
    for (const i of el.querySelectorAll('i')) i.classList.toggle('on', dirs.has(i.dataset.dir));
  }

  function state() {
    let b = 0;
    let lt = 0;
    let rt = 0;
    for (const t of touches.values()) {
      const id = t.ctl.id;
      if (BITS[id]) b |= BITS[id];
      else if (id === 'lt') lt = 1;
      else if (id === 'rt') rt = 1;
      else if (t.dirs) for (const d of t.dirs) b |= DPAD[d];
    }
    const [lx, ly] = sticks.stick_left;
    const [rx, ry] = sticks.stick_right;
    return { b, lx, ly, rx, ry, lt, rt };
  }

  const ZERO = { b: 0, lx: 0, ly: 0, rx: 0, ry: 0, lt: 0, rt: 0 };
  const same = (p, q) => p && q && Object.keys(ZERO).every((k) => p[k] === q[k]);

  // ---------- sending ----------

  let pumpQueued = false;
  function pump() {
    if (!pumpQueued) { pumpQueued = true; requestAnimationFrame(flush); }
  }

  // At most once per display frame (≤ 60 per second).
  function flush(force = false) {
    pumpQueued = false;
    if (!connected) return;
    const now = Date.now();
    const s = editing ? ZERO : state();
    if (force === true || !same(s, lastSent) || now - lastSentAt >= KEEPALIVE_MS) {
      if (Net.send({ type: 'pad', ...s })) { lastSent = s; lastSentAt = now; }
    }
  }
  // The keepalive always sends: a skipped tick from timer jitter would leave a 500 ms gap.
  setInterval(() => flush(true), KEEPALIVE_MS);

  function sendReleased() {
    releaseAll();
    if (connected) Net.send({ type: 'pad', ...ZERO });
    lastSent = { ...ZERO };
    lastSentAt = Date.now();
  }
  document.addEventListener('visibilitychange', () => { if (document.hidden) sendReleased(); });
  addEventListener('pagehide', sendReleased);
  addEventListener('blur', sendReleased);

  // ---------- messages from the PC ----------

  function showOverlay(text) {
    overlayText.textContent = text || '';
    overlay.hidden = !text;
  }

  Net.onMessage((msg) => {
    if (msg.type === 'player') {
      const n = msg.n;
      badge.textContent = `Player ${n}`;
      badge.hidden = false;
      document.documentElement.style.setProperty('--accent', PLAYER_COLORS[n] || PLAYER_COLORS[1]);
      document.documentElement.style.setProperty('--on-accent', PLAYER_INK[n] || PLAYER_INK[1]);
      showOverlay('');
    } else if (msg.type === 'full') {
      badge.hidden = true;
      showOverlay('Game is full (4 players). Try again when someone leaves.');
    } else if (msg.type === 'driver_missing') {
      badge.hidden = true;
      showOverlay('Controller driver missing: install ViGEmBus on the PC (see the Padette window).');
    } else if (msg.type === 'rumble') {
      if (navigator.vibrate) navigator.vibrate(msg.large || msg.small ? 40 + Math.round((Math.max(msg.large, msg.small) / 255) * 160) : 0);
    }
  });

  Net.onStatus((up) => {
    connected = up;
    if (!up) { releaseAll(); badge.hidden = true; }
    else flush(true);
  });

  // ---------- full screen (Android), debug overlay ----------

  let triedFullscreen = false;
  function goFullscreen() {
    if (triedFullscreen || !document.documentElement.requestFullscreen) return;
    triedFullscreen = true;
    document.documentElement.requestFullscreen({ navigationUI: 'hide' })
      .then(() => screen.orientation && screen.orientation.lock && screen.orientation.lock('landscape'))
      .catch(() => { /* not allowed here (iPhone, desktop): fine */ });
  }

  // Tap the player badge 5 times: shows the round trip to the PC in ms.
  let taps = [];
  let debugTimer = null;
  badge.addEventListener('click', () => {
    const now = Date.now();
    taps = [...taps.filter((t) => now - t < 2000), now];
    if (taps.length < 5) return;
    taps = [];
    debugEl.hidden = !debugEl.hidden;
    clearInterval(debugTimer);
    if (!debugEl.hidden) {
      const show = () => { const rtt = Net.rtt(); debugEl.textContent = rtt == null ? 'ping …' : `ping ${rtt} ms`; };
      show();
      debugTimer = setInterval(show, 1000);
    }
  });

  padEl.addEventListener('touchstart', onStart, { passive: false });
  padEl.addEventListener('touchmove', onMove, { passive: false });
  padEl.addEventListener('touchend', onEnd);
  padEl.addEventListener('touchcancel', onEnd);
  // No page scrolling, pinch zoom or double-tap zoom anywhere.
  document.addEventListener('touchmove', (e) => { if (e.target.closest('#pad')) e.preventDefault(); }, { passive: false });
  document.addEventListener('gesturestart', (e) => e.preventDefault());

  window.PadView = {
    render,
    layout: () => layout,
    /** While editing, touches move controls instead and the PC gets a released controller. */
    setEditing(on) {
      editing = on;
      releaseAll();
      flush(true);
    },
  };

  render();
})();
