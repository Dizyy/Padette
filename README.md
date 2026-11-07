<div align="center">

# JoyPad

### Your phone is the controller

Turn phones into Xbox controllers for your Windows PC. Friends come over, scan a QR code and play as
player 2, 3 or 4.<br>
**Runs in the phone's browser: no app to install, no account, no cloud.**

[![AGPL-3.0 License](https://img.shields.io/badge/license-AGPL--3.0-1f1f1f?style=flat-square)](LICENSE)
[![Windows](https://img.shields.io/badge/platform-Windows-1f1f1f?style=flat-square)](#what-you-need)

</div>

> [!NOTE]
> **Is it safe? Yes: no virus, no tracker, no ads.**
> - **Nothing is collected.** No accounts, no analytics, no ads, no tracking. JoyPad never sends your button presses or anything else to the internet.
> - **It only talks to GitHub,** once at start, to check for a new version (turn it off with `--no-update`). Everything else stays between the phones and your PC, on your own Wi-Fi.
> - **It needs one driver: ViGEmBus,** a free, open-source driver that lets programs add virtual Xbox controllers. You install it yourself, once. JoyPad never installs anything.
> - **Guests can't touch your PC.** A phone logged in to JoyPad can only press controller buttons: no mouse, no keyboard, no files.
> - **You can check every line.** The PC program and the phone page are open source, all in this repo.

## What you need

- A Windows 10 or 11 PC and phones on the **same Wi-Fi**.
- The **ViGEmBus** driver: download the installer from [its releases page](https://github.com/nefarius/ViGEmBus/releases) and run it once. The JoyPad window reminds you if it's missing.
- Any phone with a browser: iPhone, iPad or Android.

## How to play

1. **Start JoyPad on the PC.** A window shows a QR code and a PIN.
2. **Scan the QR code** with the phone's camera. The phone shows **Player 1** (or 2, 3, 4) in its own colour: that's the number games show.
3. **Turn the phone sideways** and play. The left half is the stick: put your thumb down anywhere and slide.
4. **Friends do the same.** Up to 4 players. Windows allows 4 Xbox controllers in total, so a real controller plugged in takes one of the spots.

Tip: on iPhone, tap Share → **Add to Home Screen**; JoyPad then opens full screen.

## Clip-on triggers and your own layout

Tap the **pencil** at the top. Now you can:

- **drag** any button to move it,
- **drag its corner** to make it bigger or smaller,
- turn on extras: **right stick** (camera), **D-pad**, **Guide**, **L3**, **R3**.

Tap **Done** to keep it. Got clip-on phone triggers? Put LB, RB, LT and RT exactly under the clips:
the corners are always free for them. The layout is saved on that phone.

## Good to know

- **Phone locked or Wi-Fi hiccup?** All buttons are released within a second, so nothing stays held. The controller stays plugged in for 30 seconds, so the game doesn't pause; come back in time and you're the same player.
- **Triggers are on/off**, because phones can't feel how hard you press.
- **Vibration** works on Android. iPhone browsers can't vibrate.

## Run from source

```bash
start.bat
```

It makes a Python environment in `.venv`, installs what's needed and starts the server. Try it
without the driver: `.venv\Scripts\python.exe -m server --dry-run` logs controller input instead.

Tests: `.venv\Scripts\python.exe tests\smoke_test.py`, `tests\pad_test.py`, `tests\players_test.py`,
`tests\update_test.py` and `node tests/layout_test.js`. The wire format is in [PROTOCOL.md](PROTOCOL.md).

## Credits

Built on [Mousette](https://github.com/Dizyy/Mousette) (same author): its login, encryption and PC
window. Virtual controllers by [ViGEmBus](https://github.com/nefarius/ViGEmBus) through
[vgamepad](https://github.com/yannbouteiller/vgamepad).

License: AGPL-3.0.
