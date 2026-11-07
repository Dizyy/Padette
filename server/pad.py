"""Virtual Xbox 360 controllers: one per phone. Every driver call lives in this file.

The real controllers use ViGEmBus through `vgamepad`. `DryRunPads` logs instead, for
`--dry-run` and the tests. Windows hands each controller its player number (1-4, the
light on a real pad); it arrives through the driver's notification, like rumble does.
"""

from __future__ import annotations

import importlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("joypad.pad")

# Button bits: the XUSB values Windows uses, so the phone's bitmask goes straight to the driver.
DPAD_UP, DPAD_DOWN, DPAD_LEFT, DPAD_RIGHT = 0x0001, 0x0002, 0x0004, 0x0008
START, BACK, LEFT_THUMB, RIGHT_THUMB = 0x0010, 0x0020, 0x0040, 0x0080
LB, RB, GUIDE = 0x0100, 0x0200, 0x0400
A, B, X, Y = 0x1000, 0x2000, 0x4000, 0x8000
VALID_BUTTONS = 0xF7FF
BUTTON_NAMES = {DPAD_UP: "UP", DPAD_DOWN: "DOWN", DPAD_LEFT: "LEFT", DPAD_RIGHT: "RIGHT",
                START: "START", BACK: "BACK", LEFT_THUMB: "L3", RIGHT_THUMB: "R3", LB: "LB", RB: "RB",
                GUIDE: "GUIDE", A: "A", B: "B", X: "X", Y: "Y"}


@dataclass(frozen=True)
class PadState:
    """Everything a controller reports. Sticks -1..1 with +y = up (as XInput); triggers 0..1."""
    buttons: int = 0
    lx: float = 0.0
    ly: float = 0.0
    rx: float = 0.0
    ry: float = 0.0
    lt: float = 0.0
    rt: float = 0.0


RELEASED = PadState()

# on_notify(player 1-4 or None, large motor 0-255, small motor 0-255); may come from any thread.
Notify = Callable[[int | None, int, int], None]


class Pad(Protocol):
    def apply(self, state: PadState) -> None: ...
    def close(self) -> None: ...


PadFactory = Callable[[Notify], Pad]


class DriverMissing(Exception):
    """ViGEmBus isn't installed (or vgamepad can't load it)."""


# ---------- real controllers (ViGEmBus) ----------

class VigemPad:
    def __init__(self, on_notify: Notify):
        def notification(client, target, large_motor, small_motor, led_number, user_data):
            on_notify(led_number + 1 if 0 <= led_number <= 3 else None, large_motor, small_motor)

        self._pad = None
        try:
            vgamepad = importlib.import_module("vgamepad")  # connects to ViGEmBus; fails without it
            self._pad = vgamepad.VX360Gamepad()
            self._pad.register_notification(callback_function=notification)
            # A pad that was never updated reports a crooked stick: start released.
            self.apply(RELEASED)
        except Exception as exc:  # any failure while plugging in: no half-made controller
            self._pad = None  # vgamepad unplugs it when the object goes away
            raise DriverMissing(str(exc)) from exc

    def apply(self, state: PadState) -> None:
        pad = self._pad
        pad.report.wButtons = state.buttons
        pad.left_joystick_float(x_value_float=state.lx, y_value_float=state.ly)
        pad.right_joystick_float(x_value_float=state.rx, y_value_float=state.ry)
        pad.left_trigger_float(value_float=state.lt)
        pad.right_trigger_float(value_float=state.rt)
        pad.update()

    def close(self) -> None:
        pad, self._pad = self._pad, None
        if pad is not None:
            pad.reset()
            pad.update()
            del pad  # vgamepad unplugs the controller when the object goes away


def vigem_factory(on_notify: Notify) -> Pad:
    return VigemPad(on_notify)


DRIVER_URL = "https://github.com/nefarius/ViGEmBus/releases"


def driver_available() -> bool:
    """True when ViGEmBus is installed (vgamepad connects to it when imported)."""
    try:
        importlib.import_module("vgamepad")
        return True
    except Exception:
        return False


# ---------- dry run ----------

def describe(state: PadState) -> str:
    names = [name for bit, name in BUTTON_NAMES.items() if state.buttons & bit]
    return (f"buttons={' '.join(names) or '-'} lx={state.lx:.2f} ly={state.ly:.2f} "
            f"rx={state.rx:.2f} ry={state.ry:.2f} lt={state.lt:.2f} rt={state.rt:.2f}")


class DryRunPads:
    """Pretends to be Windows: gives each new pad the lowest free player number (after
    `real_pads` real controllers), or none when all 4 are taken."""

    def __init__(self, missing: bool = False):
        self.missing = missing
        self.real_pads = 0
        self.pads: list[_DryRunPad] = []
        self.closed = 0
        self._lock = threading.Lock()

    def factory(self, on_notify: Notify) -> Pad:
        if self.missing:
            raise DriverMissing("dry run: pretending ViGEmBus is not installed")
        with self._lock:
            taken = {p.player for p in self.pads if not p.closed}
            free = [n for n in range(self.real_pads + 1, 5) if n not in taken]
            pad = _DryRunPad(self, free[0] if free else None)
            self.pads.append(pad)
        if pad.player is not None:
            on_notify(pad.player, 0, 0)
        return pad


class _DryRunPad:
    def __init__(self, owner: DryRunPads, player: int | None):
        self.owner, self.player = owner, player
        self.closed = False
        self.states: list[PadState] = []

    def apply(self, state: PadState) -> None:
        self.states.append(state)
        log.info("DRY-RUN pad %s %s", self.player or "-", describe(state))

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.owner.closed += 1
            log.info("DRY-RUN pad %s unplugged", self.player or "-")
