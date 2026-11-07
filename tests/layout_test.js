// Layout test (client/layout.js) in node, with a fake localStorage.
// Run:  node tests/layout_test.js
'use strict';

const path = require('path');
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
require(path.join(__dirname, '..', 'client', 'layout.js'));
const { Layout } = globalThis;

const failures = [];
function check(ok, label) {
  console.log((ok ? '  PASS  ' : '  FAIL  ') + label);
  if (!ok) failures.push(label);
}
const inside = (c) => c.x >= 0 && c.y >= 0 && c.x + c.w <= 1 + 1e-9 && c.y + c.h <= 1 + 1e-9;

console.log('Default layout');
check(Layout.DEFAULT.controls.every(inside), 'every default control lies inside the screen');
check(Layout.DEFAULT.controls.filter((c) => c.on).map((c) => c.id).sort().join() ===
  'a,b,lb,lt,menu,rb,rt,stick_left,view,x,y', 'B3 shows stick, ABXY, LB/LT/RB/RT, View/Menu');

console.log('Clamping (layouts from other screens)');
const moved = Layout.clamp({ v: 1, controls: [{ id: 'rt', x: 1.4, y: -0.2, w: 0.3, h: 0.1 }] });
const rt = moved.controls.find((c) => c.id === 'rt');
check(inside(rt) && rt.x === 0.7 && rt.y === 0 && rt.w === 0.3, `off-screen control pulled back in (${JSON.stringify(rt)})`);
const tiny = Layout.clamp({ v: 1, controls: [{ id: 'a', w: 0.001, h: -5 }] }).controls.find((c) => c.id === 'a');
check(tiny.w === Layout.MIN_SIZE && tiny.h === Layout.MIN_SIZE, 'too small or negative sizes become the minimum');
check(Layout.clamp({ v: 1, controls: [{ id: 'evil', x: 0 }] }).controls.length === Layout.DEFAULT.controls.length,
  'unknown controls dropped, missing ones added back');
check(Layout.clamp(null).controls.length === Layout.DEFAULT.controls.length, 'nothing saved -> default');

console.log('Saving and loading');
store.set('pd.layout', 'not json');
check(JSON.stringify(Layout.load()) === JSON.stringify(Layout.clamp(Layout.DEFAULT)), 'bad saved JSON -> default');
store.set('pd.layout', JSON.stringify({ v: 2, controls: [] }));
check(Layout.load().controls.find((c) => c.id === 'a').x === Layout.DEFAULT.controls.find((c) => c.id === 'a').x,
  'a layout from another version -> default');
const mine = Layout.load();
mine.controls.find((c) => c.id === 'rt').x = 0.5;
mine.controls.find((c) => c.id === 'dpad').on = true;
Layout.save(mine);
const back = Layout.load();
check(back.controls.find((c) => c.id === 'rt').x === 0.5 && back.controls.find((c) => c.id === 'dpad').on,
  'saved layout comes back after a reload');
check(Layout.reset().controls.find((c) => c.id === 'rt').x === 0.89 && !store.has('pd.layout'), 'reset -> B3 again');

console.log(failures.length ? `\n${failures.length} FAILED` : '\nAll checks passed.');
process.exit(failures.length ? 1 : 0);
