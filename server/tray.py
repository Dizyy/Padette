"""Padette.exe without a console: a tray icon and a window with the QR code, PIN and status.

Windows only. The source version (start.bat, `python -m server`) keeps its console.
Try it from source with  `python -m server.tray --dry-run --no-update`.
"""

from __future__ import annotations

import base64
import ctypes
import io
import json
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from ctypes import wintypes
from tkinter import messagebox

import segno

from . import APP_NAME, APP_SLUG, __version__, encryption
from .__main__ import StartupError, make_server, parse_args, prepare, setup_logging, url
from .app import CLIENT_DIR
from .pad import DRIVER_URL, driver_available

log = logging.getLogger("padette.tray")

LOG_FILE = encryption.default_key_file().with_name(f"{APP_SLUG}.log")
LOG_MAX_BYTES = 1_000_000

# Comfort theme, as on the phone.
BG, SURFACE, BORDER = "#1a1916", "#24221e", "#36332d"
TEXT, MUTED, ACCENT, BAD = "#ece6da", "#a39a8a", "#9cc5a1", "#e3796a"
# Inverted QR code (light squares on black) so it sits in the dark window. Most phone
# cameras read inverted codes; switch back to dark=BG, light=TEXT if one does not.
QR_LIGHT = "#000000"
# Colours for the QR squares, picked in the window; all light enough to scan on black.
QR_COLORS = {"Sage": ACCENT, "Blue": "#7fb8e0", "Violet": "#b5a3e0", "Amber": "#e3b866",
             "Rose": "#e3a0b4", "Coral": "#e39a8a", "Teal": "#7cc7bd", "Sand": "#d6c7a8",
             "Lime": "#b5d66e", "Sky": "#8fd0ec", "Orchid": "#d79be0", "Peach": "#f0b48a",
             "Slate": "#a9b4c2", "Cream": TEXT}
SETTINGS_FILE = LOG_FILE.with_name("settings.json")


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_setting(name: str, value) -> None:
    settings = load_settings()
    settings[name] = value
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not save settings: %s", exc)

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CreateEventW.restype = wintypes.HANDLE
ERROR_ALREADY_EXISTS = 183
INFINITE = 0xFFFFFFFF
MUTEX_NAME = f"Local\\{APP_NAME}"
SHOW_EVENT_NAME = f"Local\\{APP_NAME}Show"


