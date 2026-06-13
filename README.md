# dashcam — WebSocket transport for an exercise-controlled Subway Surfers clone

The **phone** points a camera at the player, runs pose detection in-browser, and sends
*already-classified* actions to this server. The **display** (a browser tab on the same
computer that runs the server) renders the game and reads an interpreted input state. This
repo is the **WebSocket transport + networking layer only** — game logic and pose detection
are owned by teammates and are stubbed here.

> Design rationale lives in [PLAN.md](PLAN.md). This README is the operational + contract
> reference.

---

## Quick start

```bash
npm install        # Node 20+
npm start          # server on http://localhost:8080  (ws at /ws)
# or, with pretty human-readable logs:
npm run dev
```

Then, in **two more terminals**, drive it with the fake clients (no real phone/display needed):

```bash
# terminal 2 — fake display: creates a session, prints the pairing code, shows live state
npm run fake:display

# terminal 3 — fake phone: pair with the code printed above, run a scenario
npm run fake:phone -- --code 1234 --scenario obstacle_course
```

Open the **placeholder display page** in a browser instead of (or alongside) the fake
display: <http://localhost:8080/> — it shows the code huge and renders incoming state as
text. Live JSON snapshot of all sessions + reconstructed input state: <http://localhost:8080/debug>.

---

## What's implemented vs stubbed

**Implemented:** WS bootstrap (shared HTTP server, `/ws` endpoint), zod envelope + message
validation, session create/join/cleanup with 4-digit pairing codes, connection registry,
message dispatch, **InputState reconstruction with timeout-based decay** (running / lane /
plank), 30 Hz tick loop broadcasting `input:state`, one-shot event relay (`jump`/`duck`),
per-connection token-bucket rate limiting, ws ping/pong liveness + disconnect detection,
structured logging (pino), and a `/debug` route.

**Stubbed:** [`server/game/game.js`](server/game/game.js) — `onInput()`, `tick()`,
`getState()`, `onEvent()`, `dispose()`. The session calls these every tick so the wiring is
proven, but they do nothing. This is where the game-logic teammate plugs in.

---

## Fake clients

### `fake:display`
Creates a session and shows a single rewriting status line with the live input state
(running + intensity bar, lane `[ L | C | R ]`, plank, phone connection), plus a log of
discrete `jump`/`duck` events.

```bash
npm run fake:display              # live status line
npm run fake:display -- --verbose # print every input:state on its own line (good for piping)
```

### `fake:phone`
Scripted phone controller. `--code` is required for every scenario except `bad_payload`.

```bash
npm run fake:phone -- --code 1234 --scenario steady_run
npm run fake:phone -- --code 1234 --scenario obstacle_course
npm run fake:phone -- --scenario bad_payload        # no code needed
```

| Scenario | What it does | What to watch |
|---|---|---|
| `pair_only` | Join, then idle (no input). | Pairing works; `phone:status{connected:true}` on the display. |
| `steady_run` | `running_state` @10 Hz, `intensity=0.8`, for 30 s. | Display shows `RUN` on, intensity ~0.80 steadily. |
| `obstacle_course` | Scripted run: lane changes, jump, duck, plank, then stop. | Lane indicator moves, jump/duck events fire, plank toggles, runner stops at the end. |
| `bad_payload` | Sends 7 malformed messages, then a valid `ping`. | Server replies `error INVALID_PAYLOAD` to each and a `pong` at the end — **never crashes**. |
| `disconnect_mid_run` | Run @0.8, then hard-drop the socket after 1.5 s. | On the display, `running` decays to `0` within ~300 ms (`RUNNING_TIMEOUT_MS`). |
| `spam_actions` | Flood `running_state` @200 Hz for 3 s. | Server replies `error RATE_LIMITED` (throttled); no crash. |

Options: `--intensity <0..1>` and `--duration <s>` (steady_run), `--url <ws://…>`,
`--port <n>`.

---

## Test with a real phone + computer

