# JoyPad protocol, version 1

This describes the messages between the PC server and the web page in `client/`. Server-side validation lives in `server/protocol.py`.

## Transport

- WebSocket at `ws://joypad.local/ws` or `ws://<pc-lan-ip>/ws`. The port is 80, or 8765 when 80 is taken (choose another with `--port`).
- Each WebSocket **text** frame holds one JSON object, UTF-8 encoded. On an encrypted connection (see [Encryption](#encryption)), every frame after the handshake is a **binary** frame holding one encrypted JSON object instead.
- Every message, in both directions, has `"v": 1` and a `"type"` string.
- The server only accepts connections from private/LAN addresses (10/8, 172.16/12, 192.168/16, link-local, loopback).
- A frame can be at most 16 KB.
- `GET /name` returns `{"name": "joypad.local"}` (with `:port` when not 80), or `{"name": null}` without a .local name. The QR code links to the IP address, which every phone can open; on iPhone/iPad the web client then moves to this name when it answers within 2.5 s, keeping the `#` part. The server keeps the PIN of such a move: its `Referer` is one of the server's own addresses (pages are served with `Referrer-Policy: origin`).

## Session flow

```
client                                   server
  | --- connect ws://pc/ws --------------> |
  | --- {"v":1,"type":"auth","pin":"…"} -> |   must be the first message, within 10 s
  | <-- {"v":1,"type":"auth_ok",…} ------- |   or auth_failed + close
  | --- pad / ping --------------------->  |
  | <-- player / pong / error ----------- |
```

1. Open the WebSocket.
2. Send `auth` as the **first** message. If the first message is anything else, or nothing arrives within 10 s, the server replies with `error` (`auth_required`) and closes the socket with code **4001**.
3. On success the server replies `auth_ok`. All other message types are accepted only after this.
4. The server does **not** acknowledge controller messages, which keeps latency low. It replies only to `ping` (with `pong`) and to rejected messages (with `error`). Messages are processed strictly in the order they arrive. So a `pong` also means every message sent before its `ping` has been handled.

The PIN is a random 6-digit string. The server prints it at startup, and it changes every time the server restarts. After 5 wrong PINs from one IP address, that address is locked out for 60 s. After 10 wrong PINs from all addresses together within 60 s, every address is locked out until that window has passed.

The server only accepts requests whose `Host` header is the address it printed, and refuses WebSocket handshakes whose `Origin` is not that same address (close code 4031). This stops other websites and DNS-rebinding tricks from reaching it through a browser.

## Client → server messages

| type | fields | effect |
|---|---|---|
| `auth` | `pin`: string, **or** `token`: string | Authenticate. Must be first (or first after the encryption handshake). `token` is one an earlier `auth_ok` returned; it lets a client log in again without the PIN. |
| `enc_hello` | `nonce`: 16 random bytes, base64url without padding | Start an encrypted connection. Only allowed as the very first message; see [Encryption](#encryption). |
| `ping` | `id`: number or string, optional | Server replies `pong` with the same `id`. |
| `pad` | `b`: button bitmask; `lx`, `ly`, `rx`, `ry`: −1…1; `lt`, `rt`: 0…1 (all required) | The whole controller state. Send it whenever it changes (at most 60 per second) and repeat it at least every 250 ms. The server releases the controller (no buttons, sticks centred) after 1 s without a message, and when the socket closes. Sticks: +x = right, **+y = up**. No reply. |

Unknown extra fields are ignored, so later clients can add optional fields without breaking older servers.

## Server → client messages

| type | fields | meaning |
|---|---|---|
| `enc_challenge` | `nonce`: 16 random bytes, base64url without padding | Reply to `enc_hello`, sent unencrypted. Everything after it is encrypted in both directions. |
| `auth_ok` | `protocol`: 1; `token`: string (only after a PIN); optional `direct`: the server's IP address and port | Authenticated; input is accepted from now on. Keep `token` to log in again later: it is valid until the server stops. If the page's host name (`joypad.local`) stops resolving, connect to `ws://<direct>/ws` instead: the server accepts its own page from any of its addresses. |
| `auth_failed` | `reason`: `"wrong_pin"`, `"token_expired"` (the server restarted since the token was issued) or `"locked_out"`; `retry_after`: seconds (only when locked out) | The socket closes right after this. |
| `pong` | `id`: the id from the ping, or null | Reply to `ping`. |
| `player` | `n`: 1–4 | This phone is Windows' player `n` (the number games show). Sent after login, and again if it changes. |
| `full` | none | All 4 controller spots are taken (real controllers count too). The phone gets no controller. |
| `driver_missing` | none | The controller driver (ViGEmBus) is not installed on the PC. |
| `rumble` | `large`, `small`: 0–255 | The game made the controller vibrate (large and small motor strength). `0, 0` = stop. |
| `error` | `code`, `message`, optional `ref` | The message was rejected. The connection stays open, except for `auth_required`. |

### Error codes

| code | cause |
|---|---|
| `bad_json` | The frame is not a JSON object. |
| `bad_version` | `v` is missing or is not `1`. |
| `unknown_type` | `type` is not one listed above. |
| `bad_field` | A field has the wrong type or is out of range. |
| `auth_required` | Another message was sent before `auth`. The socket then closes with 4001. |
| `no_encryption` | `enc_hello` was sent, but the server has no encryption key. The socket then closes with 4001. |
| `bad_state` | `enc_hello` was sent after the connection had started. |

### Close codes

| code | meaning |
|---|---|
| 4001 | Not authenticated (no or late `auth`, or another message first). |
| 4003 | Wrong PIN. |
| 4029 | Too many wrong PINs; wait `retry_after` seconds. |
| 4030 | The connection did not come from a local network address. |
| 4031 | The connection came from another website's page, or used a different host name. |
| 4032 | An encrypted connection received a frame that doesn't decrypt (wrong key, changed, replayed or out-of-order frame), or an unencrypted frame. Nothing in it was used. A client that gets this after its handshake should drop its saved key and pair again. |

## Encryption

Optional, and used by the web client whenever it has the key. It keeps everything sent over the network (PIN and every button press) unreadable to others on the same network, and stops recorded traffic from being replayed.

**The key.** The server keeps one random 32-byte key `K` in a file (`%APPDATA%\JoyPad\key`), so phones stay paired across restarts. `--new-key` replaces it, which unpairs every phone. The QR code carries `K` after `#` (`http://<pc-ip>/#p=123456&k=<base64url K>`; the web client also accepts the older `#pin=…&key=…`): browsers never send that part over the network, so `K` reaches the phone only through the camera. A PIN typed by hand doesn't give the phone `K`; such connections stay unencrypted. On iPhone/iPad, where a home-screen app gets its own empty storage, the web client keeps `#k=<K>` (never the PIN) in Safari's address once it has `K`, so an icon added from there carries `K` into the app.

**Handshake.** The client sends `{"v":1,"type":"enc_hello","nonce":cn}` as its first message, with 16 fresh random bytes `cn`. The server replies `{"v":1,"type":"enc_challenge","nonce":sn}` with 16 fresh random bytes `sn`. Both then compute the session key

```
session_key = SHA-512("JoyPad session v1" || K || sn || cn)[0:32]
```

**Frames.** From then on, every message in both directions is the usual JSON object, UTF-8 encoded and sealed with NaCl `secretbox` (XSalsa20-Poly1305) under `session_key`, sent as one binary frame (ciphertext with its 16-byte tag; the nonce is not sent). The 24-byte nonce is

```
nonce = direction (1 byte: 0 = client to server, 1 = server to client)
        || counter (8 bytes, little-endian: 0 for the first frame in that direction, then 1, 2, ...)
        || 15 zero bytes
```

So a frame that is replayed, dropped, reordered or changed fails to open, and the connection is closed (4032). The first encrypted client message must be `auth`, as on an unencrypted connection.

### Button bits (`b`)

The values Windows uses for Xbox controllers (XUSB), added together:

| button | bit | button | bit |
|---|---|---|---|
| D-pad up | `0x0001` | LB | `0x0100` |
| D-pad down | `0x0002` | RB | `0x0200` |
| D-pad left | `0x0004` | Guide | `0x0400` |
| D-pad right | `0x0008` | A | `0x1000` |
| Menu (Start) | `0x0010` | B | `0x2000` |
| View (Back) | `0x0020` | X | `0x4000` |
| L3 (left stick click) | `0x0040` | Y | `0x8000` |
| R3 (right stick click) | `0x0080` | | |

`0x0800` is unused; a bitmask with it, or above `0xFFFF`, is refused (`bad_field`).

## Behaviour notes for client authors

- **Connection health:** send `ping` every ~2 s. If no `pong` arrives within ~6 s, treat the socket as dead and reconnect. Mobile operating systems often keep dead sockets "open" after the phone sleeps.
- **Reconnecting:** use a new socket and authenticate again with the `token` from `auth_ok`. The web client saves the token (never the PIN), because iOS often reloads the page after the phone was locked. On `token_expired` or `wrong_pin`, drop the token and ask the user for the PIN again (the server has restarted). Wrong tokens don't count towards the PIN lockout, and a token still works while the PIN is locked out.
## Versioning

`v` is the protocol version. Changes that keep compatibility (new optional fields, new message types) keep `v: 1`. Any change that alters how an existing message behaves bumps `v`. The server answers messages with a version it doesn't support with `bad_version`.
