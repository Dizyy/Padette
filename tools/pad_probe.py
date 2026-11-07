"""Real-driver check (Windows + ViGEmBus): plugs in 2 virtual Xbox 360 controllers for a few
seconds and shows which player number Windows gives each, from the two sources ViGEm offers
(the user index and the LED notification), and whether XInput sees A held on the first one.

Run:  .venv\\Scripts\\python.exe tools\\pad_probe.py
"""

from __future__ import annotations

import ctypes
import sys
import time

import vgamepad
from vgamepad.win import vigem_client as vcli


class XInputGamepad(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class XInputState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", XInputGamepad)]


def xinput_states() -> dict[int, XInputGamepad]:
    xinput = ctypes.windll.xinput1_4
    found = {}
    for slot in range(4):
        state = XInputState()
        if xinput.XInputGetState(slot, ctypes.byref(state)) == 0:
            found[slot] = state.Gamepad
    return found


def user_index(pad) -> int | None:
    index = ctypes.c_uint(0)
    err = vcli.vigem_target_x360_get_user_index(pad._busp, pad._devicep, ctypes.byref(index))
    return index.value if err == 0x20000000 else None  # VIGEM_ERROR_NONE


def main() -> int:
    print("Before:", sorted(xinput_states()))
    leds: dict[int, int] = {}
    pads = []
    for n in range(2):
        pad = vgamepad.VX360Gamepad()

        def on_notify(client, target, large_motor, small_motor, led_number, user_data, n=n):
            leds[n] = led_number
        pad.register_notification(callback_function=on_notify)
        pads.append(pad)
    time.sleep(1.0)
    for n, pad in enumerate(pads):
        print(f"pad {n}: user_index={user_index(pad)} led_number={leds.get(n)}")
    pads[0].press_button(button=vgamepad.XUSB_BUTTON.XUSB_GAMEPAD_A)
    pads[0].left_joystick_float(x_value_float=0.0, y_value_float=1.0)
    pads[0].update()
    time.sleep(0.3)
    states = xinput_states()
    print("XInput slots now:", sorted(states))
    for slot, g in states.items():
        print(f"  slot {slot}: buttons=0x{g.wButtons:04x} LY={g.sThumbLY}")
    for pad in pads:
        pad.reset()
        pad.update()
    del pads, pad
    time.sleep(0.5)
    print("After removing:", sorted(xinput_states()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
