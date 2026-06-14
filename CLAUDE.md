    # CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A browser host for a vendored **Subway Surfers: San Francisco** Unity 2019.4 WebGL build, controlled remotely. There is **no JavaScript game API** — the WASM blob exposes nothing. The *only* way to drive the game is to dispatch synthetic `KeyboardEvent`s onto the Unity `<canvas>`. Everything in this repo exists to get a keypress from some external input source onto that canvas.

Key codes (see `KEY_MAP` in `index.html` / `game.html`): left=37, up=38, right=39, down=40, space/jump=32, plus WASD. Input names used across the wire are `left|right|up|down|space|jump` with actions `press`/`release`.

The longer-term intent (per project notes) is camera/OpenCV gesture control; what's built today is keyboard, phone-over-WebSocket, and a test client.

## Running

The static files **must be served over HTTP** (not opened as `file://`) — the `.unityweb` assets are fetched relatively and the loader chain breaks otherwise. Serve from the repo root.

The relay needs Python with the `websockets` package. The repo's `.venv` has it (websockets 16.0); the system `python3` does **not**. Prefer the venv interpreter:

```bash
# Solo mode — one player, browser connects straight to the relay
.venv/Scripts/python.exe input_server.py                 # WebSocket relay on 0.0.0.0:8765
python -m http.server 8000 --bind 0.0.0.0                # static files
# then open http://localhost:8000  (index.html)

# 1v1 mode — split screen + two phone controllers
.venv/Scripts/python.exe input_server.py
python -m http.server 8000 --bind 0.0.0.0
# screen:   http://localhost:8000/1v1.html
# phones:   http://<computer-LAN-ip>:8000/controller.html   (?p=1 / ?p=2 to skip the lobby)

# Smoke-test the relay without a phone (sends a scripted press/release sequence)
.venv/Scripts/python.exe test_controller.py
```

`package.json` has `npm run solo` / `npm run 1v1`, but they invoke `python3`, which lacks `websockets` on this machine — they will fail. Use the manual commands above (or fix the scripts to point at a working interpreter).

For phones to reach the computer on the LAN: same Wi-Fi, allow Python through the **Windows Firewall (Private)**, and disable any VPN (e.g. Windscribe) that blocks local traffic.

## Architecture

Two distinct input paths share the relay and the same canvas-keypress sink:

**Solo (`index.html`)** — loads Unity *and* opens its own WebSocket to `ws://localhost:8765`. Relay messages (`{action,key}`) are turned directly into `KeyboardEvent`s on the canvas. Self-contained; no screen/postMessage hop.

**1v1 (`1v1.html` → `game.html`)** — `1v1.html` is the host: it registers with the relay as `role:"screen"`, shows two `game.html` iframes (P1 blue / P2 red) with "Waiting…" overlays, and **routes** each `input` message to the matching iframe via `postMessage`. `game.html` is a Unity panel that only listens for `postMessage` (no WebSocket of its own) and dispatches the keypress. So a phone press travels: `controller.html → relay → 1v1.html (screen) → postMessage → game.html → canvas`.

**`input_server.py`** — async `websockets` relay on `0.0.0.0:8765`, a pure fan-out hub with no game logic. First message is a register: `{type:"register",role:"screen"}` or `{type:"register",role:"controller",player:"p1"|"p2"}` (player optional → first free slot). It replies `assign` / `error:slot_taken` / `error:game_full`, broadcasts `player_connected` / `player_disconnected` so panels can toggle their overlays, and rebroadcasts each controller input as `{type:"input",player,action,key}` to everyone except the sender. Only two player slots exist.

**`controller.html`** — mobile-first phone controller. Lobby to pick P1/P2, then a themed 2×2 touch pad (LEFT/RIGHT/JUMP/ROLL) using pointer events. Derives the relay URL from `location.hostname`, auto-reconnects, and falls back to the lobby on `slot_taken`/`game_full`.

### Vendored game build — do not edit
`SanFrancisco.*.unityweb` (data/code blobs), `SanFrancisco.json` (manifest), `UnityLoader.2019.2.js`, `unity.js`, and the `4399.*.js` portal loaders are the shipped Unity WebGL build and its loader chain. `loader.js` selects the Unity loader from `window.config` (set inline in the HTML hosts). `poki-sdk.js` / `poki-sdk-core-*.js` are a stubbed Poki SDK the build calls into. Treat all of these as third-party — game behavior changes happen in the small custom files, not here.

### `subway-bridge.js` — score HUD + restart (injected into the game pages)
Custom overlay that draws a score/best-score HUD (`subway-ui.css`), tracks game phase, and recovers the live score two ways: hooking the Poki SDK highscore calls, and **OCR'ing the score digits off the canvas** (template-matching against rendered font masks, polled on an interval). Exposes `window.SubwayBridge.restart()` (clicks the canvas + sends Space/Enter) and emits `subway:*` CustomEvents. This is the place to hook score/phase/restart logic.

## Branch landscape

Branches have **diverged layouts** — orient yourself after a checkout:
- `feature/1v1` (this one) and `main`/`master`: flat repo root, files as above.
- `integrate-display`: the solo emulator + WebSocket transport reorganized under a `server/` tree (`server/index.js`, `server/public/`, `server/game/`). `main`/`master` and `websockets` were merged with unrelated histories.

Don't assume `server/` or `1v1.html` exists until you've checked the current branch.

## Notes

- `node_modules/` is committed-adjacent clutter left over from another branch's `npm install` and shows as untracked — ignore it; the runtime here is Python + static HTML, not Node.
- When changing the wire protocol, keep all four endpoints in sync: `input_server.py`, `controller.html`, `1v1.html`, and (for solo) `index.html`.