def log_to_file() -> None:
    """A windowed .exe has no console: print() and logging go to the log file instead."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        if LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            LOG_FILE.unlink()
    except OSError:
        pass
    stream = open(LOG_FILE, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = sys.stderr = stream


def take_instance_lock(wait: bool) -> bool:
    """True when this is the only copy running. Otherwise asks the running one to show its window.
    `wait`: this copy replaces one that is closing (update)."""
    deadline = time.monotonic() + (15 if wait else 0)
    while True:
        handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
            return True  # kept open until the process ends
        _kernel32.CloseHandle(handle)
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    event = _kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)
    _kernel32.SetEvent(event)
    _kernel32.CloseHandle(event)
    return False


def status_text(players: list[tuple[int | None, bool]]) -> tuple[str, str]:
    """(summary, details) for the controllers right now: (player number, away) each."""
    if not players:
        return "Waiting for players", ""
    count = "1 player" if len(players) == 1 else f"{len(players)} players"
    names = [f"Player {n}" + (" (away)" if away else "")
             for n, away in sorted(players, key=lambda p: p[0] or 9) if n is not None]
    return count, " · ".join(names)


class TrayApp:
    def __init__(self, argv: list[str]):
        self.argv = argv
        self.args = parse_args(argv)
        self.events: queue.Queue = queue.Queue()  # work for the Tk thread, from other threads
        self.root = tk.Tk()
        self.root.withdraw()
        self.server = None
        self.server_thread: threading.Thread | None = None
        self.icon = None
        self.quitting = False
        self.told_about_tray = False

    # ---------- start and stop ----------

    def run(self) -> int:
        try:
            self.setup = prepare(self.args, on_players=lambda p: self.events.put(("players", p)))
        except StartupError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return 1
        self.server = make_server(self.setup, log_config=None)
        self.server_thread = threading.Thread(target=self.server.run, name="server", daemon=True)
        self.server_thread.start()
        self.build_window()
        self.start_tray()
        threading.Thread(target=self.wait_for_show_requests, daemon=True).start()
        self.root.after(100, self.poll)
        self.show()
        self.root.mainloop()
        return 0

    def quit(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        if self.icon:
            self.icon.stop()
        if self.server:
            self.server.should_exit = True
        if self.server_thread:
            self.server_thread.join(5)
        if self.setup.mdns:
            self.setup.mdns.stop()
        self.root.destroy()

    def poll(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "players":
                self.show_players(value)
            elif kind == "show":
                self.show()
            elif kind == "log":
                sys.stdout.flush()
                os.startfile(LOG_FILE)  # opens in the default text editor
            elif kind == "quit":
                self.quit()
                return
        if self.server_thread and not self.server_thread.is_alive() and not self.quitting:
            messagebox.showerror(APP_NAME, f"{APP_NAME} stopped unexpectedly. Details are in\n"
                                             f"{LOG_FILE}")
            self.quit()
            return
        self.root.after(100, self.poll)

    def wait_for_show_requests(self) -> None:
        """Starting the .exe again shows this window instead (see take_instance_lock)."""
        event = _kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)
        while _kernel32.WaitForSingleObject(event, INFINITE) == 0:
            self.events.put(("show", None))

    # ---------- tray icon ----------

    def start_tray(self) -> None:
        import pystray
        from PIL import Image

        def later(kind):
            return lambda icon, item: self.events.put((kind, None))

        menu = pystray.Menu(
            pystray.MenuItem(f"Show {APP_NAME}", later("show"), default=True),
            pystray.MenuItem("Open log", later("log")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Quit {APP_NAME}", later("quit")),
        )
        image = Image.open(CLIENT_DIR / "icons" / "icon-192.png")
        self.icon = pystray.Icon(APP_NAME, image, f"{APP_NAME} · Waiting for players", menu)
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

    # ---------- window ----------

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def hide(self) -> None:
        self.root.withdraw()
        if not self.told_about_tray and self.icon:
            self.told_about_tray = True
            try:
                self.icon.notify(f"{APP_NAME} keeps running here. Click the icon to show it again.",
                                 APP_NAME)
            except Exception:  # notifications may be turned off
                log.info("Could not show the tray notification", exc_info=True)

    def build_window(self) -> None:
        s = self.setup
        root = self.root
        root.title(APP_NAME)
        root.configure(bg=BG, padx=28, pady=22)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        root.iconphoto(True, tk.PhotoImage(file=str(CLIENT_DIR / "icons" / "icon-192.png")))
        try:  # dark title bar (Windows 10 20H1 and later)
            root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
        except (AttributeError, OSError):
            pass
        scale = max(1.0, root.winfo_fpixels("1i") / 96)

        def label(parent, text, size=11, color=TEXT, bold=False, **kw):
            font = ("Segoe UI", size, "bold" if bold else "normal")
            return tk.Label(parent, text=text, font=font, fg=color, bg=parent["bg"], **kw)

        title = f"{APP_NAME} {__version__}" + ("  (dry run: no real controllers)" if s.args.dry_run else "")
        label(root, title, 16, bold=True).pack(anchor="w")
        self.status = label(root, "", 11)
        self.status.pack(anchor="w", pady=(2, 0))
        self.players = label(root, "", 10, MUTED)
        self.players.pack(anchor="w", pady=(0, 14))
        self.show_players([])

        # The QR code holds the encryption key, so it stays hidden until asked for.
        qr_area = tk.Frame(root, bg=BG)
        qr_area.pack(pady=(0, 12))
        qr_box = tk.Frame(qr_area, bg=BG)
        card = tk.Frame(qr_box, bg=QR_LIGHT, padx=6, pady=6)
        card.pack()
        qr = segno.make(s.qr_url, error="l", boost_error=False)
        qr_label = tk.Label(card, bg=QR_LIGHT, bd=0)
        qr_label.pack()
        label(qr_box, "Scan with the phone camera (encrypted connection)", 10, MUTED).pack(pady=(8, 0))

        color_row = tk.Frame(qr_area, bg=BG)
        color_row.pack(pady=(0, 10))
        swatches = {}

        def set_color(name):
            buffer = io.BytesIO()
            qr.save(buffer, kind="png", scale=round(6 * scale), border=3,
                    dark=QR_COLORS[name], light=QR_LIGHT)
            self.qr_image = tk.PhotoImage(data=base64.b64encode(buffer.getvalue()))
            qr_label.config(image=self.qr_image)
            for other, swatch in swatches.items():
                swatch.config(highlightbackground=TEXT if other == name else BORDER)
            pin_value.config(fg=QR_COLORS[name])  # the PIN shows the accent colour too

        def pick_color(name):
            set_color(name)
            save_setting("qr_color", name)

        for i, (name, color) in enumerate(QR_COLORS.items()):
            swatches[name] = tk.Button(
                color_row, bg=color, activebackground=color, relief="flat", bd=0, cursor="hand2",
                width=2, highlightthickness=2, command=lambda n=name: pick_color(n))
            swatches[name].grid(row=i // 7, column=i % 7, padx=3, pady=3)

        def toggle_qr():
            if qr_box.winfo_manager():
                qr_box.pack_forget()
                qr_button.config(text="Show QR encryption code")
            else:
                qr_box.pack(pady=(10, 0))
                qr_button.config(text="Hide QR encryption code")

        qr_button = self.button(qr_area, "Show QR encryption code", toggle_qr)
        qr_button.config(font=("Segoe UI", 12, "bold"), padx=24, pady=10)
        qr_button.pack()

        pin_row = tk.Frame(root, bg=BG)
        pin_row.pack(pady=(0, 14))
        label(pin_row, "PIN", 11, MUTED).pack(side="left", padx=(0, 12))
        pin_value = label(pin_row, f"{s.pin[:3]} {s.pin[3:]}", 26, bold=True)
        pin_value.pack(side="left")
        saved = load_settings().get("qr_color")
        set_color(saved if saved in QR_COLORS else "Sage")

        rows = [("Open on your phone", url(s.host, s.port))]
        if s.hostname:
            rows.append(("iPhone/iPad also", url(s.hostname, s.port)))
        if len(s.candidates) > 1:
            rows.append(("Other addresses", ", ".join(s.candidates[1:])))
        table = tk.Frame(root, bg=BG)
        table.pack(fill="x")
        for row, (name, value) in enumerate(rows):
            label(table, name, 10, MUTED).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=1)
            label(table, value, 10).grid(row=row, column=1, sticky="w", pady=1)

        if not s.args.dry_run and not driver_available():
            warn = tk.Frame(root, bg=SURFACE, padx=12, pady=10, highlightthickness=1,
                            highlightbackground=BORDER)
            warn.pack(fill="x", pady=(14, 0))
            label(warn, "Controller driver missing", 11, BAD, bold=True).pack(anchor="w")
            label(warn, f"{APP_NAME} needs the free ViGEmBus driver to act as Xbox controllers. "
                        f"Install it, then restart {APP_NAME}.", 10, MUTED,
                  wraplength=round(330 * scale), justify="left").pack(anchor="w")
            self.button(warn, "Download ViGEmBus", lambda: webbrowser.open(DRIVER_URL)).pack(anchor="w", pady=(8, 0))

        foot = tk.Frame(root, bg=BG)
        foot.pack(fill="x", pady=(16, 0))
        label(foot, f"Closing this window keeps {APP_NAME}\nrunning in the tray, next to the clock.",
              9, MUTED, justify="left").pack(side="left")
        self.button(foot, f"Quit {APP_NAME}", self.quit).pack(side="right")

    def button(self, parent, text, command) -> tk.Button:
        return tk.Button(parent, text=text, command=command, font=("Segoe UI", 10), fg=TEXT,
                         bg=SURFACE, activebackground=BORDER, activeforeground=TEXT, relief="flat",
                         bd=0, padx=12, pady=5, cursor="hand2", highlightthickness=1,
                         highlightbackground=BORDER)

    def show_players(self, players: list[tuple[int | None, bool]]) -> None:
        summary, details = status_text(players)
        self.status.configure(text=f"●  {summary}", fg=ACCENT if players else MUTED)
        self.players.configure(text=details)
        if self.icon:
            self.icon.title = f"{APP_NAME} · {summary}"[:127]  # Windows caps tooltips at 128 characters


def run(argv: list[str]) -> int:
    log_to_file()
    setup_logging()
    args = parse_args(argv)
    if not take_instance_lock(wait=args.replace or bool(os.environ.get(f"{APP_SLUG.upper()}_UPDATED"))):
        return 0
    if getattr(sys, "frozen", False):
        from .update import update_exe
        update_exe(argv)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled screens
    except (AttributeError, OSError):
        pass
    log.info("%s %s started (tray)", APP_NAME, __version__)
    try:
        return TrayApp(argv).run()
    except Exception:
        log.exception("%s crashed", APP_NAME)
        messagebox.showerror(APP_NAME, f"{APP_NAME} stopped because of an error. Details are in\n{LOG_FILE}")
        return 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
