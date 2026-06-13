# PLAN.md — Integrating the Unity emulator (`master`) with the WebSocket server (`websockets`)

**Goal:** make the Subway-Surfers Unity emulator from `master` run as the *display* for the
Node WebSocket server from `websockets`, driven by the server's `input:state` / `input:event`
stream. End state: `node server/index.js`, pair `fakePhone.js` in `steady_run`, open the
display in a browser, and watch the emulator respond to simulated input.

**Status:** proposal. No code until approved.

---

## 0. What each branch actually contains (verified by reading the code)

### `master` — the emulator / renderer
- A **Unity 2019.4 WebGL build** of *Subway Surfers: San Francisco* (`SanFrancisco.*.unityweb`,
  `unity.js`, `UnityLoader.2019.2.js`, `loader.js`, `poki-sdk*.js`, `4399.*.js`).
- [index.html](index.html) bootstraps Unity via `window.config` → `loader.js` → `unity.js`, **and**
  already contains a hand-written **WebSocket input bridge** (the seam we will rework):
  - connects to `ws://localhost:8765` (a comment literally says `// TODO: client to be changed`),
  - parses `{ action: "press"|"release", key: "left"|... }`,
  - calls `dispatchKey(name, "keydown"|"keyup")`, which builds a synthetic **`KeyboardEvent`** with
    a `keyCode`/`which` from `KEY_MAP` and dispatches it on the Unity `<canvas>` (`bubbles:true`).
- A **legacy stack** that we are replacing, not reusing: [controller.html](controller.html) (phone UI),
  [input_server.py](input_server.py) (a Python `websockets` relay on 8765), [test_controller.py](test_controller.py).

**The emulator's only input API is synthetic `KeyboardEvent`s on the canvas.** There is no JS
game-logic object to call — the game is a compiled WASM blob. `KEY_MAP`: `left:37, up:38,
right:39, down:40, space:32` (+ WASD). Subway-Surfers controls: **←/→ = change lane (relative,
one tap = one lane), ↑ = jump, ↓ = roll/duck**, and the avatar **runs forward automatically —
there is no "run" key and no "jetpack" key** (jetpack is an in-game pickup).

### `websockets` — the Node transport server
- Node 20+, ESM, `express` + `ws` + `zod` + `pino`. HTTP+WS on **`:8080`**, WS path **`/ws`**.
  `express.static(server/public)` serves the display page at `/`; `/debug` dumps live state.
- Wire format: JSON envelope `{ type, data, t }`. Bootstrap + contract (from
  [server/net/messages.js](server/net/messages.js), [server/sessions/session.js](server/sessions/session.js)):
  - **Display:** connect → send `display:create_session` → receive `session:created {sessionCode}` →
    then receive, every 30 Hz tick:
    - `input:state` = `{ running:bool, intensity:0..1, lane:-1|0|1, plank:bool, serverT }`
    - `input:event` = `{ type:"jump"|"duck", serverT }`
    - plus `phone:status {connected}`.
  - **Phone** (the fake clients / real controller) joins with a 4-digit `sessionCode` and sends
    `phone:running_state` / `phone:lane_position` / `phone:plank_state` / `phone:jump` / `phone:duck`.
- The server **reconstructs** authoritative input from heartbeats with timeouts
  ([server/input/inputState.js](server/input/inputState.js)): no `running_state` for 300 ms → intensity
  snaps to 0; lane is *held*; plank forced off after 400 ms.
- [server/public/index.html](server/public/index.html) is a **placeholder display**: it already does the
  whole WS dance (create_session, show code, render `input:state` as text cells, log events,
  auto-reconnect). **This is the file we replace.**
- The **`Game` stub** ([server/game/game.js](server/game/game.js)) — `onInput(state)`, `tick()`,
  `getState()`, `onEvent(type)`, `dispose()` — is constructed per session and called every tick
  (`session.#onTick()` does `broadcastToDisplay(input:state)` → `game.onInput(state)` → `game.tick()`).
  It is a no-op today. Its `ctx.emit(type, data)` routes a message to the display, and the README
  reserves `game:state|event|over` for exactly this — **emitting `game:*` needs no transport change.**
