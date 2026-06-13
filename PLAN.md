# PLAN.md — Exercise-Controlled Subway Surfers: WebSocket Transport Layer

**Scope of this document:** the WebSocket transport + networking layer only.
Game logic (obstacles, scoring, collision, lives) and pose detection are owned by
teammates and are **out of scope**. This layer provides a clean transport with
well-defined stub seams where game logic plugs in later.

**Status:** design proposal. No implementation until approved.

---

## 1. Architecture Overview

Three logical participants, two physical machines:

| Participant | Runs on | Responsibility |
|---|---|---|
| **Phone** (controller) | Player's phone browser | Camera + OpenCV pose detection in-browser. Classifies movements and sends **already-classified actions** over WebSocket. Renders **nothing** game-related. |
| **Display** (renderer) | Computer browser tab | Renders the game canvas. Connects to the same server, reads interpreted input state every frame. Owned by a teammate (game renderer). |
| **Server** (this build) | Node.js on the computer | WebSocket transport, session pairing, input-state reconstruction, 30 Hz broadcast to display, static file host for the display page. |

The computer is both the **server host** and the **display**. The display is just a
browser tab on the same machine pointing at the local server.

### Connection topology

```
                         ┌─────────────────────────────────────────────┐
                         │            Computer (server host)            │
                         │                                              │
   ┌───────────┐         │   ┌────────────────────────────────────┐    │
   │   Phone    │  WS    │   │        Node.js Server              │    │
   │ (browser)  │◀──────────▶│  http + express + ws (shared srv)  │    │
   │            │ /ws    │   │                                    │    │
   │ OpenCV pose│        │   │  ┌──────────────────────────────┐  │    │
   │ detection  │        │   │  │ SessionManager               │  │    │
   └───────────┘         │   │  │  └─ Session (1 phone+display)│  │    │
                         │   │  │       ├─ InputState          │  │    │
   ┌───────────┐         │   │  │       └─ Game (STUB)         │  │    │
   │  Display   │  WS    │   │  └──────────────────────────────┘  │    │
   │ (browser   │◀──────────▶│  30 Hz tick → input:state          │    │
   │  tab, same │ /ws    │   │                                    │    │
   │  machine)  │        │   │  GET /         → public/index.html │    │
   │ renders    │◀──────────│  GET /debug    → live session JSON │    │
   │ game       │  HTTP  │   └────────────────────────────────────┘    │
   └───────────┘         └─────────────────────────────────────────────┘
```

### Message flow (happy path)

```
Display                         Server                          Phone
   │                              │                               │
   │── display:create_session ──▶ │                               │
   │ ◀── session:created {code} ──│   (e.g. code = "4821")        │
   │                              │                               │
   │     ...shows code "4821"...  │ ◀──── phone:join {"4821"} ─────│
   │                              │──── session:joined ──────────▶│
   │ ◀── phone:status{conn:true} ─│                               │
   │                              │                               │
   │                              │ ◀── phone:running_state 10Hz ─│
   │                              │ ◀── phone:lane_position  5Hz ─│
   │                              │ ◀── phone:jump (one-shot) ────│
   │                              │                               │
   │ ◀── input:state @ 30Hz ──────│  (reconstructed authoritative │
   │ ◀── input:event {jump} ──────│       state, every tick)      │
   │                              │                               │
```

**Key idea:** the phone sends *classified intents at its own cadence*; the server
reconstructs a single authoritative `InputState` and pushes it to the display at a
fixed 30 Hz. The display never talks to the phone directly and never sees raw
phone messages — it only consumes `input:state` / `input:event`.

---

## 2. Session Model

**Decision: Option (b) — short 4-digit pairing code.** ✅

### Why
- **Cost is marginal.** Code generation + a `Map<code, Session>` lookup is ~20 lines
  more than a single global session.
- **Prevents the demo disaster.** At a hackathon expo booth there will likely be
  *multiple* setups on the same network, or someone refreshing/reconnecting. A global
  session means "any phone controls any game" — the classic live-demo embarrassment.
  A pairing code guarantees the right phone drives the right display.
- **Mirrors the mental model** users already have from Jackbox / Chromecast-style
  "type the code on your phone" pairing. Zero explanation needed at the booth.

### How it works
1. The **display** is the session *owner*. On connect it sends `display:create_session`.
2. Server generates a unique 4-digit code (`SESSION_CODE_LENGTH`, configurable),
   creates a `Session`, and replies `session:created {sessionCode}`.
