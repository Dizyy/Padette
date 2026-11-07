// Padette layout editor: drag controls to move them, drag the corner handle to resize, turn
// optional controls on or off. Made for clip-on triggers: put LB/RB/LT/RT under the clips.
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const SNAP = 0.01; // positions and sizes snap to 1 % of the screen
  const OPTIONAL = [['stick_right', 'Right stick'], ['dpad', 'D-pad'], ['guide', 'Guide'], ['l3', 'L3'], ['r3', 'R3']];

  const app = $('app');
  const padEl = $('pad');
  const bar = $('edit-bar');
  const toggles = $('edit-toggles');
  let layout = null;
  let drag = null; // { ctl, mode: 'move' | 'size', x0, y0, start: {...} }

  const snap = (v) => Math.round(v / SNAP) * SNAP;
  const find = (id) => layout.controls.find((c) => c.id === id);

  function draw() {
    PadView.render(layout);
    for (const el of padEl.querySelectorAll('.ctl')) {
      const handle = document.createElement('span');
      handle.className = 'handle';
      el.append(handle);
    }
    toggles.textContent = '';
    for (const [id, name] of OPTIONAL) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'chip' + (find(id).on ? ' on' : '');
      b.textContent = name;
      b.setAttribute('aria-pressed', String(find(id).on));
      b.addEventListener('click', () => { find(id).on = !find(id).on; draw(); });
      toggles.append(b);
    }
  }

  function open() {
    layout = Layout.clamp(PadView.layout());
    PadView.setEditing(true);
    app.classList.add('editing');
    bar.hidden = false;
    draw();
  }

  function close(saveIt) {
    if (saveIt) Layout.save(layout);
    const next = Layout.load(); // the one just saved, or (Cancel) the one from before editing
    app.classList.remove('editing');
    bar.hidden = true;
    drag = null;
    PadView.render(next);
    PadView.setEditing(false);
  }

  padEl.addEventListener('pointerdown', (e) => {
    if (!app.classList.contains('editing')) return;
    const el = e.target.closest('.ctl');
    if (!el) return;
    e.preventDefault();
    const ctl = find(el.dataset.id);
    drag = { ctl, el, mode: e.target.classList.contains('handle') ? 'size' : 'move',
      x0: e.clientX, y0: e.clientY, start: { ...ctl }, id: e.pointerId };
    el.setPointerCapture(e.pointerId);
    el.classList.add('dragging');
  });

  padEl.addEventListener('pointermove', (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const W = padEl.clientWidth;
    const H = padEl.clientHeight;
    const dx = (e.clientX - drag.x0) / W;
    const dy = (e.clientY - drag.y0) / H;
    const { ctl, start } = drag;
    if (drag.mode === 'move') {
      ctl.x = Math.min(1 - ctl.w, Math.max(0, snap(start.x + dx)));
      ctl.y = Math.min(1 - ctl.h, Math.max(0, snap(start.y + dy)));
    } else {
      ctl.w = Math.min(1 - ctl.x, Math.max(Layout.MIN_SIZE, snap(start.w + dx)));
      // Round buttons stay circles: same size in pixels both ways.
      ctl.h = ctl.kind === 'round' ? (ctl.w * W) / H : start.h + dy;
      ctl.h = Math.min(1 - ctl.y, Math.max(Layout.MIN_SIZE, snap(ctl.h)));
    }
    Object.assign(drag.el.style, { left: `${ctl.x * 100}%`, top: `${ctl.y * 100}%`, width: `${ctl.w * 100}%`, height: `${ctl.h * 100}%` });
  });

  const endDrag = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    drag.el.classList.remove('dragging');
    drag = null;
    layout = Layout.clamp(layout);
  };
  padEl.addEventListener('pointerup', endDrag);
  padEl.addEventListener('pointercancel', endDrag);

  $('edit-btn').addEventListener('click', open);
  $('edit-done').addEventListener('click', () => close(true));
  $('edit-cancel').addEventListener('click', () => close(false));
  // Back to B3 on screen; saved only with Done, so Cancel still keeps the old layout.
  $('edit-reset').addEventListener('click', () => { layout = Layout.clamp(Layout.DEFAULT); draw(); });
})();
