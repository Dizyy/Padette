"""Players test (server/players.py): slots, full game, 30 s hold, 1 s safety release.
Uses the dry-run pads and a fake clock, so it runs instantly and needs no driver.

Run:  .venv\\Scripts\\python.exe tests\\players_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server import pad  # noqa: E402
from server.players import HOLD_S, JOIN_TIMEOUT_S, SILENCE_S, Players  # noqa: E402

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label)
    if not ok:
        failures.append(label)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Setup:
    def __init__(self, real_pads: int = 0, missing: bool = False):
        self.pads = pad.DryRunPads(missing=missing)
        self.pads.real_pads = real_pads
        self.clock = Clock()
        self.sent: list[tuple[str, dict]] = []
        self.players = Players(self.pads.factory, lambda token, msg: self.sent.append((token, msg)), self.clock)

    def last(self, token: str) -> dict | None:
        msgs = [m for t, m in self.sent if t == token]
        return msgs[-1] if msgs else None

    def wait(self, seconds: float) -> None:
        self.clock.now += seconds
        self.players.tick()


A = pad.PadState(buttons=pad.A)


def main() -> int:
    print("Joining")
    s = Setup()
    for t in "abcde":
        s.players.join(t)
    s.wait(JOIN_TIMEOUT_S + 0.1)
    got = [s.last(t) for t in "abcde"]
    check(got[:4] == [{"type": "player", "n": n} for n in (1, 2, 3, 4)], f"4 phones -> players 1-4 ({got[:4]})")
    check(got[4] == {"type": "full"}, f"5th phone -> full ({got[4]})")
    check(s.pads.closed == 1 and len(s.players.snapshot()) == 4, "the 5th phone's pad is removed")

    s = Setup(real_pads=1)
    for t in "abcd":
        s.players.join(t)
    s.wait(JOIN_TIMEOUT_S + 0.1)
    check([s.last(t) for t in "abcd"] == [{"type": "player", "n": n} for n in (2, 3, 4)] + [{"type": "full"}],
          "1 real controller plugged in -> players 2-4, then full")

    s = Setup(missing=True)
    s.players.join("a")
    check(s.last("a") == {"type": "driver_missing"} and s.players.snapshot() == [], "missing driver -> driver_missing, no pad")

    print("Safety release")
    s = Setup()
    s.players.join("a")
    p1 = s.pads.pads[0]
    s.players.apply("a", A)
    s.wait(SILENCE_S - 0.2)
    check(p1.states[-1] == A, "still held before 1 s of silence")
    s.wait(0.3)
    check(p1.states[-1] == pad.RELEASED, "released after 1 s without a message")
    count = len(p1.states)
    s.wait(0.25)
    s.wait(0.25)
    check(len(p1.states) == count, "released once, not on every tick")
    s.players.apply("unknown", A)
    check(len(p1.states) == count, "state for an unknown phone is ignored")

    print("Away and back")
    s = Setup()
    s.players.join("a")
    s.players.join("b")
    p1 = s.pads.pads[0]
    s.players.apply("a", A)
    s.players.leave("a")
    check(p1.states[-1] == pad.RELEASED, "leaving releases everything at once")
    check(sorted(s.players.snapshot()) == [(1, True), (2, False)], f"tray shows player 1 away ({s.players.snapshot()})")
    s.wait(20)
    s.players.join("a")
    check(len(s.pads.pads) == 2 and s.last("a") == {"type": "player", "n": 1}, "back within 30 s -> same pad, same player")
    s.players.leave("a")
    s.wait(HOLD_S + 1)
    check(p1.closed and sorted(s.players.snapshot()) == [(2, False)], "gone for 30 s -> pad removed")

    print("Same phone twice")
    s = Setup()
    s.players.join("a")
    s.players.join("a")
    check(len(s.pads.pads) == 1 and s.last("a") == {"type": "player", "n": 1}, "joining twice -> still one pad")

    print("Rumble")
    s = Setup()
    s.players.join("a")
    notify = s.players._notifier("a")
    notify(1, 200, 50)
    notify(1, 200, 50)  # the driver repeats notifications on every update
    rumbles = [m for t, m in s.sent if m["type"] == "rumble"]
    check(rumbles == [{"type": "rumble", "large": 200, "small": 50}], f"rumble sent once per change ({rumbles})")
    notify(1, 0, 0)
    check(s.last("a") == {"type": "rumble", "large": 0, "small": 0}, "rumble stop sent")

    print("Driver errors and shutdown")
    s = Setup()
    s.players.join("a")
    s.players.join("b")

    def broken(state):
        raise OSError("driver hiccup")
    s.pads.pads[0].apply = broken
    try:
        s.players.apply("a", A)
        ok = True
    except Exception:
        ok = False
    s.players.apply("b", A)
    check(ok and s.pads.pads[1].states[-1] == A, "a failing pad doesn't raise or stop other players")
    s.players.close_all()
    check(s.pads.closed == 2 and s.players.snapshot() == [], "shutdown removes every pad")

    print("Review fixes")
    import logging
    records: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())
    logging.getLogger("padette.players").addHandler(Grab())

    # A driver failure mid-game: that phone hears driver_missing, the log gets one line.
    s = Setup()
    s.players.join("a")
    s.players.join("b")

    def broken2(state):
        raise OSError("driver gone")
    s.pads.pads[0].apply = broken2
    records.clear()
    for _ in range(30):
        s.players.apply("a", A)
    s.players.apply("b", A)
    check(s.last("a") == {"type": "driver_missing"}, f"failing controller -> phone told driver_missing ({s.last('a')})")
    check(sum("failed" in r for r in records) == 1, f"logged once, not 30 times ({len(records)} lines)")
    check(s.pads.pads[0].closed and s.pads.pads[1].states[-1] == A, "broken pad unplugged, player 2 still plays")

    # Any error while plugging in (not only DriverMissing) leaves no silent, dead player.
    class Boom:
        calls = 0

        def factory(self, on_notify):
            self.calls += 1
            raise RuntimeError("register_notification failed")
    boom = Boom()
    sent: list = []
    players = Players(boom.factory, lambda t, m: sent.append((t, m)), Clock())
    players.join("a")
    players.join("a")
    check(sent[-1] == ("a", {"type": "driver_missing"}) and boom.calls == 2 and players.snapshot() == [],
          f"plug-in error -> driver_missing, and a rejoin tries again ({boom.calls} tries)")

    # A slow first plug-in is not "Game is full": the wait starts once the pad exists.
    clock = Clock()
    sent = []
    late: dict = {}

    def slow_factory(on_notify):
        clock.now += 2.0  # Windows setting up the device the first time
        late["notify"] = on_notify
        return pad.DryRunPads().factory(lambda *a: None)
    players = Players(slow_factory, lambda t, m: sent.append((t, m)), clock)
    players.join("a")
    players.tick()
    clock.now += JOIN_TIMEOUT_S - 0.5
    players.tick()
    check(("a", {"type": "full"}) not in sent, "slow plug-in (2 s) -> no false 'Game is full'")
    late["notify"](1, 0, 0)
    check(sent[-1] == ("a", {"type": "player", "n": 1}), "its number arrives late -> Player 1")
    check(JOIN_TIMEOUT_S >= 3, f"Windows gets at least 3 s to give a number ({JOIN_TIMEOUT_S})")

    # Release within 1 s, even when the tick falls just after the deadline.
    s = Setup()
    s.players.join("a")
    p1 = s.pads.pads[0]
    s.players.apply("a", A)
    for _ in range(4):
        s.wait(0.25)
    check(p1.states[-1] == pad.RELEASED, "held button released within 1.0 s of silence (0.25 s ticks)")

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
