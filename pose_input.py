"""Runtime for the feet-intent lane controller: camera -> pose -> lanes -> game.

This is the IO half of the system (the decision logic lives in ``lane_engine.py``).
It captures the webcam on a latest-frame-only thread, runs a pose model, feeds the
landmarks through the lane engine, draws a debug overlay, and emits "left"/"right"
to the repo's existing input server.

Integration with the repo
--------------------------
The real game is the Node server in ``server/`` (``node server/index.js``). It is
session-based: a DISPLAY (the browser at ``http://localhost:8080/``) creates a
session and shows a 4-digit code, a PHONE joins with that code, and the phone then
streams an ABSOLUTE lane position (``phone:lane_position {lane: -1|0|1}``). The
display folds that absolute lane into relative arrow-key taps for the Unity
emulator (see ``server/public/emulatorInput.js``). This controller acts as that
phone (GamePhoneEmitter): it joins the session and streams lane state, mapping the
engine's early "left"/"right" intent onto the absolute lane. The data path is:

    webcam -> pose_input.py --(phone:lane_position)--> server/index.js (:8080/ws)
                                                       -> display -> Unity game

A second, legacy transport also exists: the standalone ``input_server.py`` on
``ws://localhost:8765`` re-broadcasts ``{"action","key"}`` taps (what
``test_controller.py`` sends). Use ``--legacy-ws`` to target that instead.

Run (game integration)
----------------------
    node server/index.js                   # terminal 1: game server (display @ :8080)
    # open http://localhost:8080/ in a browser, note the 4-digit session code
    python pose_input.py --code 1234       # terminal 2: this controller (calibrate, play)

Other modes
-----------
    python pose_input.py --no-ws           # print intent commands, don't send
    python pose_input.py --legacy-ws       # tap protocol -> input_server.py (:8765)
    python pose_input.py --code 1234 --load-calib calib.json   # reuse calibration
    python pose_input.py --code 1234 --skip-calibration        # neutral geometry

Requires: opencv-python, numpy, websockets, and a pose backend (mediapipe by
default: ``pip install mediapipe``). Any model that yields the landmark format
documented in lane_engine.py can replace MediaPipePoseBackend.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from collections import deque
from typing import Dict, Optional, Tuple

import cv2
import numpy as np

from lane_engine import (
    Config, Calibration, Calibrator, CalibrationPhase, FeatureExtractor,
    LaneStateMachine, PositionLaneController, Landmark, State,
)


# --------------------------------------------------------------------------- #
# Latest-frame-only camera capture
# --------------------------------------------------------------------------- #
class ThreadedCamera:
    """Background capture thread that keeps only the newest frame.

    Reading always returns the most recent frame (plus its capture timestamp),
    so the processing loop never works through a backlog of stale frames.
    """

    def __init__(self, index: int, width: int, height: int):
        self.cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            self.cap = cv2.VideoCapture(index)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.cap.set(cv2.CAP_PROP_FPS, 60)
        if not self.cap.isOpened():
            raise RuntimeError(f"could not open camera index {index}")

        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._ts: float = 0.0
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while self._running:
            ok, frame = self.cap.read()
            if not ok:
                time.sleep(0.002)
                continue
            ts = time.perf_counter()
            with self._lock:
                self._frame = frame
                self._ts = ts

    def read(self) -> Tuple[Optional[np.ndarray], float]:
        with self._lock:
            if self._frame is None:
                return None, 0.0
            return self._frame.copy(), self._ts

    def release(self) -> None:
        self._running = False
        self._thread.join(timeout=0.5)
        self.cap.release()


# --------------------------------------------------------------------------- #
# Pose backend (MediaPipe by default; lazy import so the module loads without it)
# --------------------------------------------------------------------------- #
_MP_INDEX = {
    "left_shoulder": 11, "right_shoulder": 12,
    "left_hip": 23, "right_hip": 24,
    "left_knee": 25, "right_knee": 26,
    "left_ankle": 27, "right_ankle": 28,
    "left_heel": 29, "right_heel": 30,
    "left_foot_index": 31, "right_foot_index": 32,
}


class MediaPipePoseBackend:
    """Adapts MediaPipe Pose to the landmark format documented in lane_engine.

    Supports both the legacy ``mp.solutions`` API (mediapipe < ~0.10.20) and the
    current Tasks API (mediapipe >= 0.10.0). The Tasks path downloads the model
    file on first run (~25 MB for lite, ~100 MB for heavy) into the working dir.

    Returns ``(landmarks_dict, bbox)`` where landmarks_dict maps each name to a
    ``Landmark(x, y, conf)`` (or None) and bbox is the normalized person-center
    ``(cx, cy)`` used as the last-resort horizontal fallback.
    """

    _TASK_MODELS = {
        0: ("pose_landmarker_lite.task",
            "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
            "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"),
        1: ("pose_landmarker_full.task",
            "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
            "pose_landmarker_full/float16/latest/pose_landmarker_full.task"),
        2: ("pose_landmarker_heavy.task",
            "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
            "pose_landmarker_heavy/float16/latest/pose_landmarker_heavy.task"),
    }

    def __init__(self, model_complexity: int = 1):
        import mediapipe as mp  # lazy: only needed when actually running on a camera
        self._mp = mp
        self._mode: str

        if hasattr(mp, "solutions") and hasattr(mp.solutions, "pose"):
            try:
                self._pose = mp.solutions.pose.Pose(
                    model_complexity=model_complexity,
                    smooth_landmarks=False,
                    min_detection_confidence=0.5,
                    min_tracking_confidence=0.5,
                )
                self._mode = "solutions"
                return
            except Exception:
                pass

        # mediapipe >= ~0.10.20 dropped mp.solutions; use the Tasks API instead.
        self._mode = "tasks"
        self._init_tasks(model_complexity)

    def _init_tasks(self, complexity: int) -> None:
        import os, urllib.request
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision

        fname, url = self._TASK_MODELS[complexity]
        if not os.path.exists(fname):
            print(f"[mediapipe] downloading {fname} (first run only) ...")
            urllib.request.urlretrieve(url, fname)
            print("[mediapipe] model ready")

        options = vision.PoseLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=fname),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=0.5,
            min_pose_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self._landmarker = vision.PoseLandmarker.create_from_options(options)

    # ------------------------------------------------------------------ helpers
    def _pack(self, lms_iter) -> Tuple[Dict, Optional[Tuple[float, float]]]:
        out: Dict[str, Optional[Landmark]] = {}
        xs, ys = [], []
        for name, idx in _MP_INDEX.items():
            lm = lms_iter[idx]
            conf = float(getattr(lm, "visibility", 1.0))
            out[name] = Landmark(float(lm.x), float(lm.y), conf)
            if conf >= 0.5:
                xs.append(float(lm.x))
                ys.append(float(lm.y))
        bbox = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2) if xs else None
        return out, bbox

    # ------------------------------------------------------------------ public
    def infer(self, frame_bgr: np.ndarray):
        if self._mode == "solutions":
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            rgb.flags.writeable = False
            res = self._pose.process(rgb)
            if not res.pose_landmarks:
                return {name: None for name in _MP_INDEX}, None
            return self._pack(res.pose_landmarks.landmark)

        # Tasks API path
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        ts_ms = int(time.perf_counter() * 1000)
        result = self._landmarker.detect_for_video(mp_image, ts_ms)
        if not result.pose_landmarks:
            return {name: None for name in _MP_INDEX}, None
        return self._pack(result.pose_landmarks[0])

    def close(self) -> None:
        if self._mode == "solutions":
            self._pose.close()
        else:
            self._landmarker.close()


# --------------------------------------------------------------------------- #
# Command emission to ws://localhost:8765 (input_server.py protocol)
# --------------------------------------------------------------------------- #
class WebSocketEmitter:
    """Sends key taps ({"action":"press"/"release","key":...}) to the input
    server from a background asyncio loop, with auto-reconnect."""

    def __init__(self, url: str, tap_hold: float = 0.05):
        self.url = url
        self.tap_hold = tap_hold
        self._ws = None
        self._connected = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def connected(self) -> bool:
        return self._connected

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect_forever())

    async def _connect_forever(self) -> None:
        import websockets
        while True:
            try:
                async with websockets.connect(self.url) as ws:
                    self._ws = ws
                    self._connected = True
                    await ws.wait_closed()
            except Exception:
                pass
            self._ws = None
            self._connected = False
            await asyncio.sleep(1.0)

    async def _tap(self, key: str) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps({"action": "press", "key": key}))
            await asyncio.sleep(self.tap_hold)
            await ws.send(json.dumps({"action": "release", "key": key}))
        except Exception:
            pass

    def send(self, key: str) -> None:
        if self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._tap(key), self._loop)


# --------------------------------------------------------------------------- #
# Command emission to the Node game server (server/index.js phone protocol)
# --------------------------------------------------------------------------- #
class GamePhoneEmitter:
    """Drives the Node game server (``server/``) as a phone client.

    The game is session-based (see this module's docstring): we JOIN with the
    display's 4-digit code, then stream ABSOLUTE lane state. The engine emits
    early "left"/"right" intent commands; we fold those into an absolute lane
    (-1/0/1, clamped) and push ``phone:lane_position`` immediately for low latency,
    re-asserting it (plus an optional running heartbeat) on a timer so the server
    holds the value and re-syncs after any reconnect. Auto-reconnects and re-joins.

    Pair this with ``Config.resync_on_cancel = True`` so an aborted step emits the
    opposite intent and the absolute lane stays in sync with the player.
    """

    def __init__(self, url: str, code: str, intensity: float = 1.0,
                 heartbeat_sec: float = 0.15):
        self.url = url
        self.code = code
        self.intensity = intensity
        self.heartbeat = heartbeat_sec
        self._ws = None
        self._connected = False
        self._joined = False
        self._lane = 0
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def connected(self) -> bool:
        return self._connected and self._joined

    @property
    def lane(self) -> int:
        return self._lane

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        import websockets
        print(f"[game] connecting to {self.url} (session {self.code}) ...")
        while True:
            try:
                async with websockets.connect(self.url) as ws:
                    self._ws = ws
                    self._connected = True
                    self._joined = False
                    print(f"[game] socket open -> phone:join {self.code}")
                    await self._raw(ws, "phone:join", {"sessionCode": self.code})
                    # reader ends on close; heartbeat breaks on send failure.
                    await asyncio.gather(self._reader(ws), self._heartbeat(ws))
            except Exception as e:
                print(f"[game] connection failed: {type(e).__name__}: {e} "
                      f"(is `node server/index.js` running on {self.url}?)")
            self._ws = None
            self._connected = False
            self._joined = False
            await asyncio.sleep(1.0)

    async def _reader(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")
            if mtype == "session:joined":
                self._joined = True
                with self._lock:
                    lane = self._lane
                await self._raw(ws, "phone:lane_position", {"lane": lane})
                print(f"[game] joined session {msg.get('data', {}).get('sessionCode')}")
            elif mtype == "error":
                d = msg.get("data", {})
                print(f"[game] error {d.get('code')}: {d.get('message')}")
                if d.get("code") in ("SESSION_NOT_FOUND", "SESSION_FULL"):
                    self._joined = False

    async def _heartbeat(self, ws) -> None:
        while True:
            try:
                if self._joined:
                    with self._lock:
                        lane = self._lane
                    await self._raw(ws, "phone:lane_position", {"lane": lane})
                    if self.intensity > 0:
                        await self._raw(ws, "phone:running_state",
                                        {"intensity": self.intensity})
            except Exception:
                return                     # socket dead -> let _main reconnect
            await asyncio.sleep(self.heartbeat)

    async def _raw(self, ws, mtype: str, data: dict) -> None:
        await ws.send(json.dumps({"type": mtype, "data": data,
                                  "t": int(time.time() * 1000)}))

    async def _send_now(self, mtype: str, data: dict) -> None:
        ws = self._ws
        if ws is not None and self._joined:
            try:
                await self._raw(ws, mtype, data)
            except Exception:
                pass

    # -- called from the main thread --------------------------------------- #
    def send(self, command: str) -> None:
        """Fold an engine intent ("left"/"right") into the absolute lane."""
        with self._lock:
            if command == "left":
                self._lane = max(-1, self._lane - 1)
            elif command == "right":
                self._lane = min(1, self._lane + 1)
            else:
                return
            lane = self._lane
        if self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._send_now("phone:lane_position", {"lane": lane}), self._loop)

    def event(self, kind: str) -> None:
        """One-shot jump/duck -> phone:jump / phone:duck."""
        mtype = {"jump": "phone:jump", "duck": "phone:duck"}.get(kind)
        if mtype and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._send_now(mtype, {}), self._loop)


# --------------------------------------------------------------------------- #
# Debug visualization
# --------------------------------------------------------------------------- #
class Visualizer:
    """Draws the debug overlay. All engine x's are in the lateral axis, so when
    the preview is mirrored we flip the frame and draw at ``lat_x * width`` to
    line everything up over the player."""

    COLORS = {
        "lane": (90, 90, 90), "center": (60, 200, 255), "boundary": (120, 120, 120),
        "left_foot": (80, 200, 80), "right_foot": (80, 160, 255), "lead": (0, 255, 255),
        "hip": (255, 200, 0), "text": (235, 235, 235), "ok": (90, 220, 130),
        "bad": (90, 90, 240), "warn": (90, 200, 240),
    }

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.trails = {"left": deque(maxlen=12), "right": deque(maxlen=12)}

    def draw(self, frame, calib, features, decision, metrics):
        cfg = self.cfg
        if cfg.invert_x:
            frame = cv2.flip(frame, 1)
        h, w = frame.shape[:2]

        def px(lat_x):
            return int(lat_x * w)

        # lane centers + boundaries
        for lane, cx in calib.lane_centers.items():
            color = self.COLORS["center"] if lane == 0 else self.COLORS["lane"]
            cv2.line(frame, (px(cx), 0), (px(cx), h), color, 1)
            cv2.putText(frame, {-1: "L", 0: "C", 1: "R"}[lane], (px(cx) - 6, 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        for b in calib.boundaries:
            cv2.line(frame, (px(b), 0), (px(b), h), self.COLORS["boundary"], 1, cv2.LINE_AA)

        # feet, trails, velocity arrows
        for side in ("left", "right"):
            ff = getattr(features, side)
            if ff.source == "none":
                self.trails[side].clear()
                continue
            x, y = px(ff.x), int(ff.y * h)
            self.trails[side].append((x, y))
            for j in range(1, len(self.trails[side])):
                cv2.line(frame, self.trails[side][j - 1], self.trails[side][j],
                         self.COLORS[f"{side}_foot"], 1, cv2.LINE_AA)
            is_lead = decision.leading_foot == side
            r = 9 if is_lead else 6
            col = self.COLORS["lead"] if is_lead else self.COLORS[f"{side}_foot"]
            cv2.circle(frame, (x, y), r, col, -1 if is_lead else 2)
            # velocity arrow (scaled), + = right
            ax = int(x + ff.vx * 0.18 * w)
            cv2.arrowedLine(frame, (x, y), (ax, y), col, 2, tipLength=0.3)
            cv2.putText(frame, f"{ff.source.split('_')[-1]} {ff.conf:.2f}",
                        (x - 20, y + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.4, col, 1)

        # hip midpoint (x is tracked; drawn at mid-height as a reference marker)
        cv2.drawMarker(frame, (px(features.hip_x), h // 2), self.COLORS["hip"],
                       cv2.MARKER_DIAMOND, 16, 2)

        self._panel(frame, calib, features, decision, metrics)
        return frame

    def _panel(self, frame, calib, features, decision, metrics):
        # tick = signal already clears its threshold; helps see why intent is "weak"
        vmark = "OK" if decision.lead_vx >= decision.vel_thr else "--"
        dmark = "OK" if decision.lead_disp >= decision.disp_thr else "--"
        lines = [
            f"state: {decision.state.name}   lane: {decision.current_lane:+d}",
            f"last cmd: {metrics['last_command'] or '-'}   intent: {decision.intent_confidence:.2f}",
            f"lead foot: {decision.leading_foot or '-'}   reason: {decision.reason}",
            f"vx: {decision.lead_vx:.2f}/{decision.vel_thr:.2f} {vmark}   "
            f"disp: {decision.lead_disp:+.3f}/{decision.disp_thr:.3f} {dmark}",
            f"suppressed: {decision.suppressed}",
            f"FPS: {metrics['fps']:.1f}   cap->cmd: {metrics['last_latency_ms']:.0f} ms",
            f"infer: {metrics['infer_ms']:.1f} ms   classify: {metrics['classify_ms']:.2f} ms",
            f"hip_conf: {features.hip_conf:.2f}  ws: {'on' if metrics['ws'] else 'off'}",
        ]
        y = frame.shape[0] - 8 - 18 * (len(lines) - 1)
        cv2.rectangle(frame, (4, y - 18), (360, frame.shape[0] - 2), (20, 20, 20), -1)
        for i, ln in enumerate(lines):
            cv2.putText(frame, ln, (10, y + 18 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        self.COLORS["text"], 1, cv2.LINE_AA)

    def banner(self, frame, text, sub=""):
        if self.cfg.invert_x:
            frame = cv2.flip(frame, 1)
        h, w = frame.shape[:2]
        cv2.rectangle(frame, (0, h // 2 - 46), (w, h // 2 + 30), (20, 20, 20), -1)
        cv2.putText(frame, text, (24, h // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                    (60, 200, 255), 2, cv2.LINE_AA)
        if sub:
            cv2.putText(frame, sub, (24, h // 2 + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        (235, 235, 235), 1, cv2.LINE_AA)
        return frame


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
_PHASE_PROMPT = {
    CalibrationPhase.CENTER: "Stand in the CENTER lane, then press SPACE",
    CalibrationPhase.LEFT: "Step into the LEFT lane, then press SPACE",
    CalibrationPhase.CENTER_RETURN: "Return to CENTER, then press SPACE",
    CalibrationPhase.RIGHT: "Step into the RIGHT lane, then press SPACE",
}


class App:
    def __init__(self, args):
        self.args = args
        self.cfg = Config(invert_x=not args.no_mirror)
        self.fx = FeatureExtractor(self.cfg)
        self.viz = Visualizer(self.cfg)
        self.cam = ThreadedCamera(args.camera, self.cfg.frame_width, self.cfg.frame_height)
        self.backend = MediaPipePoseBackend(model_complexity=args.model_complexity)

        # Emitter selection: game phone (default) -> legacy taps -> print-only.
        if args.no_ws:
            self.emitter = None
        elif args.legacy_ws:
            self.emitter = WebSocketEmitter(args.url)
        else:
            # Game mode streams absolute lane state; an aborted step must emit the
            # opposite intent so the avatar's lane stays synced with the player.
            self.cfg.resync_on_cancel = True
            self.emitter = GamePhoneEmitter(args.game_url, args.code, args.intensity)

        self.calibrator: Optional[Calibrator] = None
        self.calib: Optional[Calibration] = None
        self.sm: Optional[LaneStateMachine] = None

        self._fps = 0.0
        self._last_frame_ts = 0.0
        self._metrics = {
            "fps": 0.0, "last_latency_ms": 0.0, "infer_ms": 0.0, "classify_ms": 0.0,
            "last_command": None, "ws": False,
        }

        if args.load_calib:
            self.calib = Calibration.load(args.load_calib)
            self._start_play()
        elif args.skip_calibration:
            self.calib = Calibration.default(self.cfg)
            self._start_play()
        else:
            self.calibrator = Calibrator(self.cfg)

    def _start_play(self) -> None:
        if self.args.mode == "feet":
            self.sm = LaneStateMachine(self.cfg, self.calib, emit=self._emit)
        else:
            self.sm = PositionLaneController(self.cfg, self.calib, emit=self._emit)
        self.calibrator = None

    def _emit(self, command: str) -> None:
        self._metrics["last_command"] = command
        if self.emitter is not None:
            self.emitter.send(command)
        else:
            print(f"[lane] {command}  (t={time.perf_counter():.3f})")

    def run(self) -> None:
        win = "pose_input - feet-intent lanes"
        cv2.namedWindow(win, cv2.WINDOW_NORMAL)
        try:
            while True:
                frame, cap_ts = self.cam.read()
                if frame is None:
                    if cv2.waitKey(5) & 0xFF == ord("q"):
                        break
                    continue

                now = time.perf_counter()
                if self._last_frame_ts:
                    inst = 1.0 / max(now - self._last_frame_ts, 1e-3)
                    self._fps = 0.9 * self._fps + 0.1 * inst if self._fps else inst
                self._last_frame_ts = now

                t0 = time.perf_counter()
                landmarks, bbox = self.backend.infer(frame)
                infer_ms = (time.perf_counter() - t0) * 1000.0

                features = self.fx.update(landmarks, cap_ts, bbox)

                if self.calibrator is not None:
                    out = self._run_calibration(frame, features)
                else:
                    out = self._run_play(frame, features, cap_ts, infer_ms)

                cv2.imshow(win, out)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if key == ord("c"):                       # recalibrate
                    self.calibrator = Calibrator(self.cfg)
                    self.sm = None
                if key == ord(" ") and self.calibrator is not None:
                    if self.calibrator.next_phase():
                        self.calib = self.calibrator.compute()
                        if self.args.save_calib:
                            self.calib.save(self.args.save_calib)
                        self._start_play()
                # manual jump/duck (only the game emitter forwards these)
                if key in (ord("j"), ord("k")) and hasattr(self.emitter, "event"):
                    self.emitter.event("jump" if key == ord("j") else "duck")
        finally:
            self.cam.release()
            self.backend.close()
            cv2.destroyAllWindows()

    def _run_calibration(self, frame, features):
        self.calibrator.add(features)
        phase = self.calibrator.phase
        n = len(self.calibrator._samples.get(phase, []))
        return self.viz.banner(frame, _PHASE_PROMPT.get(phase, "calibrating"),
                               f"samples: {n}   (C to restart, Q to quit)")

    def _run_play(self, frame, features, cap_ts, infer_ms):
        t0 = time.perf_counter()
        decision = self.sm.update(features, cap_ts)
        classify_ms = (time.perf_counter() - t0) * 1000.0

        self._metrics["fps"] = self._fps
        self._metrics["infer_ms"] = infer_ms
        self._metrics["classify_ms"] = classify_ms
        self._metrics["ws"] = bool(self.emitter and self.emitter.connected)
        if decision.command is not None:
            self._metrics["last_latency_ms"] = (time.perf_counter() - cap_ts) * 1000.0

        return self.viz.draw(frame, self.calib, features, decision, self._metrics)


def parse_args():
    p = argparse.ArgumentParser(description="Feet-intent lane controller")
    p.add_argument("--camera", type=int, default=0, help="camera index")
    # Game mode (default): act as a phone for the Node server in server/.
    p.add_argument("--code", help="4-digit session code shown on the game display")
    p.add_argument("--game-url", default="ws://localhost:8080/ws",
                   help="Node game server phone endpoint")
    p.add_argument("--intensity", type=float, default=1.0,
                   help="running-intensity heartbeat sent to the game (0 disables)")
    # Alternate transports.
    p.add_argument("--legacy-ws", action="store_true",
                   help="use the standalone input_server.py tap protocol instead")
    p.add_argument("--url", default="ws://localhost:8765",
                   help="input_server.py ws url (legacy tap mode)")
    p.add_argument("--no-ws", action="store_true", help="print commands instead of sending")
    # Common.
    p.add_argument("--no-mirror", action="store_true", help="disable mirrored preview/axis")
    p.add_argument("--model-complexity", type=int, default=1, choices=[0, 1, 2])
    p.add_argument("--load-calib", help="load calibration json and skip calibration")
    p.add_argument("--save-calib", help="save calibration json after calibrating")
    p.add_argument("--skip-calibration", action="store_true",
                   help="use neutral default geometry (no calibration pass)")
    args = p.parse_args()

    # Game mode is the default and needs a session code.
    if not args.no_ws and not args.legacy_ws and not args.code:
        p.error("game mode needs a session code: pass --code XXXX (the 4-digit "
                "code shown on the display at http://localhost:8080/), or use "
                "--legacy-ws / --no-ws for the other transports")
    return args


if __name__ == "__main__":
    App(parse_args()).run()
