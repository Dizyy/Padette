"""End-to-end protocol test.

Starts the server in --dry-run mode on 127.0.0.1 (no real controller driver is used),
then sends every protocol message type over a WebSocket and checks the replies.

Run:  .venv\\Scripts\\python.exe tests\\smoke_test.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import segno  # noqa: E402
from nacl.secret import SecretBox  # noqa: E402
from server.__main__ import connect_url  # noqa: E402
from server.app import PinGuard, create_app  # noqa: E402
from server import pad  # noqa: E402
from server.encryption import load_key  # noqa: E402
from server.qr import qr_lines  # noqa: E402
HOST = "127.0.0.1"

# A valid controller state: A + LB held, left stick up, right trigger pressed.
GOOD_PAD = {"b": 0x1000 | 0x0100, "lx": 0, "ly": 1, "rx": -0.5, "ry": 0.25, "lt": 0, "rt": 1}

BAD_MESSAGES = [
    ("not json", "bad_json"),
    (json.dumps({"type": "ping"}), "bad_version"),
    (json.dumps({"v": 2, "type": "ping"}), "bad_version"),
    (json.dumps({"v": 1, "type": "teleport"}), "unknown_type"),
    # Mousette's mouse/keyboard messages don't exist here: guests can't control the PC.
    (json.dumps({"v": 1, "type": "move", "dx": 1, "dy": 1}), "unknown_type"),
    (json.dumps({"v": 1, "type": "hotkey", "keys": ["win", "r"]}), "unknown_type"),
    (json.dumps({"v": 1, "type": "shutdown"}), "unknown_type"),
    (json.dumps({"v": 1, "type": "ping", "id": [1]}), "bad_field"),
    *[(json.dumps({"v": 1, "type": "pad", **{**GOOD_PAD, **bad}}), "bad_field") for bad in (
        {"b": -1}, {"b": 0x10000}, {"b": 0x0800}, {"b": True}, {"b": 1.5},
        {"lx": 1.5}, {"lx": "0"}, {"ry": -1.01}, {"lt": -0.1}, {"rt": 1.2})],
    (json.dumps({"v": 1, "type": "pad", **{k: v for k, v in GOOD_PAD.items() if k != "ry"}}), "bad_field"),
    (json.dumps({"v": 1, "type": "pad", **GOOD_PAD, "lx": float("nan")}), "bad_field"),
]

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label)
    if not ok:
        failures.append(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, port: int):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        # A known key in a temporary file, so the test never touches the real one.
        self._key_dir = tempfile.TemporaryDirectory()
        self.key = os.urandom(32)
        key_file = Path(self._key_dir.name) / "key"
        key_file.write_bytes(self.key)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "server", "--host", HOST, "--port", str(port), "--dry-run",
             "--key-file", str(key_file)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8",
        )
        self.lines: list[str] = []
        self.pin: str | None = None
        self._ready = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))
            m = re.search(r"PIN:\s+(\d{6})", line)
            if m:
                self.pin = m.group(1)
                self._ready.set()

    def wait_ready(self, port: int) -> None:
        if not self._ready.wait(15):
            raise RuntimeError("server did not print a PIN:\n" + "\n".join(self.lines))
        for _ in range(50):
            try:
                with socket.create_connection((HOST, port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start listening")

    async def wait_for_log(self, needle: str, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while not self.log_count(needle) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    def log_count(self, needle: str) -> int:
        return sum(needle in line for line in self.lines)

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(5)
        self._key_dir.cleanup()


class EncryptedClient:
    """The phone's side of an encrypted connection, written from PROTOCOL.md (not server code)."""

    def __init__(self, ws, key: bytes):
        self.ws, self.key = ws, key
        self.box = None
        self.sent = self.received = 0

    @staticmethod
    def nonce(direction: int, counter: int) -> bytes:
        return bytes([direction]) + counter.to_bytes(8, "little") + bytes(15)

    async def handshake(self) -> None:
        client_nonce = os.urandom(16)
        await send(self.ws, {"type": "enc_hello", "nonce": base64.urlsafe_b64encode(client_nonce).decode().rstrip("=")})
        challenge = await recv_json(self.ws)
        server_nonce = base64.urlsafe_b64decode(challenge["nonce"] + "==")
        session_key = hashlib.sha512(b"Padette session v1" + self.key + server_nonce + client_nonce).digest()[:32]
        self.box = SecretBox(session_key)

    def frame(self, msg: dict) -> bytes:
        text = json.dumps({"v": 1, **msg}, ensure_ascii=False).encode()
        frame = self.box.encrypt(text, self.nonce(0, self.sent)).ciphertext
        self.sent += 1
        return frame

    async def send(self, msg: dict) -> bytes:
        frame = self.frame(msg)
        await self.ws.send(frame)
        return frame

    async def recv(self) -> dict:
        while True:
            data = await asyncio.wait_for(self.ws.recv(), 3)
            assert isinstance(data, bytes), f"expected an encrypted frame, got {data!r}"
            msg = json.loads(self.box.decrypt(data, self.nonce(1, self.received)))
            self.received += 1
            if msg.get("type") not in UNSOLICITED:
                return msg


