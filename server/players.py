"""The players: one virtual controller per logged-in phone, keyed by the phone's login token.

- Windows decides each controller's player number; it arrives through the pad's notification.
  No number within JOIN_TIMEOUT_S of plugging in means all 4 spots are taken: "full".
- A controller the driver refuses (on plug-in or mid-game) is removed: the phone gets
  "driver_missing" and the log gets one line, not one per message.
- No stuck input: a pad that hears nothing for SILENCE_S is released (no buttons, sticks
  centred). The phone repeats its state every 250 ms, so only a real dropout gets here.
- A phone that disconnects keeps its pad, released, for HOLD_S, so a screen lock or Wi-Fi blip
  doesn't make the game pause with "controller disconnected". Back in time = same pad and number.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .pad import RELEASED, DriverMissing, Notify, Pad, PadFactory, PadState

log = logging.getLogger("padette.players")

JOIN_TIMEOUT_S = 3.0  # counted from plug-in: the first plug-in can take Windows a while
SILENCE_S = 0.75      # + one 0.25 s tick = released within 1 s of the last message
HOLD_S = 30.0
TICK_S = 0.25  # how often the app calls tick()


@dataclass
class _Player:
    joined: float
    last_state: float
    pad: Pad | None = None
    number: int | None = None
    away_since: float | None = None
    released: bool = True
    rumble: tuple[int, int] = (0, 0)


class Players:
    def __init__(self, factory: PadFactory, notify: Callable[[str, dict], None],
                 clock: Callable[[], float] = time.monotonic):
        """`notify(token, message)` sends a message to that phone; called from any thread."""
        self._factory, self._notify, self._clock = factory, notify, clock
        self._players: dict[str, _Player] = {}
        self._lock = threading.RLock()

    # ---------- phones ----------

    def join(self, token: str) -> None:
        now = self._clock()
        with self._lock:
            player = self._players.get(token)
            if player is not None:  # back within the hold time, or the same phone twice
                player.away_since = None
                player.last_state = now
                if player.number is not None:
                    self._notify(token, {"type": "player", "n": player.number})
                return
            player = self._players[token] = _Player(joined=now, last_state=now)
        try:
            pad = self._factory(self._notifier(token))
        except Exception as exc:  # DriverMissing, or anything else while plugging in
            if isinstance(exc, DriverMissing):
                log.warning("Controller driver missing: %s", exc)
            else:
                log.exception("Could not plug in a controller")
            with self._lock:
                if self._players.get(token) is player:
                    del self._players[token]  # no silent, dead player left behind
            self._notify(token, {"type": "driver_missing"})
            return
        with self._lock:
            if self._players.get(token) is player:
                player.pad = pad
                player.joined = self._clock()  # the wait for a number starts now
                return
        pad.close()  # close_all() ran meanwhile

    def apply(self, token: str, state: PadState) -> None:
        with self._lock:
            player = self._players.get(token)
            if player is None or player.pad is None or player.away_since is not None:
                return
            player.last_state = self._clock()
            player.released = state == RELEASED
            if self._apply(player, state):
                return
            del self._players[token]
        self._broken(token, player)

    def leave(self, token: str) -> None:
        with self._lock:
            player = self._players.get(token)
            if player is None:
                return
            player.released = True
            player.away_since = self._clock()
            if player.pad is None or self._apply(player, RELEASED):
                return
            del self._players[token]
        self._broken(token, player)

    # ---------- housekeeping ----------

    def tick(self) -> None:
        now = self._clock()
        full, gone, broken = [], [], []
        with self._lock:
            for token, player in list(self._players.items()):
                if player.away_since is not None:
                    if now - player.away_since >= HOLD_S:
                        gone.append(self._players.pop(token))
                elif player.number is None and player.pad is not None and now - player.joined >= JOIN_TIMEOUT_S:
                    full.append(token)
                    gone.append(self._players.pop(token))
                elif not player.released and now - player.last_state >= SILENCE_S:
                    player.released = True
                    if not self._apply(player, RELEASED):
                        broken.append((token, self._players.pop(token)))
        for token, player in broken:
            self._broken(token, player)
        for player in gone:
            self._close(player)
        for token in full:
            log.info("A phone joined but all 4 controller spots are taken")
            self._notify(token, {"type": "full"})

    def close_all(self) -> None:
        with self._lock:
            players = list(self._players.values())
            self._players.clear()
        for player in players:
            self._close(player)

    def snapshot(self) -> list[tuple[int | None, bool]]:
        """(player number, away) for every controller, for the PC window."""
        with self._lock:
            return [(p.number, p.away_since is not None) for p in self._players.values() if p.pad is not None]

    # ---------- internals ----------

    def _notifier(self, token: str) -> Notify:
        def on_notify(number: int | None, large: int, small: int) -> None:
            messages = []
            with self._lock:
                player = self._players.get(token)
                if player is None:
                    return
                # The driver repeats its notification on every update: act on changes only.
                if number is not None and number != player.number:
                    player.number = number
                    messages.append({"type": "player", "n": number})
                if (large, small) != player.rumble:
                    player.rumble = (large, small)
                    messages.append({"type": "rumble", "large": large, "small": small})
            for msg in messages:
                self._notify(token, msg)
        return on_notify

    def _apply(self, player: _Player, state: PadState) -> bool:
        """False when the driver refused: the caller removes that player (see _broken)."""
        try:
            player.pad.apply(state)
            return True
        except Exception:
            log.exception("Controller update failed (player %s): removing it", player.number)
            return False

    def _broken(self, token: str, player: _Player) -> None:
        """Called outside the lock for a player already taken out of the list."""
        self._close(player)
        self._notify(token, {"type": "driver_missing"})

    def _close(self, player: _Player) -> None:
        if player.pad is None:
            return
        try:
            player.pad.close()
        except Exception:
            log.exception("Could not unplug controller (player %s)", player.number)
