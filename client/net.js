// JoyPad web client: connection, login and encryption. Speaks protocol v1 (see PROTOCOL.md)
// over /ws. The controller (pad.js) talks to the PC only through window.Net.
(() => {
  'use strict';

  const V = 1;
  const $ = (id) => document.getElementById(id);

  // ---------- Storage (all access guarded: private mode can throw) ----------
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* ignore */ } },
    remove(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } },
  };

  const appleTouch = /iPhone|iPad|iPod/.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const homeScreenApp = navigator.standalone === true
    || matchMedia('(display-mode: standalone), (display-mode: fullscreen)').matches;

  // The PIN lives in memory only.
  // After a correct PIN the server hands out a token instead, which is saved: iOS often
  // reloads the page after the phone was locked, and the token logs in again without the PIN.
  // It stops working when the server restarts, just like the PIN.
  let currentPin = null;
  const getPin = () => currentPin;
  const setPin = (pin) => { currentPin = pin; };
  const forgetPin = () => { currentPin = null; };
  const getToken = () => store.get('jp.token');
  const setToken = (token) => store.set('jp.token', token);
  const forgetToken = () => store.remove('jp.token');

  // ---------- Encryption (see server/encryption.py and PROTOCOL.md) ----------
  // The QR code carries the PC's key after "#", so it never travels over the network.
  // With it, each connection agrees on a session key and every frame is encrypted and
  // numbered (NaCl secretbox via tweetnacl.js). A typed PIN alone connects unencrypted.
  const SESSION_LABEL = new TextEncoder().encode('JoyPad session v1');
  const FROM_CLIENT = 0;
  const FROM_SERVER = 1;
  const canEncrypt = typeof nacl !== 'undefined';

  function b64encode(bytes) {
    let s = '';
    for (const b of bytes) s += String.fromCharCode(b);
    return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  function b64decode(text) {
    const s = atob(text.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((text.length + 3) % 4));
    return Uint8Array.from(s, (c) => c.charCodeAt(0));
  }
  function getKey() {
    const saved = store.get('jp.key');
    if (!saved || !canEncrypt) return null;
    try { const key = b64decode(saved); return key.length === 32 ? key : null; } catch { return null; }
  }
  const setKey = (text) => store.set('jp.key', text);
  const forgetKey = () => store.remove('jp.key');

  function frameNonce(direction, counter) {
    const nonce = new Uint8Array(24);
    nonce[0] = direction;
    for (let i = 1; i <= 8; i++) { nonce[i] = counter % 256; counter = Math.floor(counter / 256); }
    return nonce;
  }
  function makeSession(key, serverNonce, clientNonce) {
    const all = new Uint8Array(SESSION_LABEL.length + key.length + serverNonce.length + clientNonce.length);
    all.set(SESSION_LABEL, 0);
    all.set(key, SESSION_LABEL.length);
    all.set(serverNonce, SESSION_LABEL.length + key.length);
    all.set(clientNonce, SESSION_LABEL.length + key.length + serverNonce.length);
    return { key: nacl.hash(all).subarray(0, 32), sent: 0, received: 0 };
  }

  // Sends on one socket, encrypted once its handshake is done.
  function sockSend(sock, msg) {
    const text = JSON.stringify({ v: V, ...msg });
    if (!sock.session) { sock.send(text); return; }
    const s = sock.session;
    sock.send(nacl.secretbox(new TextEncoder().encode(text), frameNonce(FROM_CLIENT, s.sent++), s.key));
  }

  // The PC's IP address, from auth_ok. After a long sleep the phone sometimes can't look up
  // joypad.local any more; then the connection goes to the IP address instead.
  const getDirect = () => store.get('jp.direct');
  let viaDirect = false;
  function failedToOpen() {
    const direct = getDirect();
    if (direct && direct !== location.host) viaDirect = !viaDirect; // alternate name and IP
  }

  // ---------- Connection ----------
  const statusEl = $('status');
  const statusText = $('status-text');
  let ws = null;
  let authed = false;
  let reconnectDelay = 500;
  let reconnectTimer = null;
  let pingTimer = null;
  let lastPong = 0;
  let pingId = 0;
  let pingSentAt = 0;
  let lastRtt = null;
  let pinJustEntered = false; // typed just now, rather than reused for a reconnect
  let warnedUnencrypted = false;
  const CLOSE_BAD_FRAME = 4032; // the server couldn't decrypt what we sent
  const CLOSE_REPLACED = 4009;  // the same phone connected in another tab
  let replaced = false;         // then this tab waits until it's used again

  function setStatus(connected, text) {
    statusEl.classList.toggle('connected', connected);
    statusText.textContent = text;
    for (const fn of statusListeners) fn(connected);
  }

  function rawSend(msg) {
    if (ws && authed && ws.readyState === WebSocket.OPEN) {
      sockSend(ws, msg);
      return true;
    }
    return false;
  }

  function scheduleReconnect() {
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 5000);
  }

  function startPing() {
    stopPing();
    pingTimer = setInterval(() => {
      if (Date.now() - lastPong > 6000) { dropConnection(true); return; }
      pingSentAt = Date.now();
      rawSend({ type: 'ping', id: ++pingId });
    }, 2000);
  }

  function stopPing() { clearInterval(pingTimer); pingTimer = null; }

  // Abandon the current socket immediately (iOS often leaves dead sockets open after sleep).
  function dropConnection(reconnect) {
    const sock = ws;
    ws = null;
    authed = false;
    stopPing();
    if (sock) { try { sock.close(); } catch { /* ignore */ } }
    setStatus(false, reconnect ? 'Reconnecting…' : 'Not connected');
    if (reconnect) scheduleReconnect();
  }

  function connect() {
    clearTimeout(reconnectTimer);
    if (ws) return;
    const pin = getPin();
    // The saved token first, so scanning the QR code again keeps this phone's player instead
    // of making a second controller. The PIN is used when there is no token or it expired.
    const token = getToken();
    replaced = false;
    if (!pin && !token) { setStatus(false, 'Enter PIN'); showPin(); return; }

    setStatus(false, 'Connecting…');
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const target = viaDirect && getDirect() ? getDirect() : location.host;
    const sock = new WebSocket(`${proto}//${target}/ws`);
    sock.binaryType = 'arraybuffer';
    ws = sock;
    const key = getKey();
    let clientNonce = null;
    const sendAuth = () => sockSend(sock, token ? { type: 'auth', token } : { type: 'auth', pin });
    let authRejected = false;
    let opened = false;
    // After the phone wakes up, a new socket sometimes hangs without opening or failing.
    setTimeout(() => {
      if (ws !== sock || authed || authRejected) return;
      if (!opened) failedToOpen();
      dropConnection(true);
    }, 6000);

    sock.onopen = () => {
      opened = true;
      if (!key) { sendAuth(); return; }
      clientNonce = nacl.randomBytes(16);
      sockSend(sock, { type: 'enc_hello', nonce: b64encode(clientNonce) });
    };
    sock.onmessage = (event) => {
      let text = event.data;
      if (typeof text !== 'string') {
        if (!sock.session) return;
        const s = sock.session;
        const plain = nacl.secretbox.open(new Uint8Array(text), frameNonce(FROM_SERVER, s.received++), s.key);
        if (!plain) { if (ws === sock) dropConnection(true); return; } // tampered or out of order
        text = new TextDecoder().decode(plain);
      } else if (sock.session) {
        return; // never trust an unencrypted message on an encrypted connection
      }
      let msg;
      try { msg = JSON.parse(text); } catch { return; }
      if (msg.type === 'enc_challenge') {
        if (!clientNonce || sock.session) return;
        sock.session = makeSession(key, b64decode(msg.nonce), clientNonce);
        sendAuth();
        return;
      }
      switch (msg.type) {
        case 'auth_ok':
          authed = true;
          pinJustEntered = false;
          if (msg.direct) store.set('jp.direct', msg.direct);
          if (msg.token) {
            setToken(msg.token);
            if (getToken() === msg.token) forgetPin(); // keep the PIN if storage is blocked
          }
          reconnectDelay = 500;
          lastPong = Date.now();
          setStatus(true, sock.session ? 'Connected · encrypted' : 'Connected');
          if (!sock.session && !warnedUnencrypted) {
            warnedUnencrypted = true;
            showToast('Not encrypted. Scan the QR code on the PC to encrypt the connection.', { ms: 6000 });
          }
          hidePin();
          startPing();
          maybeShowHomeTip();
          break;
        case 'auth_failed':
          authRejected = true;
          if (msg.reason === 'token_expired') {
            forgetToken();
            if (getPin()) authRejected = false; // try the scanned PIN next (onclose reconnects)
            else showPin('JoyPad on the PC was restarted. Enter the new PIN from the JoyPad window.');
          } else if (msg.reason === 'locked_out') {
            showPin(`Too many wrong PINs. Try again in ${msg.retry_after || 60} s.`);
          } else {
            forgetToken();
            forgetPin();
            showPin(pinJustEntered
              ? 'Wrong PIN. Check the JoyPad window on your PC.'
              : 'The PIN on the PC has changed. Enter the new PIN from the JoyPad window.');
          }
          pinJustEntered = false;
          break;
        case 'pong':
          lastPong = Date.now();
          if (msg.id === pingId) lastRtt = lastPong - pingSentAt;
          break;
        case 'error':
          console.warn('Server error:', msg.code, msg.message);
          break;
        default:
          for (const fn of listeners) fn(msg);
      }
    };
    sock.onclose = (event) => {
      if (ws !== sock) return; // already dropped
      ws = null;
      authed = false;
      stopPing();
      if (event.code === CLOSE_BAD_FRAME && key) {
        // The PC has a different key now (a new key, or another PC): pair again.
        forgetKey();
        cleanAddress();
        authRejected = true;
        showPin('JoyPad has a new encryption key. Scan the QR code on the PC again, or enter the PIN to connect without encryption.');
      }
      if (event.code === CLOSE_REPLACED) {
        // Reconnecting by itself would take the controller back from the other tab, which
        // would take it back again, forever. Wait until this tab is used.
        replaced = true;
        setStatus(false, 'Open in another tab');
        showToast('JoyPad is open in another tab.', {
          ms: 24 * 3600 * 1000, button: 'Use here', onButton: () => { reconnectDelay = 500; connect(); },
        });
      } else if (authRejected) {
        setStatus(false, 'Not connected');
      } else {
        if (!opened) failedToOpen();
        setStatus(false, 'Reconnecting…');
        scheduleReconnect();
      }
    };
  }

  // Coming back from a locked phone or another app: reconnect straight away instead of
  // waiting for the backoff timer, and check an open socket really survived the sleep.
  // iOS doesn't always send visibilitychange (home-screen app, back/forward cache),
  // so pageshow, focus and online do the same.
  let lastWake = 0;
  function wake() {
    if (document.visibilityState !== 'visible') return;
    const now = Date.now();
    if (now - lastWake < 1000) return; // these events often fire together
    lastWake = now;
    reconnectDelay = 500;
    if (!ws) {
      if (getPin() || getToken()) {
        if (replaced) toast.hidden = true;
        connect();
      }
      return;
    }
    if (!authed) return; // still connecting
    rawSend({ type: 'ping', id: ++pingId });
    setTimeout(() => { if (ws && authed && lastPong < now) dropConnection(true); }, 2000);
  }
  document.addEventListener('visibilitychange', wake);
  window.addEventListener('pageshow', wake);
  window.addEventListener('focus', wake);
  window.addEventListener('online', wake);

  // ---------- Toast ----------
  const toast = $('toast');
  const toastText = $('toast-text');
  const toastBtn = $('toast-btn');
  let toastTimer = null;
  let toastAction = null;

  function showToast(text, { bad = false, ms = 4000, button = null, onButton = null } = {}) {
    clearTimeout(toastTimer);
    toastText.textContent = text;
    toast.classList.toggle('bad', bad);
    toastBtn.hidden = !button;
    toastBtn.textContent = button || '';
    toastAction = onButton;
    toast.hidden = false;
    toastTimer = setTimeout(() => { toast.hidden = true; }, ms);
  }

  toastBtn.addEventListener('click', () => {
    toast.hidden = true;
    if (toastAction) toastAction();
  });

  // ---------- PIN sheet ----------
  const pinSheet = $('pin-sheet');
  const pinInput = $('pin-input');
  const pinError = $('pin-error');

  function showPin(message) {
    pinError.textContent = message || '';
    pinError.hidden = !message;
    pinInput.value = '';
    pinSheet.hidden = false;
  }

  function hidePin() { pinSheet.hidden = true; }

  $('pin-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const pin = pinInput.value.trim();
    if (!/^\d{6}$/.test(pin)) { showPin('The PIN has 6 digits.'); return; }
    setPin(pin);
    pinJustEntered = true;
    pinInput.blur();
    pinError.hidden = true;
    dropConnection(false);
    reconnectDelay = 500;
    connect();
  });

  // Opened from the QR code in the JoyPad window: the PIN (and the encryption key) come
  // after "#", which the browser never sends anywhere. Use it, then wipe it so it isn't kept in history or an icon.
  // Pages linked from other websites arrive as /?from=link (see server/app.py): there the
  // PIN is ignored, so a website can't make JoyPad try wrong PINs.
  // A home-screen icon opens "#k=…" (see cleanAddress): the key only, no PIN.
  // Returns true when a PIN or key from the address was taken.
  function takePinFromQr() {
    if (!location.hash) { cleanAddress(); return false; }
    // "#p=…&k=…" (the long "#pin=…&key=…" from 1.5/1.6 works too).
    const fromQr = /^#p(?:in)?=(\d{6})(?:&k(?:ey)?=([A-Za-z0-9_-]{43}))?$/.exec(location.hash);
    const keyOnly = /^#k=([A-Za-z0-9_-]{43})$/.exec(location.hash);
    const trusted = !new URLSearchParams(location.search).has('from');
    history.replaceState(null, '', location.pathname);
    if (trusted && keyOnly) setKey(keyOnly[1]);
    if (trusted && fromQr) {
      setPin(fromQr[1]);
      if (fromQr[2]) setKey(fromQr[2]);
      // Not "just entered": a QR code from an earlier start then says the PIN has changed.
      pinJustEntered = false;
    }
    cleanAddress();
    return trusted && Boolean(fromQr || keyOnly);
  }

  // iPhone gives a home-screen app its own empty storage, and the camera always opens Safari, so
  // the app could never get the key. In Safari the address therefore keeps "#k=<key>" (never the
  // PIN): an icon added from there carries the key over. Like the QR code, "#" never leaves the phone.
  function cleanAddress() {
    const key = store.get('jp.key');
    const keep = appleTouch && !homeScreenApp && canEncrypt && key;
    history.replaceState(null, '', location.pathname + (keep ? `#k=${key}` : ''));
  }

  // Scanning again while JoyPad is already open only changes the "#" part.
  window.addEventListener('hashchange', () => {
    if (takePinFromQr()) {
      dropConnection(false);
      reconnectDelay = 500;
      connect();
    }
  });

  // iPhones and iPads can open the PC's .local name, which keeps working when the router gives
  // the PC a new IP address (so does a home-screen icon added there). The QR code uses the IP
  // address so that every phone can open it; an iPhone moves over to the name when it answers.
  // The "#" part, with the PIN and key, comes along and still never leaves the phone.
  async function moveToLocalName() {
    if (!appleTouch || !/^\d+\.\d+\.\d+\.\d+$/.test(location.hostname) || !/^#p(?:in)?=/.test(location.hash)
        || new URLSearchParams(location.search).has('from')) return false;
    try {
      const { name } = await (await fetch('/name', { cache: 'no-store' })).json();
      if (!name) return false;
      // Some networks block .local names: only move when it answers quickly.
      const stop = new AbortController();
      const timer = setTimeout(() => stop.abort(), 2500);
      await fetch(`http://${name}/name`, { mode: 'no-cors', cache: 'no-store', signal: stop.signal });
      clearTimeout(timer);
      location.replace(`http://${name}/${location.hash}`);
      return true;
    } catch {
      return false;
    }
  }

  // Opened in the browser: suggest adding JoyPad to the home screen (full screen, no address
  // bar). Shown once per page load on up to 3 visits, or until "Got it". Waits a few seconds so it
  // doesn't cover the "Not encrypted" message.
  const android = /Android/.test(navigator.userAgent);
  let homeTipShown = false;
  function maybeShowHomeTip() {
    if (homeTipShown || homeScreenApp || !(appleTouch || android)) return;
    // Without the key an icon would connect unencrypted; the "Not encrypted" message says to scan.
    if (appleTouch && !getKey()) return;
    const shown = Number(store.get('jp.hometip')) || 0;
    if (shown >= 3) return;
    homeTipShown = true;
    setTimeout(() => {
      if (!toast.hidden) return;
      store.set('jp.hometip', String(shown + 1));
      const how = appleTouch ? 'tap Share, then Add to Home Screen' : 'tap ⋮, then Add to Home screen';
      showToast(`Tip: ${how} to open JoyPad full screen.`, {
        ms: 15000, button: 'Got it', onButton: () => store.set('jp.hometip', '3'),
      });
    }, 7000);
  }

  // ---------- What the controller uses ----------
  const listeners = [];
  const statusListeners = [];
  window.Net = {
    /** Sends a message to the PC; false when not connected. */
    send: (msg) => rawSend(msg),
    /** fn(msg) for every server message except login, encryption and pong. */
    onMessage: (fn) => listeners.push(fn),
    /** fn(connected) whenever the connection goes up or down. */
    onStatus: (fn) => statusListeners.push(fn),
    /** Round-trip time of the last ping in ms (debug overlay), or null. */
    rtt: () => lastRtt,
    store,
    showToast,
  };

  // Scripts after this one (pad.js, editor.js) register their listeners first.
  addEventListener('DOMContentLoaded', () => {
    moveToLocalName().then((moving) => {
      if (moving) return;
      takePinFromQr();
      connect();
    });
  });
})();