# Sent by the server on its own (player number, full game, rumble): skipped by recv_json.
UNSOLICITED = {"player", "full", "driver_missing", "rumble"}


async def recv_json(ws, timeout=3.0, skip=UNSOLICITED) -> dict:
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if msg.get("type") not in skip:
            return msg


async def expect_closed(ws) -> int | None:
    try:
        while True:
            msg = await asyncio.wait_for(ws.recv(), 3)
            if not (isinstance(msg, str) and json.loads(msg).get("type") in UNSOLICITED):
                return None
    except websockets.ConnectionClosed as exc:
        return exc.rcvd.code if exc.rcvd else None


def send(ws, msg: dict):
    return ws.send(json.dumps({"v": 1, **msg}, ensure_ascii=False))


async def run(server: Server, port: int) -> None:
    url = f"ws://{HOST}:{port}/ws"

    print("Messages before login are refused")
    async with websockets.connect(url) as ws:
        await send(ws, {"type": "ping"})
        reply = await recv_json(ws)
        code = await expect_closed(ws)
        check(reply.get("code") == "auth_required" and code == 4001,
              f"ping before auth is rejected and socket closed (code {code})")

    print("Static client")
    for path, ctype in [("/", "text/html"), ("/net.js", "javascript"), ("/layout.js", "javascript"), ("/pad.js", "javascript"), ("/style.css", "text/css"),
                        ("/manifest.webmanifest", "manifest+json"), ("/icons/icon-180.png", "image/png")]:
        with urllib.request.urlopen(f"http://{HOST}:{port}{path}") as r:
            check(r.status == 200 and ctype in r.headers["content-type"]
                  and r.headers.get("cache-control") == "no-cache",
                  f"GET {path} -> {r.status} {r.headers['content-type']}, {r.headers.get('cache-control')}")
    with urllib.request.urlopen(f"http://{HOST}:{port}/") as r:
        csp = r.headers.get("content-security-policy", "")
        check("frame-ancestors 'none'" in csp and "script-src 'self'" in csp
              and r.headers.get("x-content-type-options") == "nosniff",
              "security headers set (CSP, nosniff)")

    print("Other websites and host names are refused")
    try:
        async with websockets.connect(url, origin="http://evil.example") as ws:
            await recv_json(ws)
        refused = False
    except Exception as exc:  # handshake rejected (HTTP 403) or closed with 4031
        refused = "403" in str(exc) or "4031" in str(exc)
    check(refused, "WebSocket from another website's page is refused")
    async with websockets.connect(url, origin=f"http://{HOST}:{port}") as ws:
        await send(ws, {"type": "ping"})
        check((await recv_json(ws)).get("code") == "auth_required", "WebSocket from our own page is accepted")
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://{HOST}:{port}/", headers={"Host": "evil.example"}))
        status = 200
    except urllib.error.HTTPError as exc:
        status = exc.code
    check(status == 403, f"page requested under another host name is refused ({status})")

    # A link from another website must not log in with a PIN in its "#": the page is sent
    # to /?from=link, where the client ignores it. Camera, typed address and reload log in.
    def page(path: str, fetch_site: str | None, referer: str | None = None) -> tuple[int, str | None]:
        conn = http.client.HTTPConnection(HOST, port, timeout=5)
        headers = {"Sec-Fetch-Site": fetch_site} if fetch_site else {}
        if referer:
            headers["Referer"] = referer
        conn.request("GET", path, headers=headers)
        r = conn.getresponse()
        r.read()
        conn.close()
        return r.status, r.getheader("location")
    for path, site, expected in [("/", "cross-site", (303, "/?from=link")),
                                 ("/index.html", "cross-site", (303, "/?from=link")),
                                 ("/", "same-site", (303, "/?from=link")),
                                 ("/?from=link", "cross-site", (200, None)),
                                 ("/", "none", (200, None)),
                                 ("/", "same-origin", (200, None)),
                                 ("/", None, (200, None))]:
        check(page(path, site) == expected, f"GET {path} from {site or 'unknown'} -> {page(path, site)}")
    # The page moving itself from the IP address to the .local name keeps its PIN; a link
    # from any other website still doesn't.
    own = f"http://{HOST}:{port}/"
    check(page("/", "cross-site", own) == (200, None), f"GET / from our own address ({own}) -> keeps the PIN")
    check(page("/", "cross-site", "http://evil.example/") == (303, "/?from=link"), "GET / from evil.example -> /?from=link")
    conn = http.client.HTTPConnection(HOST, port, timeout=5)
    conn.request("GET", "/name")
    r = conn.getresponse()
    check(r.status == 200 and json.loads(r.read()) == {"name": None},
          "GET /name -> no .local name on a loopback test server")
    conn.close()

    print("Wrong PINs are limited across all addresses")
    guard = PinGuard("123456")
    for i in range(10):
        guard.check(f"192.168.0.{i + 10}", "000000")  # 10 addresses, one guess each
    check(guard.locked_for("192.168.0.99") > 0, "an 11th address is locked out too")

    print("Authentication")
    wrong = "000000" if server.pin != "000000" else "111111"
    async with websockets.connect(url) as ws:
        await send(ws, {"type": "auth", "pin": wrong})
        reply = await recv_json(ws)
        code = await expect_closed(ws)
        check(reply == {"v": 1, "type": "auth_failed", "reason": "wrong_pin"} and code == 4003,
              f"wrong PIN -> auth_failed, closed (code {code})")

    async with websockets.connect(url) as ws:
        await send(ws, {"type": "auth", "pin": server.pin})
        reply = await recv_json(ws)
        token = reply.pop("token", None)
        check(reply == {"v": 1, "type": "auth_ok", "protocol": 1, "direct": f"{HOST}:{port}"}
              and isinstance(token, str) and len(token) == 32,
              "correct PIN -> auth_ok with a token and the IP address")

    print("Token login (phone reloaded the page after being locked)")
    async with websockets.connect(url) as ws:
        await send(ws, {"type": "auth", "token": token})
        reply = await recv_json(ws)
        check(reply == {"v": 1, "type": "auth_ok", "protocol": 1, "direct": f"{HOST}:{port}"},
              f"saved token -> {reply}")
    async with websockets.connect(url) as ws:
        await send(ws, {"type": "auth", "token": "0" * 32})
        reply = await recv_json(ws)
        code = await expect_closed(ws)
        check(reply == {"v": 1, "type": "auth_failed", "reason": "token_expired"} and code == 4003,
              f"unknown token -> token_expired, closed (code {code})")

    async with websockets.connect(url) as ws:
        await send(ws, {"type": "auth", "pin": server.pin})
        await recv_json(ws)

        print("Ping / pong")
        await send(ws, {"type": "ping", "id": 42})
        reply = await recv_json(ws)
        check(reply == {"v": 1, "type": "pong", "id": 42}, f"ping -> {reply}")

        print("Encrypted connection (key from the QR code)")
        async with websockets.connect(url) as enc_ws:
            enc = EncryptedClient(enc_ws, server.key)
            await enc.handshake()
            await enc.send({"type": "auth", "pin": server.pin})
            reply = await enc.recv()
            check(reply.get("type") == "auth_ok" and "token" in reply, "encrypted PIN login -> encrypted auth_ok")
            ping_frame = await enc.send({"type": "ping", "id": "secret-id"})
            check((await enc.recv()) == {"v": 1, "type": "pong", "id": "secret-id"}, "encrypted ping -> encrypted pong")
            check(b"secret-id" not in ping_frame and b"ping" not in ping_frame, "the frame on the wire doesn't show the message")
            await enc_ws.send(ping_frame)  # someone on the network replays a recorded frame
            code = await expect_closed(enc_ws)
            check(code == 4032, f"replayed frame -> closed (code {code})")

        async with websockets.connect(url) as enc_ws:
            enc = EncryptedClient(enc_ws, os.urandom(32))
            await enc.handshake()
            await enc.send({"type": "auth", "pin": server.pin})
            code = await expect_closed(enc_ws)
            check(code == 4032, f"wrong key -> closed (code {code}) without logging in")

        async with websockets.connect(url) as enc_ws:
            enc = EncryptedClient(enc_ws, server.key)
            await enc.handshake()
            await send(enc_ws, {"type": "auth", "pin": server.pin})
            code = await expect_closed(enc_ws)
            check(code == 4032, f"unencrypted message after the handshake -> closed (code {code})")

        async with websockets.connect(url) as enc_ws:
            await send(enc_ws, {"type": "enc_hello", "nonce": "short"})
            reply = await recv_json(enc_ws)
            check(reply.get("code") == "bad_field", "enc_hello with a bad nonce -> bad_field")

        print("Controller state")
        await send(ws, {"type": "pad", **GOOD_PAD})
        await send(ws, {"type": "ping", "id": "after-pad"})
        reply = await recv_json(ws)
        check(reply == {"v": 1, "type": "pong", "id": "after-pad"}, f"valid pad message -> no error ({reply})")

        print("Invalid messages get an error reply, connection stays open")
        for raw, expected_code in BAD_MESSAGES:
            await ws.send(raw)
            reply = await recv_json(ws)
            check(reply.get("type") == "error" and reply.get("code") == expected_code,
                  f"{raw[:50]!r} -> {reply.get('code')}")
        await send(ws, {"type": "ping", "id": 7})
        check((await recv_json(ws)).get("type") == "pong", "still connected after bad messages")


    print("PIN lockout")
    for _ in range(5):
        async with websockets.connect(url) as ws:
            await send(ws, {"type": "auth", "pin": wrong})
            await recv_json(ws)
    async with websockets.connect(url) as ws:
        await send(ws, {"type": "auth", "pin": server.pin})
        reply = await recv_json(ws)
        code = await expect_closed(ws)
        check(reply.get("reason") == "locked_out" and code == 4029,
              f"correct PIN refused after 5 wrong guesses (code {code})")


