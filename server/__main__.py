"""Entry point: `python -m server [--host IP] [--port N] [--dry-run] [--no-update] [--new-key]`."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import secrets
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from . import APP_NAME, APP_SLUG, __version__, encryption
from .app import create_app, is_lan_address
from .mdns import MdnsResponder
from .pad import DryRunPads, vigem_factory
from .qr import qr_lines

# Port 80 lets the phone open the address without ":port"; 8765 if something else has 80.
PREFERRED_PORTS = (80, 8765)


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Without this, Windows lets us share a port another program listens on.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def url(host: str, port: int) -> str:
    return f"http://{host}" if port == 80 else f"http://{host}:{port}"


def connect_url(name: str, port: int, pin: str, key: bytes | None = None) -> str:
    """Address for the QR code. The PIN (p) and the encryption key (k) go after "#", which
    browsers never send to the server, so they only travel through the camera. The short
    names keep the code small enough for picky phone cameras to read."""
    return f"{url(name, port)}/#p={pin}" + (f"&k={encryption.b64encode(key)}" if key else "")


def lan_ip_candidates() -> list[str]:
    """This PC's private IPv4 addresses, most likely home network first.

    VPN adapters (typically on 10.x.x.x) often own the default
    route, so addresses are ranked rather than taken from the routing table:
    home routers almost always hand out 192.168.x.x, then 172.16-31.x, then 10.x.
    """
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    # Connecting a UDP socket sends no packets; it only makes the OS pick a route.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
        except OSError:
            pass

    def usable(ip: str) -> bool:
        addr = ipaddress.ip_address(ip)
        return addr.is_private and not addr.is_loopback and not addr.is_link_local

    def rank(ip: str) -> int:
        if ip.startswith("192.168."):
            return 0
        if ipaddress.ip_address(ip) in ipaddress.ip_network("172.16.0.0/12"):
            return 1
        return 2

    return sorted((ip for ip in ips if usable(ip)), key=lambda ip: (rank(ip), ip))


class StartupError(Exception):
    """The app can't start; the message says why and what to do."""


@dataclass
class Setup:
    """Everything decided before the server starts: shown in the console or the window."""
    args: argparse.Namespace
    host: str
    port: int
    pin: str
    key: bytes | None
    hostname: str | None
    candidates: list[str]
    mdns: MdnsResponder | None
    app: FastAPI

    @property
    def qr_url(self) -> str:
        return connect_url(self.host, self.port, self.pin, self.key)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=APP_SLUG, description=f"{APP_NAME} PC server")
    parser.add_argument("--host", help="address to bind (default: this PC's LAN IP)")
    parser.add_argument("--port", type=int,
                        help="port to listen on (default: 80, or 8765 if 80 is taken)")
    parser.add_argument("--dry-run", action="store_true",
                        help="log controller input instead of using the real controller driver")
    parser.add_argument("--no-update", action="store_true",
                        help="don't check for a new version")
    parser.add_argument("--new-key", action="store_true",
                        help="make a new encryption key: phones must scan the QR code again")
    parser.add_argument("--key-file", type=Path, default=None,
                        help=f"where the encryption key is kept (default: {encryption.default_key_file()})")
    # The .exe restarting itself (as administrator): wait for the old copy to close.
    parser.add_argument("--replace", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    # On Windows, asyncio prints a traceback when a phone drops its connection (locked, app
    # closed): ConnectionResetError while closing a socket that is already gone. Nothing is wrong.
    logging.getLogger("asyncio").addFilter(
        lambda record: not (record.exc_info and isinstance(record.exc_info[1], ConnectionResetError)))


def prepare(args: argparse.Namespace,
            on_players: Callable[[list[tuple[int | None, bool]]], None] | None = None) -> Setup:
    """Picks the address, port and PIN and builds the app. `on_players` gets (player number,
    away) for every controller whenever that changes."""
    candidates = [] if args.host else lan_ip_candidates()
    host = args.host or (candidates[0] if candidates else None)
    if host is None:
        raise StartupError("Could not find a local network address. Is this PC connected to "
                           "Wi-Fi/Ethernet?\nYou can pass one explicitly, e.g.  --host 192.168.x.x")
    if not is_lan_address(host):
        raise StartupError(f"Refusing to bind to {host}: {APP_NAME} only listens on private/LAN addresses.")

    port = args.port or next((p for p in PREFERRED_PORTS if port_is_free(host, p)), None)
    if port is None:
        raise StartupError(f"Ports {' and '.join(map(str, PREFERRED_PORTS))} are both in use. "
                           "Choose another, e.g.  --port 9000")

    # The .local name only makes sense on a real network, not for local tests.
    mdns = None if ipaddress.ip_address(host).is_loopback else MdnsResponder(host)
    hostname = mdns.start() if mdns else None

    pin = f"{secrets.randbelow(1_000_000):06d}"
    key_file = Path(args.key_file) if args.key_file else encryption.default_key_file()
    key = encryption.load_key(key_file, new=args.new_key)
    pad_factory = DryRunPads().factory if args.dry_run else vigem_factory
    names = [hostname, host] if hostname else [host]
    app = create_app(pad_factory, pin, addresses=[f"{name}:{port}" for name in names], key=key,
                     on_players=on_players)
    return Setup(args, host, port, pin, key, hostname, candidates, mdns, app)


def print_banner(s: Setup) -> None:
    bar = "=" * 46
    print(bar)
    print(f"  {APP_NAME} {__version__} is running" + ("  (DRY RUN - no real controllers)" if s.args.dry_run else ""))
    # Android browsers can't open .local names, so the IP address comes first and is in the QR.
    print(f"  Open on your phone:    {url(s.host, s.port)}")
    if s.hostname:
        print(f"  iPhone/iPad also:      {url(s.hostname, s.port)}")
    print(f"  PIN:                  {s.pin}")
    try:
        lines = qr_lines(s.qr_url)
        print("  Or scan with the phone camera (encrypted connection):")
        for line in lines:
            print("  " + line)
    except UnicodeEncodeError:
        pass  # the console can't show it; the address and PIN above still work
    if len(s.candidates) > 1:
        print(f"  Other addresses:      {', '.join(s.candidates[1:])}")
        print(f"  (if the phone can't connect, start {APP_NAME} with  --host <address>)")
    print("  Press Ctrl+C to stop.")
    print(bar, flush=True)


def make_server(s: Setup, **config) -> uvicorn.Server:
    return uvicorn.Server(uvicorn.Config(s.app, host=s.host, port=s.port, log_level="warning",
                                         ws_max_size=16 * 1024, **config))


def main() -> int:
    args = parse_args()
    setup_logging()
    try:
        s = prepare(args)
    except StartupError as exc:
        print(exc)
        return 1
    print_banner(s)
    try:
        make_server(s).run()
    finally:
        if s.mdns:
            s.mdns.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
