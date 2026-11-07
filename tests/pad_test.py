"""Pad backend test (server/pad.py), with the dry-run pads: no driver needed.

Run:  .venv\\Scripts\\python.exe tests\\pad_test.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server import pad  # noqa: E402

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label)
    if not ok:
        failures.append(label)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def players_of(pads: pad.DryRunPads, count: int) -> list[int | None]:
    got = []
    for _ in range(count):
        seen = []
        pads.factory(lambda player, large, small: seen.append(player))
        got.append(seen[0] if seen else None)  # no notification = no number, like Windows
    return got


def main() -> int:
    print("Player numbers")
    check(players_of(pad.DryRunPads(), 5) == [1, 2, 3, 4, None], "5 pads -> players 1-4, then none")
    with_real = pad.DryRunPads()
    with_real.real_pads = 1
    check(players_of(with_real, 4) == [2, 3, 4, None], "1 real controller plugged in -> players 2-4, then none")

    pads = pad.DryRunPads()
    made = [pads.factory(lambda *a: None) for _ in range(3)]
    made[1].close()
    check(pads.closed == 1, "closing a pad counts it")
    check(players_of(pads, 1) == [2], "a closed pad's number is given out again")

    print("Applying state")
    capture = Capture()
    logging.getLogger("padette.pad").addHandler(capture)
    logging.getLogger("padette.pad").setLevel(logging.INFO)
    one = pad.DryRunPads().factory(lambda *a: None)
    one.apply(pad.PadState(buttons=pad.A | pad.LB, lx=1.0, ly=1.0))
    line = capture.lines[-1] if capture.lines else ""
    check("DRY-RUN pad 1 buttons=LB A " in line
          and "lx=1.00 ly=1.00" in line, f"applied state is logged: {line!r}")
    one.apply(pad.RELEASED)
    check("buttons=-" in capture.lines[-1], f"released state logs no buttons: {capture.lines[-1]!r}")
    check(pad.VALID_BUTTONS == 0xF7FF and pad.A == 0x1000 and pad.GUIDE == 0x0400, "button bits are the XUSB values")

    print("Driver missing")
    try:
        pad.DryRunPads(missing=True).factory(lambda *a: None)
        raised = False
    except pad.DriverMissing:
        raised = True
    check(raised, "missing driver -> DriverMissing")

    print("Plug-in errors")
    import types
    fake = types.ModuleType("vgamepad")

    class FakePad:
        report = types.SimpleNamespace(wButtons=0)

        def register_notification(self, callback_function):
            raise RuntimeError("Cannot register a callback")
    fake.VX360Gamepad = FakePad
    sys.modules["vgamepad"] = fake
    try:
        pad.vigem_factory(lambda *a: None)
        outcome = "no error"
    except pad.DriverMissing:
        outcome = "DriverMissing"
    except Exception as exc:
        outcome = type(exc).__name__
    finally:
        del sys.modules["vgamepad"]
    check(outcome == "DriverMissing", f"any error while plugging in -> DriverMissing ({outcome})")

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