3. The display shows the code huge on screen.
4. The **phone** connects and sends `phone:join {sessionCode}`.
5. Server attaches the phone to that session (if it exists and has no phone yet).

### Single-player invariants
- **Exactly one phone and one display per session.** A second `phone:join` to a
  session that already has a live phone → `SESSION_FULL` error (no kick, no swap).
- **The display owns the lifecycle.** Display disconnect tears down the session.
- Codes are case-insensitive digits, collision-checked against active sessions on
  generation, and freed on session teardown so the small 4-digit space (10⁴ = 10000)
  is reused.

---

## 3. Continuous vs Discrete Actions — *the most important section*

### The core insight

This is **not** a boxing game. In a boxing game, a punch is a fire-and-forget event:
it happens once, it lands, done. Here the runner must **keep moving only while the
player keeps doing high knees**. The control signal is a *sustained physical state*,
not a momentary gesture. So we cannot model everything as one-shot events.

We split the action vocabulary into two categories with fundamentally different
transport semantics:

### A. Continuous state signals (heartbeat-like, sent while the state is active)

These represent an *ongoing physical state*. The phone sends them repeatedly while
the state holds. The **server reconstructs the current value from the stream of
heartbeats** — it does not treat each one as a discrete event.

| Signal | Payload | Cadence | Meaning |
|---|---|---|---|
| `running_state` | `{intensity: 0..1}` | ~10 Hz (every ~100 ms) while running | High-knee cadence. `0` = standing still, `1` = full pace. |
| `lane_position` | `{lane: -1\|0\|1}` | on transition **+** heartbeat ~5 Hz (every ~200 ms) | Body lean: left / center / right. |
| `plank_state` | `{active: bool}` | on transition **+** heartbeat while active | Plank hold = jetpack on/off. |

### B. Discrete events (one-shot, sent at the moment of detection)

These are momentary gestures with no sustained state. Fire once, relay once.

| Event | Payload | Meaning |
|---|---|---|
| `jump` | `{}` | Player jumped. |
| `duck` | `{}` | Player ducked / rolled. |

### Why this is the right design — the self-healing property

Because continuous state is **reconstructed from heartbeats with timeouts**, the
system is naturally fault-tolerant:

> **If the phone disconnects, freezes, or the player simply stops, the heartbeats
> stop arriving. The server's `running_state` times out after `RUNNING_TIMEOUT_MS`
> (300 ms) and the runner stops on its own — no explicit "stop" message required.**

This is a deliberate and important property. A naive "send `run_start` / `run_stop`"
event design would leave the runner sprinting forever if the `stop` packet were lost
or the phone died mid-stride. Heartbeat-reconstructed state has no such failure mode:
*the absence of signal is itself the signal.* The same logic protects the jetpack
(`plank_state` timeout) — we never want to leave the jetpack stuck "on" because a
phone died.

The cost is bandwidth (10–15 small messages/sec), which is trivial and well within
our rate-limit headroom (§9).

---

## 4. Connection Lifecycle

### Session state machine

```
                      ┌──────────────────────────────────────────────┐
                      │                                              │
   display connects   ▼                                              │
   + create_session ┌──────────────┐  phone:join (valid code)  ┌─────────────┐
  ─────────────────▶│   WAITING    │──────────────────────────▶│   PAIRED    │
                    │ (display only│                            │(phone +     │
                    │  no phone)   │◀───────────────────────────│ display)    │
                    └──────────────┘   phone disconnects        └─────────────┘
                      │   │                                       │   │   ▲
   empty TTL expires  │   │ display disconnects                   │   │   │ phone
   (no phone joined)  │   │                                       │   │   │ rejoins
                      ▼   ▼                                       │   │   │ (same code)
                    ┌──────────────────────────────────┐         │   │   │
                    │            DESTROYED              │◀────────┘   │   │
                    │ (code freed, sockets closed,      │  display    │   │
                    │  Game.dispose(), removed from map)│  disconnects │   │
                    └──────────────────────────────────┘             │   │
                                                                      └───┘
```

### Lifecycle events and how each is handled

