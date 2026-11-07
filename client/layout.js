// Padette layout: where each control sits. Positions and sizes are fractions (0..1) of the
// landscape screen, so a layout works on any phone and can never end up off-screen.
// Saved on the phone under "pd.layout". Works in the browser and in node (tests/layout_test.js).
((root) => {
  'use strict';

  const KEY = 'pd.layout';
  const VERSION = 1;
  const MIN_SIZE = 0.06; // smallest control: 6 % of the screen

  // Round buttons keep their circle inside the box (the smaller side wins).
  // B3 "Floating": the left stick appears where the thumb lands on the left half.
  const c = (id, kind, x, y, w, h, on = true, label = '') => ({ id, kind, x, y, w, h, on, label });
  const face = (id, cx, cy) => c(id, 'round', cx - 0.0425, cy - 0.09, 0.085, 0.18, true, id.toUpperCase());
  const DEFAULT = Object.freeze({
    v: VERSION,
    controls: [
      c('stick_left', 'stickzone', 0.02, 0.14, 0.5, 0.84, true, 'Move'),
      c('stick_right', 'stickzone', 0.54, 0.4, 0.2, 0.58, false, 'Camera'),
      c('dpad', 'dpad', 0.32, 0.56, 0.16, 0.32, false),
      face('y', 0.84, 0.4),
      face('x', 0.755, 0.58),
      face('b', 0.925, 0.58),
      face('a', 0.84, 0.76),
      c('lb', 'button', 0.56, 0.12, 0.1, 0.13, true, 'LB'),
      c('lt', 'button', 0.67, 0.12, 0.1, 0.13, true, 'LT'),
      c('rb', 'button', 0.78, 0.12, 0.1, 0.13, true, 'RB'),
      c('rt', 'button', 0.89, 0.12, 0.1, 0.13, true, 'RT'),
      c('view', 'button', 0.56, 0.86, 0.08, 0.1, true, 'View'),
      c('menu', 'button', 0.65, 0.86, 0.08, 0.1, true, 'Menu'),
      c('guide', 'round', 0.47, 0.84, 0.06, 0.13, false, '⌂'),
      c('l3', 'round', 0.4, 0.16, 0.06, 0.13, false, 'L3'),
      c('r3', 'round', 0.54, 0.26, 0.06, 0.13, false, 'R3'),
    ].map(Object.freeze),
  });

  const copy = (layout) => ({ v: layout.v, controls: layout.controls.map((ctl) => ({ ...ctl })) });
  const num = (v, fallback) => (typeof v === 'number' && Number.isFinite(v) ? v : fallback);

  /** Every control inside the screen and at least MIN_SIZE big; unknown ids dropped,
   *  missing ones added from the default. */
  function clamp(layout) {
    const byId = new Map((layout && Array.isArray(layout.controls) ? layout.controls : [])
      .filter((ctl) => ctl && typeof ctl === 'object').map((ctl) => [ctl.id, ctl]));
    return {
      v: VERSION,
      controls: DEFAULT.controls.map((base) => {
        const saved = byId.get(base.id) || {};
        const w = Math.min(1, Math.max(MIN_SIZE, num(saved.w, base.w)));
        const h = Math.min(1, Math.max(MIN_SIZE, num(saved.h, base.h)));
        return {
          ...base,
          w, h,
          x: Math.min(1 - w, Math.max(0, num(saved.x, base.x))),
          y: Math.min(1 - h, Math.max(0, num(saved.y, base.y))),
          on: typeof saved.on === 'boolean' ? saved.on : base.on,
        };
      }),
    };
  }

  const storage = () => { try { return root.localStorage; } catch { return null; } };

  function load() {
    try {
      const saved = JSON.parse(storage().getItem(KEY));
      if (saved && saved.v === VERSION) return clamp(saved);
    } catch { /* bad or no saved layout */ }
    return copy(DEFAULT);
  }

  function save(layout) {
    try { storage().setItem(KEY, JSON.stringify(clamp(layout))); } catch { /* private mode */ }
  }

  function reset() {
    try { storage().removeItem(KEY); } catch { /* ignore */ }
    return copy(DEFAULT);
  }

  root.Layout = { DEFAULT, MIN_SIZE, clamp, load, save, reset };
})(typeof window !== 'undefined' ? window : globalThis);