You don't need the real pose-detection controller to test that actions go through — there's a
**manual controller page** (`server/public/controller.html`) with buttons/sliders that send the
same `phone:*` messages. It's `fakePhone` as a touch webpage.

1. **Start the server on the computer.** It binds `0.0.0.0` so it's reachable on your LAN:
   ```bash
   npm start          # http + ws on port 8080
   ```
2. **Find the computer's LAN IP** (the Wi-Fi adapter address — ignore VPN/Tailscale `100.x` ones):
   ```powershell
   Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' } | Select IPAddress, InterfaceAlias
   ```
3. **Open the display on the computer:** <http://localhost:8080/> — shows the big pairing code and
   live state. (Or run `npm run fake:display`, or watch <http://localhost:8080/debug>.)
4. **Open the controller on the phone** (must be on the **same Wi-Fi**):
   `http://<COMPUTER-LAN-IP>:8080/controller.html` — type the code, tap **JOIN**, then use the
   controls. Watch the display update in real time.

**Controls map to the contract:**
- **RUN** toggle → `running_state` @10 Hz (intensity slider). Toggle off (or lock the phone / walk
  out of Wi-Fi) and the runner stops within ~300 ms — that's the heartbeat-timeout design.
- **LEFT / CENTER / RIGHT** → `lane_position` (immediate + 5 Hz heartbeat).
- **PLANK** toggle → `plank_state`.
- **JUMP / DUCK** → one-shot events.

**If the phone can't connect:**
- **Same network?** Phone on Wi-Fi (not cellular), same SSID as the computer.
- **Windows Firewall** usually prompts on first run — allow Node on **Private** networks. If you
  missed it, add a rule (admin PowerShell): `New-NetFirewallRule -DisplayName "dashcam 8080" -Direction Inbound -LocalPort 8080 -Protocol TCP -Action Allow`
- **VPN** (e.g. Windscribe) on the computer can block LAN traffic — disable it for the test.
- **Guest/corporate Wi-Fi** often has "client isolation" that blocks phone↔computer entirely; use a
  phone hotspot or a home router instead.
- **Camera note (for later):** this manual controller is plain HTTP and works fine. The real
  pose-detection controller will need `getUserMedia`, which browsers only allow over **HTTPS or
  localhost** — so that page will need TLS (or a tunnel like ngrok) when a teammate builds it.

## Configuration

All tunables live in [`server/config.js`](server/config.js); every one is overridable by an
env var of the same name.

| Var | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | HTTP + WS port. |
| `HOST` | `0.0.0.0` | Bind address. |
| `TICK_HZ` | `30` | `input:state` broadcast rate. |
| `RUNNING_TIMEOUT_MS` | `300` | No `running_state` for this long → intensity snaps to 0. |
| `LANE_TIMEOUT_WARN_MS` | `500` | No `lane_position` for this long → warn (lane is held). |
| `PLANK_TIMEOUT_MS` | `400` | No `plank_state` for this long → jetpack forced off. |
| `RATE_LIMIT_PER_SEC` | `50` | Per-connection token bucket (capacity = rate). |
| `WS_PING_INTERVAL_MS` | `10000` | ws ping frame interval. |
| `WS_PING_TIMEOUT_MS` | `25000` | No pong within this → terminate dead socket. |
| `SESSION_CODE_LENGTH` | `4` | Pairing-code digit count. |
| `LOG_LEVEL` | `info` | pino level. |
| `LOG_PRETTY` | _(off)_ | Set to `1` for colorized logs (`npm run dev` does this). |

```bash
PORT=9000 RATE_LIMIT_PER_SEC=100 npm start
```

---

## Message contract

Every message — both directions — is a JSON envelope:

```jsonc
{ "type": "phone:running_state", "data": { "intensity": 0.8 }, "t": 1718200000000 }
```

- `type` — namespaced message type.
- `data` — type-specific payload (validated by zod; may be `{}`).
- `t` — **client** timestamp (ms). **Debug only** — the server never trusts it; it stamps its
  own `serverT` on everything it relays.

### client → server

| Type | From | Data | Notes |
|---|---|---|---|
| `display:create_session` | display | `{}` | Bootstrap. Server replies `session:created`. |
| `phone:join` | phone | `{ sessionCode: "1234" }` | Bootstrap. 4-digit string. |
| `phone:running_state` | phone | `{ intensity: 0..1 }` | Continuous heartbeat ~10 Hz. |
| `phone:lane_position` | phone | `{ lane: -1\|0\|1 }` | On change + heartbeat ~5 Hz. |
| `phone:plank_state` | phone | `{ active: bool }` | On change + heartbeat while active. |
| `phone:jump` | phone | `{}` | One-shot. |
| `phone:duck` | phone | `{}` | One-shot. |
| `ping` | either | `{}` | App-level probe; server replies `pong`. |

### server → display

| Type | Data | Notes |
|---|---|---|
| `session:created` | `{ sessionCode }` | Reply to `display:create_session`. |
| `phone:status` | `{ connected: bool }` | On phone join/leave. |
| `input:state` | `{ running, intensity, lane, plank, serverT }` | **The central contract.** Interpreted authoritative input, broadcast at `TICK_HZ` (30) regardless of inbound rate. Read every frame. |
| `input:event` | `{ type: "jump"\|"duck", serverT }` | Relayed one-shot events. |
| `error` | `{ code, message }` | See error codes below. |
| `pong` | `{ serverT }` | Reply to `ping`. |

### server → phone

| Type | Data | Notes |
|---|---|---|
| `session:joined` | `{ sessionCode }` | Reply to a successful `phone:join`. |
| `error` | `{ code, message }` | `SESSION_NOT_FOUND`, `SESSION_FULL`, `NOT_IN_SESSION`, … |
| `pong` | `{ serverT }` | Reply to `ping`. |

### error codes

| Code | Meaning |
|---|---|
| `INVALID_PAYLOAD` | Malformed JSON, bad envelope, unknown type, or schema failure. |
| `SESSION_NOT_FOUND` | `phone:join` with an unknown code. |
| `SESSION_FULL` | `phone:join` to a session that already has a phone. |
| `NOT_IN_SESSION` | Gameplay message from a connection that hasn't joined. |
| `RATE_LIMITED` | Token bucket exhausted (throttled so it doesn't flood). |
| `INTERNAL` | Defensive: a handler threw unexpectedly. Socket stays open; server never crashes. |

### reserved for the game team (NOT implemented)

`game:state`, `game:event`, `game:over` — server → display. The `Game` stub will emit these
later via its `ctx.emit(...)`; the transport already knows how to route messages to the
display, so no transport changes should be needed.

---

## Continuous vs discrete actions (the important bit)

This is **not** a fire-and-forget boxing game. The runner moves only while the player keeps
doing high knees, so **continuous state is reconstructed from heartbeats, not per-event**:

- `running_state` / `lane_position` / `plank_state` are sent repeatedly while the state holds.
  The server folds the stream into one authoritative `InputState` and applies timeouts.
- **The absence of a signal is itself a signal:** if the phone disconnects or the player
  stops, heartbeats cease and `running` times out to 0 within `RUNNING_TIMEOUT_MS` (300 ms).
  No explicit "stop" message is needed, and the jetpack can't get stuck on if the phone dies.
- `jump` / `duck` are the only true one-shot events.

The `disconnect_mid_run` fake-phone scenario demonstrates the self-healing timeout directly.

---

## Project layout

```
server/
├── index.js              # http + ws + express + static + /debug bootstrap
├── config.js             # all tunables (env-overridable)
├── sessions/             # sessionManager (codes, create/join/cleanup) + session (tick loop)
├── game/game.js          # STUB seam for game logic
├── input/inputState.js   # heartbeat -> authoritative state + timeouts
├── net/                  # messages (zod), envelope, handlers, connection, rateLimiter, heartbeat
├── public/index.html     # placeholder display page (teammate replaces with the real renderer)
├── scripts/              # fakePhone.js + fakeDisplay.js
└── utils/                # logger (pino) + timing (monotonic clock + deadline tick scheduler)
```