| Event | Handling |
|---|---|
| **Display connects + `display:create_session`** | New `Session` in `WAITING`. Reply `session:created`. Tick loop starts; `input:state` broadcasts immediately (neutral state, `running:false`). |
| **Phone connects + `phone:join {code}`** | If code unknown → `SESSION_NOT_FOUND`. If session already has a live phone → `SESSION_FULL`. Else attach, `PAIRED`. Reply `session:joined` to phone, `phone:status{connected:true}` to display. |
| **Phone disconnects** | Session → `WAITING` (display stays). `phone:status{connected:false}` to display. InputState times out to neutral within 300 ms. Code stays valid so the *same* phone can rejoin. |
| **Phone reconnects** | New socket sends `phone:join {sameCode}`. Treated as a fresh join into the now-empty phone slot. (No session-resume protocol; the runner just resumes when heartbeats return — see §3.) |
| **Display disconnects** | Session `DESTROYED`. Phone socket (if any) closed with a close reason. Code freed. This is intentional: the display is the host; no display = no game. |
| **`phone:join` on full session** | `SESSION_FULL` error to the joining phone; existing phone untouched. |
| **`phone:join` unknown code** | `SESSION_NOT_FOUND` error. Socket stays open (phone can retry with a corrected code). |
| **Phone sends gameplay msg before joining** | `NOT_IN_SESSION` error. Socket stays open. |
| **Empty `WAITING` session, no phone ever joins** | Reaped after `SESSION_EMPTY_TTL_MS` if the display also disconnects; otherwise lives as long as the display socket. |

**Connection ≠ session membership.** A raw `ws` socket connects to `/ws` first
(role unknown). Its role (phone vs display) and session binding are established by the
*first message* it sends (`display:create_session` or `phone:join`). Until then it's
an unbound connection that may only send `ping` / the two bootstrap messages.

---

## 5. Message Contract

### Envelope

Every message — both directions — is a single JSON object:

```jsonc
{
  "type": "phone:running_state",   // string, required, namespaced
  "data": { "intensity": 0.8 },     // object, type-specific (may be {})
  "t":    1718200000000            // client wall-clock ms (Date.now()), optional
}
```

- `type` — namespaced message type (`display:*`, `phone:*`, `session:*`, `input:*`,
  `game:*`, plus bare `ping`/`pong`/`error`).
- `data` — type-specific payload, validated by a per-type zod schema.
- `t` — **client** timestamp in ms. **For debug/telemetry only.** The server never
  trusts it for logic; the server stamps its own `serverT` on everything it relays
  (§11).

### Zod schemas (proposed)

```js
import { z } from "zod";

// ---- shared ----
const Lane = z.union([z.literal(-1), z.literal(0), z.literal(1)]);
const Unit = z.number().min(0).max(1);

// ---- envelope (validated first, before type-specific data) ----
export const Envelope = z.object({
  type: z.string().min(1),
  data: z.unknown().optional().default({}),
  t:    z.number().finite().optional(),
});

// ---- client → server: data schemas, keyed by type ----
export const ClientData = {
  "display:create_session": z.object({}).strict(),

  "phone:join":             z.object({ sessionCode: z.string().regex(/^\d{4}$/) }).strict(),
  "phone:running_state":    z.object({ intensity: Unit }).strict(),
  "phone:lane_position":    z.object({ lane: Lane }).strict(),
  "phone:plank_state":      z.object({ active: z.boolean() }).strict(),
  "phone:jump":             z.object({}).strict(),
  "phone:duck":             z.object({}).strict(),

  "ping":                   z.object({}).strict(),
};

// ---- server → client: data schemas (documented; server constructs these) ----
export const ServerData = {
  // → display
  "session:created": z.object({ sessionCode: z.string() }),
  "phone:status":    z.object({ connected: z.boolean() }),
  "input:state":     z.object({
    running:   z.boolean(),
    intensity: Unit,
    lane:      Lane,
    plank:     z.boolean(),
    serverT:   z.number(),
  }),
  "input:event":     z.object({
    type:    z.enum(["jump", "duck"]),
    serverT: z.number(),
  }),

  // → phone
  "session:joined":  z.object({ sessionCode: z.string() }),

  // → either
  "error":           z.object({ code: z.string(), message: z.string() }),
  "pong":            z.object({ serverT: z.number() }),
};
```

The dispatcher validates the **envelope first**, then looks up `ClientData[type]` and
validates `data`. Unknown `type` or failing `data` → `INVALID_PAYLOAD` error (§8).