def check_qr() -> None:
    print("QR code")
    key = bytes(range(32))
    with_key = connect_url("padette.local", 80, "123456", key)
    check(with_key == "http://padette.local/#p=123456&k=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
          f"connect_url with the key -> {with_key}")
    with tempfile.TemporaryDirectory() as tmp:
        key_file = Path(tmp) / "sub" / "key"
        first = load_key(key_file)
        check(len(first) == 32 and load_key(key_file) == first, "encryption key is made once, then reused")
        check(load_key(key_file, new=True) != first, "--new-key replaces it")
    for args, expected in [(("padette.local", 80, "123456"), "http://padette.local/#p=123456"),
                           (("padette.local", 8765, "123456"), "http://padette.local:8765/#p=123456"),
                           (("192.168.1.23", 80, "123456"), "http://192.168.1.23/#p=123456")]:
        check(connect_url(*args) == expected, f"connect_url{args} -> {connect_url(*args)}")

    # Decode the drawn QR back into modules: each text line holds two module rows.
    url = "http://192.168.1.23/#p=123456&k=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
    halves = {"█": (True, True), "▀": (True, False), "▄": (False, True), " ": (False, False)}  # True = light
    rows: list[list[bool]] = []
    for line in qr_lines(url):
        top, bottom = zip(*(halves[c] for c in line))
        rows += [list(top), list(bottom)]
    border = 4
    drawn = [[not light for light in row[border:-border]] for row in rows[border:border + len(rows) - 2 * border]]
    expected_matrix = [[bool(m) for m in row] for row in segno.make(url, error="l", boost_error=False).matrix]
    drawn = drawn[:len(expected_matrix)]
    check(drawn == expected_matrix, f"QR drawing matches the code for {url} ({len(expected_matrix)} modules)")