- `server/scripts/fakePhone.js` + `fakeDisplay.js` are the test clients. **Note the path: they live
  under `server/scripts/`, not `scripts/`** (the task text says `scripts/…`; same files).

---

## 1. Changes needed on each side

### Display side — `server/public/index.html` (the real work happens here)
Replace the placeholder with an **integrated page** that is *both* the Unity emulator *and* a thin
input bridge + HUD. It will:
1. **Host the Unity build** — copy in the bootstrapping from `master/index.html` (the `window.config`
   block + `loader.js`/`unity.js` machinery) so the canvas renders the game.
2. **Connect to the server, not 8765** — derive `ws://<location.host>/ws` (reusing the placeholder's
   logic), send `display:create_session` on open, and **display the pairing code** prominently so a
   tester can pair `fakePhone --code <that code>`.
3. **Translate `input:state` → emulator keys** each tick (see §2), dispatching `KeyboardEvent`s on the
   canvas via the proven `dispatchKey` mechanism carried over from `master`.
4. **Translate `input:event` → emulator keys** (jump → ↑, duck → ↓).
5. **Keep a compact always-on HUD overlay** (running / intensity bar / lane / plank / phone status),
   carried over from the placeholder, so state with no keyboard analog is still *visibly* reflected
   (this is what makes `steady_run` visible — see §5).
6. Keep the placeholder's **auto-reconnect**.

The `master` bridge's transport (`ws://…:8765`, `{action,key}`, press/release) is **discarded**; its
**`dispatchKey` keyCode-dispatch is reused** — that's the emulator's actual input API.

### Server side — `server/game/game.js` (the stub, made real)
The task asks `Game.onInput()` / `tick()` to "delegate to the emulator's real logic." Honest
constraint: **the emulator is browser-side WASM; the Node `Game` cannot host it.** So "the emulator's
real logic" that we *can* factor out is the **input-interpretation layer** — the lane-tracking +
key-command translation. Plan:
- Extract that translation into one **shared, environment-agnostic module**
  (`server/game/emulatorInput.js`, plain ESM, no DOM) that, given successive `input:state`/event
  inputs, yields abstract key-commands (`["right"]`, `["jump"]`, …) and tracks the applied lane.
- `Game.onInput(state)` / `Game.onEvent(type)` **delegate to that module** to compute commands;
  `tick()` flushes them. The display imports the *same* module so both sides share one source of
  truth. This turns the stub into real, exercised logic instead of a no-op, and is surfaced in
  `getState()` (→ `/debug`).