> **Note / addition to the spec:** the original spec enumerated server→display
> messages but the **phone also needs replies**. I'm adding `session:joined` (phone
> join ack) plus `error` and `pong` to the phone. Without a join ack the phone can't
> distinguish "joined" from "code rejected." Flagged for your review.

### Full message table

#### Client → Server

| Type | From | Data | Notes |
|---|---|---|---|
| `display:create_session` | display | `{}` | Bootstrap. First msg from a display. |
| `phone:join` | phone | `{ sessionCode }` | Bootstrap. First gameplay msg from a phone. |
| `phone:running_state` | phone | `{ intensity: 0..1 }` | Continuous heartbeat ~10 Hz. |
| `phone:lane_position` | phone | `{ lane: -1\|0\|1 }` | On change + heartbeat ~5 Hz. |
| `phone:plank_state` | phone | `{ active: bool }` | On change + heartbeat while active. |
| `phone:jump` | phone | `{}` | One-shot. |
| `phone:duck` | phone | `{}` | One-shot. |
| `ping` | either | `{}` | App-level liveness probe (distinct from WS ping frames, §10). Server replies `pong`. |

#### Server → Display

| Type | Data | Notes |
|---|---|---|
| `session:created` | `{ sessionCode }` | Reply to `display:create_session`. |
| `phone:status` | `{ connected: bool }` | On phone join/leave. |
| `input:state` | `{ running, intensity, lane, plank, serverT }` | **The central contract.** Interpreted authoritative input state, broadcast at `TICK_HZ` (30) regardless of inbound rate. Display reads this every frame. |
| `input:event` | `{ type: "jump"\|"duck", serverT }` | Relayed one-shot events. |
| `error` | `{ code, message }` | See §8 codes. |
| `pong` | `{ serverT }` | Reply to `ping`. |

#### Server → Phone

| Type | Data | Notes |
|---|---|---|
| `session:joined` | `{ sessionCode }` | Reply to successful `phone:join`. |
| `error` | `{ code, message }` | e.g. `SESSION_NOT_FOUND`, `SESSION_FULL`, `NOT_IN_SESSION`. |
| `pong` | `{ serverT }` | Reply to `ping`. |

### Reserved namespace — stubbed for later (NOT implemented this phase)

These belong to the game-logic teammate. We reserve the names and the seam (§12) but
do **not** implement them:

| Type | Direction | Future meaning |
|---|---|---|
| `game:state` | server → display | Full game world state (runner pos, obstacles, score) per tick. |
| `game:event` | server → display | Discrete game events (coin pickup, near-miss, power-up). |
| `game:over` | server → display | Run ended (collision / lives exhausted), final score. |

---

## 6. Server Tick Loop

A single fixed **30 Hz** (`TICK_HZ`) loop per session. **In this phase it does exactly
one thing:**

1. Compute the current `InputState` for the session (applying timeout/decay rules, §7).
2. Broadcast `input:state` to the display.
3. *(Call `Game.tick()` — a no-op stub today, §12 — so the wiring is proven.)*

### Deadline-based scheduler (no drift)

Naïve `setInterval(fn, 1000/30)` accumulates drift and jitter. Instead we schedule each
tick against an absolute deadline:

```
nextDeadline = startTime
loop:
  now = monotonicNow()              // performance.now()-based, not Date.now()
  runTick()
  nextDeadline += TICK_INTERVAL_MS   // 33.333… ms, fixed step
  delay = max(0, nextDeadline - monotonicNow())
  setTimeout(loop, delay)
```

- Deadlines advance by a **fixed step** (`nextDeadline += interval`), so transient
  scheduling lateness self-corrects instead of compounding.
- If we ever fall badly behind (e.g. `nextDeadline` is far in the past after a stall),
  we resync `nextDeadline = now` and log a warning rather than firing a burst of
  catch-up ticks.
- One loop **per active session** (we expect 1 at a time, but it generalizes cleanly).
- The loop starts when the display creates the session and stops on teardown.

`input:state` is broadcast **every tick regardless of whether any phone message
arrived** — the display always has a fresh, authoritative frame to read. When no phone
is connected the broadcast carries the neutral state (`running:false, intensity:0,
lane:0, plank:false`).

---

## 7. InputState Reconstruction

The server maintains one `InputState` per session, updated by inbound heartbeats and
**evaluated lazily at tick time** against the current clock. "Lazy at tick time" means
each tick computes the effective state from *when each signal was last seen*, so
timeouts apply correctly even between messages.