class InProcess:
    """A server in this process, so the test can look at its dry-run pads."""

    def __init__(self, missing: bool = False):
        import uvicorn
        self.pads = pad.DryRunPads(missing=missing)
        self.events: list[list[tuple[int | None, bool]]] = []
        self.port = free_port()
        app = create_app(self.pads.factory, "123456", on_players=self.events.append)
        self.server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        while not self.server.started:
            time.sleep(0.05)
        self.url = f"ws://{HOST}:{self.port}/ws"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(5)


async def login(url: str, token: str | None = None):
    """Logs a phone in; returns (socket, token, first controller message)."""
    ws = await websockets.connect(url)
    await send(ws, {"type": "auth", **({"token": token} if token else {"pin": "123456"})})
    ok = await recv_json(ws)
    first = await recv_json(ws, timeout=4.5, skip=())
    return ws, ok.get("token", token), first


async def wait_until(condition, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return condition()


def check_players() -> None:
    print("Players (up to 4 phones as controllers)")
    srv = InProcess()

    async def phones() -> None:
        phones = [await login(srv.url) for _ in range(5)]
        firsts = [first for _, _, first in phones]
        check(firsts[:4] == [{"v": 1, "type": "player", "n": n} for n in (1, 2, 3, 4)],
              f"phones 1-4 -> players 1-4 ({firsts[:4]})")
        check(firsts[4] == {"v": 1, "type": "full"}, f"phone 5 -> full ({firsts[4]})")
        (ws1, token1, _), (ws2, token2, _) = phones[0], phones[1]
        pad1 = srv.pads.pads[0]

        await send(ws1, {"type": "pad", **GOOD_PAD})
        check(await wait_until(lambda: pad1.states and pad1.states[-1].buttons == pad.A | pad.LB),
              "pad message from phone 1 reaches controller 1")
        await asyncio.sleep(1.3)
        check(pad1.states[-1] == pad.RELEASED, "phone 1 silent for 1 s -> controller 1 released")

        await ws2.close()
        check(await wait_until(lambda: (2, True) in (srv.events[-1] if srv.events else [])),
              f"PC window shows player 2 away ({srv.events[-1] if srv.events else None})")
        ws2, _, first = await login(srv.url, token2)
        check(first == {"v": 1, "type": "player", "n": 2}, f"phone 2 back with its token -> player 2 again ({first})")

        ws1b, _, first = await login(srv.url, token1)
        code = await expect_closed(ws1)
        check(code == 4009 and first == {"v": 1, "type": "player", "n": 1},
              f"same phone on a 2nd socket -> old one closed ({code}), still player 1")
        check(sum(not p.closed for p in srv.pads.pads) == 4, "still 4 controllers, not 5")
        for ws, *_ in [(ws1b,), (ws2,), *phones[2:]]:
            await ws.close()

    asyncio.run(phones())
    srv.stop()
    check(all(p.closed for p in srv.pads.pads), "server stop unplugs every controller")

    srv = InProcess(missing=True)

    async def no_driver() -> None:
        ws, _, first = await login(srv.url)
        check(first == {"v": 1, "type": "driver_missing"}, f"driver missing -> driver_missing ({first})")
        await send(ws, {"type": "ping", "id": 1})
        check((await recv_json(ws)).get("type") == "pong", "connection stays open without the driver")
        await ws.close()

    asyncio.run(no_driver())
    srv.stop()


def check_tray_text() -> None:
    print("PC window text")
    from server.pad import driver_available
    from server.tray import status_text
    check(status_text([]) == ("Waiting for players", ""), f"no phones -> {status_text([])}")
    got = status_text([(2, True), (1, False)])
    check(got == ("2 players", "Player 1 · Player 2 (away)"), f"two players, one away -> {got}")
    check(status_text([(1, False), (None, False)])[1] == "Player 1", "a phone still waiting for its number isn't listed")
    check(isinstance(driver_available(), bool), f"driver check answers yes/no ({driver_available()})")


def main() -> int:
    check_qr()
    check_tray_text()
    check_players()
    port = free_port()
    server = Server(port)
    try:
        server.wait_ready(port)
        print(f"Server up on {HOST}:{port} (dry run), PIN {server.pin}\n")
        asyncio.run(run(server, port))
    finally:
        server.stop()
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        print("\n--- server log ---\n" + "\n".join(server.lines))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
