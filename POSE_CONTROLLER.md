# Feet-intent lane controller (Python / OpenCV)

Detects when the player intends to step one lane left or right and taps the
corresponding arrow key — triggering from the **earliest reliable lateral foot
movement** rather than waiting for the torso to cross a lane boundary. The hips
confirm the player actually reached the new lane.

```
webcam ─▶ pose_input.py ──(phone:lane_position)──▶ server/index.js (:8080/ws)
                                                   ─▶ display ─▶ Unity game
```

The game is the **Node server in `server/`**. It is session-based: the browser
display (`http://localhost:8080/`) creates a session and shows a 4-digit code, a
phone joins with that code and streams an **absolute lane** (`-1/0/1`), and the
display folds that into relative arrow taps for the Unity emulator (see
[`server/public/emulatorInput.js`](server/public/emulatorInput.js)). This
controller acts as that phone: it maps the engine's early `left`/`right` intent
onto the absolute lane and streams `phone:lane_position`.

## Files

| File | Role |
|---|---|
| [`lane_engine.py`](lane_engine.py) | Pure logic: landmark format, filters (One Euro / EMA / timestamp velocity), calibration, feet-intent fusion, two-stage state machine. No camera/model deps. |
| [`pose_input.py`](pose_input.py) | Runtime: latest-frame threaded camera, MediaPipe backend (swappable), OpenCV debug overlay, latency metrics, and emitters (game phone / legacy taps). |
| [`test_lane_engine.py`](test_lane_engine.py) | Deterministic tests over synthetic landmark streams (no camera needed). |
| [`server/`](server/index.js) | The game server (display + session + Unity emulator). |

## Run (game integration)

```bash
pip install -r requirements.txt
node server/index.js              # terminal 1: game server + display @ :8080
# open http://localhost:8080/ in a browser → note the 4-digit session code
python pose_input.py --code 1234  # terminal 2: calibrate (4 × SPACE), then play
```

Calibration: stand **center → left → center → right**, pressing SPACE in each
position. In-game: `j` = jump, `k` = duck, `c` = recalibrate, `q` = quit.

The emitter auto-reconnects and re-joins, and in game mode `resync_on_cancel` is
on so an aborted step moves the avatar back. Useful flags:

- `--code XXXX` — session code from the display (required for game mode)
- `--game-url ws://host:8080/ws` — point at a non-default server
- `--intensity 0` — stop sending the running-state heartbeat
- `--legacy-ws` — target the standalone `input_server.py` tap server (`:8765`)
- `--no-ws` — print intent commands instead of sending (dev/debug)
- `--save-calib calib.json` / `--load-calib calib.json`
- `--skip-calibration` — neutral default geometry
- `--no-mirror`, `--camera N`, `--model-complexity {0,1,2}`

```bash
python test_lane_engine.py        # 9/9 should pass
```

## How it triggers (the key design point)

1. **Stage 1 — intent (feet).** Per frame, each foot's lateral position/velocity
   (timestamp-based, jitter-robust) is computed in a mirror-aware "lateral" axis.
   The leading foot is the one with the strongest *reliable* horizontal speed —
   **direction comes from velocity, not foot identity**, so crossover steps work.
   A command fires once horizontal motion dominates vertical, displacement passes
   the calibrated commit threshold (or velocity is decisively fast), and a knee /
   hip / large-displacement signal corroborates. Emits in ~80 ms, before the
   torso moves.
2. **Stage 2 — confirmation (hips).** The hip midpoint is tracked to the
   calibrated target-lane center to confirm arrival; an aborted step that returns
   to the origin is cancelled without a second command. The machine must settle
   in a lane before the next step arms (no return-stroke double-fires), and a
   left→right move must pass through center.

**Suppressed** while jumping/airborne (and briefly after landing) or ducking, and
robust to vertical running, forward/back motion, and single-frame pose errors.

All thresholds live in `Config` and are overridden by calibration. See the
docstring at the top of [`lane_engine.py`](lane_engine.py) for the exact expected
landmark input format.