State shape:

```js
{
  running:   { intensity: number, lastSeen: ms },   // 0..1
  lane:      { value: -1|0|1,     lastSeen: ms },
  plank:     { active: boolean,   lastSeen: ms },
}
```

### Reconstruction rules

| Field | On heartbeat | On staleness | Threshold (config) |
|---|---|---|---|
| **running.intensity** | set to received `intensity`, update `lastSeen` | if `now - lastSeen > RUNNING_TIMEOUT_MS` → **intensity = 0** (`running:false`) | `RUNNING_TIMEOUT_MS = 300` |
| **lane.value** | set to received `lane`, update `lastSeen` | **hold** last value (a lane is a *position*; last-known is the best guess), but **log a warning** if `now - lastSeen > LANE_TIMEOUT_WARN_MS` | `LANE_TIMEOUT_WARN_MS = 500` |
| **plank.active** | set to received `active`, update `lastSeen` | if `now - lastSeen > PLANK_TIMEOUT_MS` → **active = false** (safety: never leave the jetpack on if the phone died) | `PLANK_TIMEOUT_MS = 400` |

The `input:state` derived each tick:

```
running   = intensity > 0
intensity = (now - running.lastSeen > RUNNING_TIMEOUT_MS) ? 0 : running.intensity
lane      = lane.value                       // held; warn-only on staleness
plank     = (now - plank.lastSeen > PLANK_TIMEOUT_MS) ? false : plank.active
```

### Decay vs hard reset (a decision to confirm)

The spec says intensity "decays toward 0." I propose **hard reset to 0** on timeout for
phase 1 (simplest, deterministic, easiest to reason about), and leave any *visual*
smoothing to the display renderer. If you'd prefer a server-side linear decay ramp
(e.g. fade 1→0 over `RUNNING_DECAY_MS` once stale), it's a small addition gated behind
a config flag. **Listed as an open question (§14).** Default recommendation: hard reset.

All thresholds live in `config.js` and are env-overridable.

---

## 8. Validation & Error Handling

**Iron rule: the server never crashes on bad input.** Every inbound message is:

1. JSON-parsed inside try/catch (parse failure → `INVALID_PAYLOAD`, never throws up).
2. Envelope-validated with zod.
3. Type-looked-up; unknown type → `INVALID_PAYLOAD`.
4. `data`-validated with the per-type zod schema; failure → `INVALID_PAYLOAD`.
5. Dispatched to its handler **inside a top-level try/catch**. Any thrown error is
   logged with context and converted to an `error` message — the socket stays open,
   the process stays up.

On any validation failure the server: sends an `error` event to that connection, logs
at `warn` with the offending type + zod issue, and **does not disconnect**.

### Error codes

| Code | Meaning | Triggered by |
|---|---|---|
| `INVALID_PAYLOAD` | Malformed JSON, bad envelope, unknown type, or schema failure. | Any inbound message. |
| `SESSION_NOT_FOUND` | `phone:join` with a code that has no active session. | `phone:join`. |
| `SESSION_FULL` | `phone:join` to a session that already has a live phone. | `phone:join`. |
| `NOT_IN_SESSION` | Gameplay message from a connection not bound to a session. | `phone:running_state` etc. before `phone:join`. |
| `RATE_LIMITED` | Token bucket exhausted (§9). | Any inbound message over budget. |

`error` payload: `{ code, message }` where `message` is a short human-readable string
(safe to show in the fake clients / debug page; never echoes raw user input).

---

## 9. Rate Limiting

**Per-connection token bucket.** Limits are intentionally **generous** because
heartbeats are frequent and legitimate.

- **Capacity / refill:** `RATE_LIMIT_PER_SEC = 50` tokens/sec, bucket capacity 50.
  - Expected legitimate load: ~10 Hz running + ~5 Hz lane + occasional plank/jump ≈
    **15–20 msg/sec**. 50/sec gives ~2.5× headroom for bursts.
- **Scope:** applies to `phone:*` gameplay messages. `ping` and the bootstrap messages
  (`display:create_session`, `phone:join`) share the same bucket but are far below the
  limit in practice.