- **Decision (default): the server does NOT emit a new message to drive the display** — the display
  computes its own keys from `input:state` it already receives, so we honor "do not modify the
  protocol." Optionally (only if you want server-authoritative driving) `Game` may `ctx.emit("game:event",
  {keys})` over the *already-reserved* `game:*` channel — flagged as an opt-in in §7, not done by default.

### Build / packaging
- Add nothing to `package.json` dependencies (constraint). The display loads Unity from static files;
  no new server deps.

---

## 2. How `input:state` / `input:event` map to the emulator's input API

| Server signal | Emulator action | Why / mechanism |
|---|---|---|
| `input:event {type:"jump"}` | `keydown`+`keyup` **↑ (38)** | one-shot → one key tap. |
| `input:event {type:"duck"}` | `keydown`+`keyup` **↓ (40)** | one-shot → one key tap (roll). |
| `input:state.lane` (−1/0/1, **absolute**) | N relative **← (37) / → (39)** taps | game lanes are **relative**; track `appliedLane` (start 0=center). On change, `delta = lane − appliedLane`; tap → `|delta|` times if `delta>0`, ← otherwise; set `appliedLane = lane`. `delta` ∈ {1,2}. |
| `input:state.running` / `.intensity` | **no key** — HUD only | Subway-Surfers auto-runs; no run key exists. Reflected in the HUD bar (§5). |
| `input:state.plank` (jetpack) | **no key** — HUD only | jetpack is an in-game pickup, not a control. HUD pill. |
| `phone:status {connected:false}` | (server resets lane→0, plank→off) → avatar returns to center | `InputState.reset()` on phone detach broadcasts `lane:0`; our diff naturally re-centers. |

**Key shape mismatch to fix:** `master`'s `dispatchKey` builds `code = "Key"+fromCharCode(keyCode)`,
which is garbage for arrows (e.g. `"Key%"`). Unity 2019 reads `keyCode`/`which` so it worked anyway;
we'll set correct `code`/`key` (`"ArrowLeft"`, etc.) for robustness while keeping `keyCode`/`which`.

**Lane-sync caveat:** we can't observe Unity's internal lane, so we trust "starts at center and our
model stays in sync." A death/respawn could desync the model; acceptable for this integration and
noted as a limitation.

---

## 3. Naming / shape mismatches between the branches

| Concern | `master` | `websockets` | Resolution |
|---|---|---|---|
| WS endpoint | `ws://localhost:8765` (hardcoded) | `ws://<host>:8080/ws` | derive from `location.host` + `/ws`. |
| Wire format | `{action,key}` press/release | `{type,data,t}` envelope | display speaks the envelope; old format dropped. |
| Input model | discrete key press/release | continuous `input:state`@30 Hz + one-shot `input:event` | continuous→relative key diffs; one-shot→key taps (§2). |
| Bootstrap | none (just connect) | `display:create_session` → `session:created` | display performs the handshake + shows code. |
| Lane semantics | n/a (keys only) | **absolute** lane | converted to **relative** taps (§2). |
| `running`/`plank` | n/a | first-class fields | no key analog → HUD only (§5). |
| Static hosting | none (file opened directly) | `express.static(server/public)` | put display + Unity assets under `server/public/` (§4). |
| Script path | task says `scripts/…` | actually `server/scripts/…` | unchanged; just the real path. |
| "emulator real logic" | browser WASM (no JS API) | server-side `Game` stub | shared interpretation module; server can't host WASM (§1). |

---

## 4. Merge / integration strategy

**One working tree must hold both** the Unity assets (`master`) and the Node server (`websockets`),
because `node server/index.js` has to *serve* the emulator.

**Recommended: merge, don't copy.** Preserve both histories.
1. `git checkout master && git checkout -b integrate-display` (do the work on a branch; never commit
   straight to `master`/`main`).
2. `git merge websockets`. Expected conflicts (small, both are root files):
   - **`.gitignore`** — union the two (keep `node_modules/` + the websockets extras).
   - **`PLAN.md`** — `master` will carry *this* integration plan; `websockets` carries the transport
     design doc. Keep both: this file stays `PLAN.md`; rename the transport doc to
     `docs/PLAN.transport.md` during conflict resolution. No content lost.
   - `README.md`, `package*.json`, `server/` exist only on `websockets` → merge cleanly (added).
3. **Place the emulator where express already serves it (zero server-code change):**
   `git mv` the Unity runtime files (`SanFrancisco.*`, `unity.js`, `UnityLoader.2019.2.js`,
   `loader.js`, `poki-sdk*.js`, `4399.*.js`, `SanFrancisco.json`) into `server/public/`, and write the
   new integrated `server/public/index.html` that loads them with the same relative paths.
   `express.static` then serves the whole emulator at `/` untouched.

Why moving (not duplicating): the assets are ~50 MB; one copy only. Why `server/public/` (not a new
`express.static(repoRoot)` mount): that would mean editing `server/index.js`, which the constraints
fence off as server internals — placing files in the existing public dir needs **no server edit**.

The legacy root `index.html` becomes stale once its assets move; the Python stack
(`input_server.py`, `controller.html`, `test_controller.py`) is superseded. See §5 for what happens
to them.

---

## 5. Making `steady_run` visibly respond (the Definition-of-Done detail)

