"""FastAPI app: WebSocket endpoint at /ws and the static web client at /."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import mimetypes
import secrets
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import APP_NAME, encryption, protocol
from .pad import PadFactory
from .players import TICK_S, Players

log = logging.getLogger("padette")

CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"

AUTH_TIMEOUT_S = 10
MAX_FAILED_PINS = 5
# Also counted across all addresses, so changing IP address doesn't allow more guesses.
MAX_FAILED_PINS_TOTAL = 10
LOCKOUT_S = 60

# WebSocket close codes (4000-4999 are reserved for applications).
CLOSE_AUTH_REQUIRED = 4001
CLOSE_AUTH_FAILED = 4003
CLOSE_LOCKED_OUT = 4029
CLOSE_NOT_LAN = 4030
CLOSE_BAD_ORIGIN = 4031
CLOSE_BAD_FRAME = 4032
CLOSE_REPLACED = 4009  # the same phone connected again

# Windows' registry sometimes maps .js to text/plain, which browsers refuse to run.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/manifest+json", ".webmanifest")


def is_lan_address(host: str | None) -> bool:
    try:
        addr = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_private or addr.is_loopback or addr.is_link_local


class PinGuard:
    """Checks the PIN and locks out guessing: per address, and for everyone after
    too many wrong PINs in total (so switching IP addresses doesn't help)."""

    def __init__(self, pin: str):
        self.pin = pin
        self._failures: dict[str, tuple[int, float]] = {}
        self._recent: list[float] = []  # times of wrong PINs from any address

    def locked_for(self, host: str) -> float:
        now = time.monotonic()
        self._recent = [t for t in self._recent if t > now - LOCKOUT_S]
        remaining = 0.0
        if len(self._recent) >= MAX_FAILED_PINS_TOTAL:
            remaining = self._recent[0] + LOCKOUT_S - now
        count, since = self._failures.get(host, (0, 0.0))
        if count >= MAX_FAILED_PINS:
            if since + LOCKOUT_S - now <= 0:
                self._failures.pop(host, None)
            else:
                remaining = max(remaining, since + LOCKOUT_S - now)
        return remaining

    def check(self, host: str, pin: str) -> bool:
        if hmac.compare_digest(pin.encode(), self.pin.encode()):
            self._failures.pop(host, None)
            return True
        count, _ = self._failures.get(host, (0, 0.0))
        self._failures[host] = (count + 1, time.monotonic())
        self._recent.append(time.monotonic())
        return False


class Channel:
    """A WebSocket that switches to encrypted binary frames once an enc_hello handshake is done.

    Before that, messages are JSON text frames. After it, every frame in both directions is
    encrypted, and a text frame (or one that doesn't decrypt) ends the connection.
    """

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.session: encryption.Session | None = None

    async def recv(self) -> str:
        message = await self.ws.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        if self.session is None:
            if message.get("text") is None:
                raise encryption.BadFrame("binary frame before the encryption handshake")
            return message["text"]
        if message.get("bytes") is None:
            raise encryption.BadFrame("unencrypted frame in an encrypted connection")
        return self.session.decrypt(message["bytes"])

    async def send(self, msg: dict) -> None:
        text = json.dumps(msg, separators=(",", ":"), ensure_ascii=False)
        if self.session is None:
            await self.ws.send_text(text)
        else:
            await self.ws.send_bytes(self.session.encrypt(text))


def create_app(pad_factory: PadFactory, pin: str, addresses: list[str] | None = None,
               key: bytes | None = None,
               on_players: Callable[[list[tuple[int | None, bool]]], None] | None = None) -> FastAPI:
    """`pad_factory` makes one virtual controller per phone (see pad.py).
    `addresses` are the "host:port" forms the phone may open (the .local name and the IP).
    When given, requests naming any other host are refused, which stops DNS-rebinding
    tricks from websites. `key` enables encrypted connections (see encryption.py).
    `on_players` gets (player number, away) for every controller whenever that changes."""
    # Browsers leave out ":80", so accept that form too.
    allowed = None if addresses is None else {a for addr in addresses for a in (addr, addr.removesuffix(":80"))}
    # The page may be opened under one of these and connect through another: after a long
    # sleep the phone sometimes can't look up padette.local, so it falls back to the IP.
    own_origins = None if allowed is None else {f"http://{a}" for a in allowed}
    direct = None if not addresses else addresses[-1].removesuffix(":80")  # the IP form
    ok_fields = {"protocol": protocol.PROTOCOL_VERSION, **({"direct": direct} if direct else {})}
    # The .local name (e.g. "padette.local"), when there is one. The QR code uses the IP
    # address, which every phone can open; iPhones then move over to this name (see app.js).
    local_name = addresses[0].removesuffix(":80") if addresses and len(addresses) > 1 else None
    channels: dict[str, Channel] = {}  # token -> the phone's connection
    loops: list[asyncio.AbstractEventLoop] = []  # the server's event loop, once it runs

    def notify(token: str, msg: dict) -> None:
        """Sends a message to one phone. Safe to call from any thread (driver callbacks)."""
        if not loops:
            return
        async def deliver() -> None:
            ch = channels.get(token)
            if ch is not None:
                try:
                    await ch.send(protocol.make(msg["type"], **{k: v for k, v in msg.items() if k != "type"}))
                except Exception:  # the socket is closing; the phone gets it on rejoin
                    pass
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loops[0]:
            running.create_task(deliver())
        else:
            asyncio.run_coroutine_threadsafe(deliver(), loops[0])

    players = Players(pad_factory, notify)
    # One worker keeps controller updates strictly ordered and off the event loop.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pads")

    async def housekeeping() -> None:
        last = None
        while True:
            await asyncio.sleep(TICK_S)
            await asyncio.get_running_loop().run_in_executor(executor, players.tick)
            now = players.snapshot()
            if on_players and now != last:
                on_players(now)
            last = now

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loops[:] = [asyncio.get_running_loop()]
        task = asyncio.create_task(housekeeping())
        try:
            yield
        finally:
            task.cancel()
            players.close_all()  # no virtual controllers left behind in Windows

    app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    guard = PinGuard(pin)
    # Handed out after a correct PIN; valid until the server stops, like the PIN itself.
    tokens: set[str] = set()

    def wrong_host(host_header: str | None) -> bool:
        return allowed is not None and (host_header or "").lower() not in allowed

    @app.middleware("http")
    async def lan_only(request: Request, call_next):
        if not is_lan_address(request.client.host if request.client else None):
            return PlainTextResponse(f"{APP_NAME} only accepts local network clients.", status_code=403)
        host_header = request.headers.get("host")
        if wrong_host(host_header):
            return PlainTextResponse(f"Open {APP_NAME} at http://{addresses[0].removesuffix(':80')}",
                                     status_code=403)
        # Another website linking to the page could put a PIN after "#" and make it try wrong
        # PINs until everyone is locked out. Such links land on /?from=link, where the page
        # ignores "#pin". The Camera app, a typed address and reloads aren't affected, and
        # neither is the page itself moving from the IP address to the .local name: its
        # Referer is one of our own addresses, which another website can't fake.
        referer = (request.headers.get("referer") or "").rstrip("/")
        from_ourselves = own_origins is not None and referer in own_origins
        if (request.url.path in ("/", "/index.html") and "from" not in request.query_params
                and request.headers.get("sec-fetch-site") in ("cross-site", "same-site")
                and not from_ourselves):
            return RedirectResponse("/?from=link", status_code=303)
        response = await call_next(request)
        # Make Safari check for a newer client on every load (a cheap 304 when nothing changed),
        # otherwise it keeps showing an old page after an update.
        response.headers["Cache-Control"] = "no-cache"
        # The page may only load its own files and talk to this server, and can't be framed.
        # It may also check whether the .local name answers before moving over to it.
        sockets = " ".join(sorted(f"ws://{a} http://{a}" for a in allowed or {host_header}))
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            f"connect-src 'self' {sockets}; manifest-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Only the address, never the path or the "#" part (which holds the PIN and key).
        response.headers["Referrer-Policy"] = "origin"
        return response

    async def first_message(ch: Channel) -> tuple[str, dict] | None:
        """The next message before login, or None after replying with an error and closing."""
        try:
            raw = await asyncio.wait_for(ch.recv(), AUTH_TIMEOUT_S)
        except asyncio.TimeoutError:
            await ch.send(protocol.error("auth_required", "send an auth message first"))
            await ch.ws.close(CLOSE_AUTH_REQUIRED)
            return None
        try:
            return protocol.parse(raw)
        except protocol.ProtocolError as exc:
            await ch.send(protocol.error(exc.code, exc.message))
            await ch.ws.close(CLOSE_AUTH_REQUIRED)
            return None

    async def authenticate(ch: Channel, host: str) -> str | None:
        """The token the phone is logged in with, or None after refusing it."""
        ws = ch.ws
        message = await first_message(ch)
        if message is None:
            return None
        type_, fields = message

        if type_ == "enc_hello":
            if key is None:
                await ch.send(protocol.error("no_encryption", "this server has no encryption key"))
                await ws.close(CLOSE_AUTH_REQUIRED)
                return None
            server_nonce = secrets.token_bytes(encryption.NONCE_BYTES)
            await ch.send(protocol.make("enc_challenge", nonce=encryption.b64encode(server_nonce)))
            ch.session = encryption.Session(key, server_nonce, fields["nonce"])
            message = await first_message(ch)  # encrypted from here on
            if message is None:
                return None
            type_, fields = message

        if type_ != "auth":
            await ch.send(protocol.error("auth_required", f"'{type_}' rejected: not authenticated"))
            await ws.close(CLOSE_AUTH_REQUIRED)
            return None

        if "token" in fields:
            # Tokens are 128-bit random, so wrong ones aren't counted as PIN guesses.
            if not any(hmac.compare_digest(fields["token"], t) for t in tokens):
                await ch.send(protocol.make("auth_failed", reason="token_expired"))
                await ws.close(CLOSE_AUTH_FAILED)
                return None
            await ch.send(protocol.make("auth_ok", **ok_fields))
            return fields["token"]

        locked = guard.locked_for(host)
        if locked:
            await ch.send(protocol.make("auth_failed", reason="locked_out", retry_after=round(locked)))
            await ws.close(CLOSE_LOCKED_OUT)
            return None

        if not guard.check(host, fields["pin"]):
            log.warning("Wrong PIN from %s", host)
            await ch.send(protocol.make("auth_failed", reason="wrong_pin"))
            await ws.close(CLOSE_AUTH_FAILED)
            return None

        token = secrets.token_hex(16)
        tokens.add(token)
        await ch.send(protocol.make("auth_ok", **ok_fields, token=token))
        return token

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        host = ws.client.host if ws.client else None
        if not is_lan_address(host):
            await ws.close(CLOSE_NOT_LAN)
            return
        # Browsers always send Origin. Only our own page may connect, so other
        # websites open in a browser on this network can't try PINs in the background.
        host_header = ws.headers.get("host")
        origin = ws.headers.get("origin")
        own = own_origins if own_origins is not None else {f"http://{host_header}"}
        if wrong_host(host_header) or (origin is not None and origin not in own):
            log.warning("Refused connection from %s (origin %s, host %s)", host, origin, host_header)
            await ws.close(CLOSE_BAD_ORIGIN)
            return
        await ws.accept()
        ch = Channel(ws)
        token = None
        try:
            token = await authenticate(ch, host)
            if token is None:
                return
            log.info("Client connected: %s%s", host, " (encrypted)" if ch.session else "")
            old = channels.get(token)
            if old is not None:  # the same phone again (new tab, or a reconnect): newest wins
                await old.ws.close(CLOSE_REPLACED)
            channels[token] = ch
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(executor, players.join, token)
            while True:
                raw = await ch.recv()
                try:
                    type_, fields = protocol.parse(raw)
                except protocol.ProtocolError as exc:
                    await ch.send(protocol.error(exc.code, exc.message))
                    continue
                if type_ == "pad":
                    await loop.run_in_executor(executor, players.apply, token, fields["state"])
                elif type_ == "ping":
                    await ch.send(protocol.make("pong", id=fields["id"]))
                elif type_ == "auth":
                    await ch.send(protocol.make("auth_ok", **ok_fields))
                elif type_ == "enc_hello":
                    await ch.send(protocol.error("bad_state", "enc_hello must be the first message"))
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            pass  # closed by a newer connection of the same phone
        except encryption.BadFrame as exc:
            # Wrong key, tampering or a replayed frame. Nothing it carried was used.
            log.warning("Closed connection from %s: %s", host, exc)
            await ws.close(CLOSE_BAD_FRAME)
        finally:
            if token is not None and channels.get(token) is ch:  # not replaced by a newer socket
                del channels[token]
                await asyncio.get_running_loop().run_in_executor(executor, players.leave, token)
            log.info("Client disconnected: %s", host)

    @app.get("/name")
    async def name() -> JSONResponse:
        return JSONResponse({"name": local_name})

    app.mount("/", StaticFiles(directory=CLIENT_DIR, html=True), name="client")
    return app