- **Over budget:** drop the message, send `RATE_LIMITED` error (itself throttled so we
  don't spam errors back), log at `warn`. **Do not disconnect** — a misbehaving phone
  gets throttled, not kicked.
- Bucket lives on the `Connection` object; refilled lazily on each message based on
  elapsed time (no background timer needed).

The `spam_actions` fake-phone scenario (§Phase 2) drives 200 Hz to prove this fires.

---

## 10. Heartbeats / Disconnect Detection

**Two unrelated kinds of "heartbeat" — do not conflate them:**

| Kind | Layer | Purpose |
|---|---|---|
| **Game-state heartbeats** (`running_state`, `lane_position`, `plank_state`) | Application | Reconstruct authoritative input state (§7). **NOT** a liveness mechanism. |
| **WS ping/pong frames** | Transport | Detect dead/half-open connections. |

Game-state heartbeats are **not** a substitute for connection-level liveness: a phone
could stop sending `running_state` simply because the player stood still, while the
socket is perfectly healthy. We need a separate transport signal.

### Transport liveness (via `ws` built-in ping/pong frames)

- Server sends a **WS ping frame every `WS_PING_INTERVAL_MS` (10 000 ms)** to each
  connection.
- Each connection tracks `isAlive`. The `pong` frame handler sets `isAlive = true`.
- A watcher checks: if a connection hasn't ponged within `WS_PING_TIMEOUT_MS`
  (25 000 ms), it's considered dead → `terminate()` the socket → triggers the normal
  disconnect lifecycle (§4).
- This is independent of the application-level `ping`/`pong` *messages* (§5), which are
  for client-initiated RTT/debug.

> Note: the 25 s transport timeout is for *detecting a dead socket*. The gameplay-facing
> "runner stops" behavior is much faster (300 ms) and comes from the **state-timeout**
> in §7, not from transport liveness. They operate on different timescales by design.

---

## 11. Server Timestamps

- The server stamps **`serverT`** (monotonic-derived ms) on every message it relays or
  broadcasts: `input:state`, `input:event`, `pong`.
- The client `t` is **preserved for debug/telemetry only** and never used for game
  logic or ordering. (We may log `serverT - t` as a rough one-way latency hint, with
  the caveat that client/server clocks aren't synchronized.)
- `serverT` gives the display a single consistent time base for interpolation/animation
  without trusting phone clocks.

---

## 12. Game-Logic Seams

The game-logic teammate plugs in through one well-defined class. **Today every method
is a no-op.** The session wires them in so the integration path is proven before any
game code exists.

```js
// server/game/game.js  — STUB. Owned by game-logic teammate later.
export class Game {
  /**
   * @param {object} ctx  { sessionCode, emit(type, data) }
   *   emit() is how the game will later send game:state / game:event / game:over
   *   to the display. Provided now; unused by the stub.
   */
  constructor(ctx) { this.ctx = ctx; }

  /**
   * Called by the session whenever a fresh InputState is computed (each tick).
   * Future: feed input into game simulation (move runner, set jetpack, etc.).
   * @param {InputState} state  { running, intensity, lane, plank, serverT }
   */
  onInput(state) { /* TODO(game-team): no-op in transport phase */ }

  /**
   * Called once per server tick (30 Hz) after onInput.
   * Future: advance simulation, detect collisions, update score,
   *   and ctx.emit("game:state"/"game:event"/"game:over", ...).
   */
  tick() { /* TODO(game-team): no-op in transport phase */ }

  /** Future: return current game world snapshot for /debug or late-join. */
  getState() { return null; /* TODO(game-team) */ }

  /** Cleanup hook on session teardown. */
  dispose() { /* TODO(game-team) */ }
}
```

### Where it's called (per tick, per session)

```
tick():
  state = inputState.compute(now)      // §7
  broadcastToDisplay("input:state", state)   // transport phase: this is the payload
  game.onInput(state)                  // stub no-op
  game.tick()                          // stub no-op; will later emit game:* events
```

**Future events the Game will emit** (reserved, not implemented — see §5):
`game:state`, `game:event`, `game:over`. When the game team implements them, they call
`ctx.emit(...)`; the transport layer already knows how to route emitted messages to the
display. No transport changes should be needed.

---

## 13. Static File Serving

The display page is served by the **same** Node server:

- `express.static` mounts `server/public/`.
- `GET /` → `server/public/index.html`.
- The WebSocket endpoint `/ws` and HTTP share **one** `http.Server` instance (express
  app handles HTTP routes; `ws` `WebSocketServer` attached with `noServer` + an
  `upgrade` handler that only accepts `/ws`).
- **This matters:** the real game renderer (teammate's work) will live in
  `server/public/`. Our placeholder `index.html` will be replaced by their canvas
  renderer, which connects to `/ws` exactly as our placeholder does and consumes the
  same `input:state` / `input:event` contract. The transport layer doesn't change when
  they drop in.
- `GET /debug` → JSON snapshot of all active sessions + their live `InputState` (and
  `Game.getState()` once it exists) for at-a-glance debugging.

---

## 14. Open Questions

1. **Intensity decay vs hard reset (§7).** Default proposal: hard reset to 0 on
   `RUNNING_TIMEOUT_MS`. Do you want a server-side linear decay ramp instead, or leave
   smoothing entirely to the display? *(Recommend: hard reset; display smooths.)*
2. **`input:state` send policy.** Always broadcast every tick (simple, constant
   bandwidth, display always has a frame) vs only-on-change (less traffic). *(Recommend:
   always, per your spec — 30 Hz × a tiny payload is negligible and simplifies the
   display.)*
3. **Phone reconnect semantics.** Current proposal: phone re-`join`s with the same code,
   no session-resume protocol; the runner just resumes when heartbeats return. Is that
   sufficient, or do you want an explicit "resume" ack carrying any state? *(Recommend:
   the simple rejoin; matches the self-healing design in §3.)*
4. **Display reconnect / refresh.** Currently a display disconnect destroys the session
   (and its code). If the renderer teammate refreshes the tab mid-demo, the code
   changes. Acceptable, or should a display be allowed to reclaim its prior code within
   a short grace window? *(Recommend: accept teardown for phase 1; revisit if it bites
   at the booth.)*
5. **Lane on prolonged staleness (§7).** We *hold* the last lane and only warn. Confirm
   you don't want it to recenter to lane 0 after some timeout. *(Recommend: hold; a
   lane is a position, not a momentary action.)*
6. **`phone:join` ack (§5).** I'm adding `session:joined` to the contract (spec only
   listed server→display). Confirm the name/shape.
7. **Multiple displays on one code?** Spec says single display per session; confirm a
   second `display`-role connection that somehow targets an existing session is simply
   rejected (we treat display creation as always-new-session, so this shouldn't arise).
8. **Code format.** 4 numeric digits (`0000`–`9999`). OK to allow leading zeros and
   send as a string? *(Recommend: yes, always a 4-char string.)*

---

## Appendix: proposed file structure

Matches your suggested layout. One deviation noted:

```
server/
├── index.js                 # http + ws + express + static bootstrap, /debug route
├── config.js
├── sessions/
│   ├── sessionManager.js    # create/join/cleanup, code generation + collision check
│   └── session.js           # holds phone+display conns + InputState + Game stub + tick loop
├── game/
│   └── game.js              # STUB: onInput(), tick(), getState(), dispose()
├── input/
│   └── inputState.js        # reconstructs running/lane/plank from heartbeats + timeouts
├── net/
│   ├── messages.js          # zod schemas (Envelope, ClientData, ServerData) + MessageType enum
│   ├── envelope.js          # parse + envelope-validate + build outbound envelopes (stamps serverT)
│   ├── handlers.js          # one handler per inbound type
│   ├── connection.js        # wraps ws socket: send/recv, role, sessionCode, last-seen, token bucket
│   ├── rateLimiter.js       # token bucket
│   └── heartbeat.js         # ws ping/pong frame loop + timeout watcher
├── public/
│   └── index.html           # placeholder display page
├── scripts/
│   ├── fakePhone.js         # CLI: scenarios pair_only/steady_run/obstacle_course/...
│   └── fakeDisplay.js       # CLI: create session, print code, print input:state/input:event
└── utils/
    ├── logger.js            # pino
    └── timing.js            # monotonic ms (performance.now base), deadline tick scheduler
```

**Deviation / suggestion:** I'd consider housing the per-session **tick loop driver**
either in `session.js` (each session owns its loop — simplest for single-player and
keeps lifecycle co-located) or factoring a tiny `tickScheduler` into `utils/timing.js`
that `session.js` consumes. I lean toward **the scheduler primitive in `timing.js`,
driven by `session.js`** so the deadline math is unit-testable in isolation. Flagging
rather than deciding unilaterally.

---

*End of plan. Awaiting review before scaffolding Phase 2.*