`steady_run` sends **only** `running_state` (intensity 0.8) for 30 s — **no** lane/jump/duck. Since
the Unity avatar auto-runs and there is no run key, the canvas alone won't change. The **HUD overlay**
(carried from the placeholder) is therefore the guaranteed-visible response: it shows `RUN: yes`,
`intensity 0.80`, `phone ✓` updating live at 30 Hz. The Unity *avatar* itself visibly responds to
**lane / jump / duck**, best demonstrated with the `obstacle_course` scenario. I'll verify both, but
the HUD satisfies the literal `steady_run` DoD.

> If you'd rather the **canvas** also react to `steady_run` (e.g. auto-press Space once on
> `running` false→true to dismiss the Unity start screen so the avatar is actually running), that's a
> small, build-specific add — flagged as an open decision in §7, off by default.

---

## 6. What I will NOT touch

- **WS message protocol / server internals** — no edits to `net/`, `sessions/`, `input/`, `config.js`,
  `index.js`, or any message schema. (The only server file I touch is the `Game` *stub*, which is
  explicitly the integration seam.)
- **The fake clients** `server/scripts/fakePhone.js` & `fakeDisplay.js` — left byte-for-byte; they must
  keep working. (My changes are display- and stub-only; the server they talk to is unchanged.)
- **`server/public/controller.html`** (the manual phone controller) — untouched; still pairs and sends
  `phone:*` messages.
- **The Unity build artifacts** — moved, never modified (binaries, `unity.js`, loaders, poki SDK,
  `4399.*`). No re-compilation.
- **`package.json` dependencies** — none added (per constraints). If anything unforeseen needs a dep,
  I'll stop and call it out first.
- **Legacy Python stack** (`input_server.py`, `test_controller.py`) and the old root `index.html`/
  `controller.html`: left in place as historical reference, **not deleted** unless you ask. They're
  simply no longer on the active path.

---

## 7. Open decisions (my recommendation in **bold**) — please confirm or override

1. **Game stub scope:** **make `onInput/tick` delegate to the shared interpretation module (real logic,
   surfaced in `/debug`) but emit no new messages by default.** Alt: also `ctx.emit("game:event",{keys})`
   to let the server *drive* the display over the reserved `game:*` channel.
2. **Canvas reaction to `steady_run`:** **HUD-only (no fake start-key).** Alt: auto-press Space on
   `running` rising edge to kick the Unity start screen so the avatar actually runs on screen.
3. **Gate lane/jump/duck on `running`:** **no — forward always (simpler; doesn't break obstacle_course,
   where running is true during all actions).** Alt: ignore inputs while `running` is false.
4. **Legacy files:** **keep them.** Alt: delete the Python stack + old root page for tidiness.

---

## 8. Verification plan (maps to the Definition of Done)

1. `npm install` (Node 20+); `node server/index.js` → server up on `:8080`.
2. Open `http://localhost:8080/` → Unity emulator loads **and** a pairing code shows.
3. `node server/scripts/fakePhone.js --code <code> --scenario steady_run` → **HUD shows RUN/0.80 live**
   (primary DoD check).
4. Re-run with `--scenario obstacle_course` → **avatar changes lanes, jumps, rolls** on the canvas.
5. `npm run fake:display` still prints code + live state; `controller.html` on a phone still drives it
   — proving nothing in the transport/fake-client path broke.

---

## 9. Risks / things to watch
- **Static MIME / compression** of `.unityweb`/`.wasm` under `express.static`: UnityLoader 2019.2
  decompresses client-side, so raw bytes should be fine, but if the loader stalls I'll check
  `Content-Type`/`Content-Encoding`. (No server-code change intended; if a header tweak is truly
  required I'll surface it before touching `index.js`.)
- **Poki SDK** network calls: `master` is described as a working local emulator, so it should run
  offline; verify on first load.
- **Lane-model desync** after a Unity death/respawn (see §2). Out of scope to fully solve.
- **Key-event target:** dispatching on the canvas with `bubbles:true` reaches `document`, where the
  emscripten input handler listens (the mechanism `master` already relied on); verify focus isn't
  required.
```
