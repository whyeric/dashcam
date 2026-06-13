"""Feet-intent lane engine for the motion-controlled Subway Surfers clone.

This module is the *pure logic* half of the controller: filtering, calibration,
lower-body signal fusion, and the two-stage lane state machine. It has no camera,
pose-model, or websocket dependencies (only the stdlib + numpy is optional), so it
can be unit-tested deterministically with synthetic landmark streams. The runtime
glue (camera thread, pose model, OpenCV preview, websocket emission) lives in
``pose_input.py``.

================================================================================
EXPECTED LANDMARK INPUT FORMAT
================================================================================
Each processed frame is a ``dict`` mapping a landmark *name* to either a
``Landmark(x, y, conf)`` or ``None`` when the model did not produce it:

    {
        "left_ankle":   Landmark(x=0.41, y=0.92, conf=0.97),
        "right_ankle":  Landmark(x=0.55, y=0.91, conf=0.96),
        "left_heel":    Landmark(...) | None,    # optional
        "right_heel":   Landmark(...) | None,    # optional
        "left_foot_index":  Landmark(...) | None,  # optional
        "right_foot_index": Landmark(...) | None,  # optional
        "left_knee":    Landmark(...),
        "right_knee":   Landmark(...),
        "left_hip":     Landmark(...),
        "right_hip":    Landmark(...),
        "left_shoulder":  Landmark(...),
        "right_shoulder": Landmark(...),
    }

  * ``x`` and ``y`` are normalized to [0, 1] with the origin at the TOP-LEFT of
    the image, x increasing to image-right, y increasing DOWNWARD.
  * ``conf`` is a 0..1 visibility/presence score.
  * Missing landmarks must be ``None`` (not absent keys) so fallbacks engage.

Any pose model (MediaPipe Pose, MoveNet, BlazePose, a custom net, ...) can drive
this engine as long as it emits the names in ``LANDMARK_NAMES`` in this format.
``pose_input.py`` ships a MediaPipe adapter that produces exactly this shape.

A frame may also carry an optional person bounding-box center ``bbox=(cx, cy)``
(normalized) used as the last-resort horizontal fallback when even the hips and
shoulders are unreliable.
================================================================================
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Callable, Deque, Dict, Optional, Tuple


# --------------------------------------------------------------------------- #
# Landmark format
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Landmark:
    x: float          # normalized [0,1], origin top-left, +x = image right
    y: float          # normalized [0,1], +y = image down
    conf: float       # 0..1 visibility/presence


LANDMARK_NAMES = (
    "left_ankle", "right_ankle",
    "left_heel", "right_heel",
    "left_foot_index", "right_foot_index",
    "left_knee", "right_knee",
    "left_hip", "right_hip",
    "left_shoulder", "right_shoulder",
)

# Per-side fallback chain for "where is this foot, horizontally": prefer the
# ankle, then the toe/heel, then the knee. Confidence-gated at use time.
_FOOT_CHAIN = {
    "left": ("left_ankle", "left_foot_index", "left_heel", "left_knee"),
    "right": ("right_ankle", "right_foot_index", "right_heel", "right_knee"),
}


# --------------------------------------------------------------------------- #
# Configuration (all thresholds normalized to screen width, or width/second)
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    # Orientation: with a mirrored selfie preview the player's physical right is
    # on the LEFT of the raw image. We work in a "lateral" axis where + always
    # means the player's intended right / positive lane; invert_x remaps raw x.
    invert_x: bool = True

    # Feet filtering: very responsive (One Euro), jitter-suppressing only at rest.
    # Velocity is taken from RAW positions (the regression below smooths it), so
    # these only smooth the *reported* position used for displacement/preview.
    use_oneeuro_for_feet: bool = True
    foot_oneeuro_min_cutoff: float = 3.0
    foot_oneeuro_beta: float = 3.0
    foot_ema_alpha: float = 0.75          # used when One Euro disabled (0.65..0.8)
    knee_ema_alpha: float = 0.7
    hip_ema_alpha: float = 0.5            # 0.4..0.55: hips only confirm
    body_ema_alpha: float = 0.45

    # Velocity via rolling linear regression over a short time window. The fit
    # itself rejects single-frame jitter, so we feed it raw positions for the
    # lowest possible lag.
    velocity_window_sec: float = 0.05     # ~3 frames @60, ~2 @30
    velocity_min_samples: int = 3

    # Confidence gate.
    landmark_conf_min: float = 0.5

    # Intent thresholds (calibration overrides vel/disp).
    vel_threshold: float = 0.30           # lead-foot |horizontal velocity| (width/s)
    fast_vel_factor: float = 1.6          # (legacy) decisive-velocity multiple
    disp_threshold: float = 0.035         # lead-foot lateral displacement from rest
    strong_disp_threshold: float = 0.060  # displacement big enough to self-support
    knee_vel_threshold: float = 0.22
    hip_vel_threshold: float = 0.18
    horiz_dominance: float = 1.0          # |vx| > this*|vy| (reject vertical motion)
    confirm_time: float = 0.025           # intent must persist this long (debounce)
    rearm_time: float = 0.150             # min gap between emitted commands

    # Confirmation / occupancy.
    confirm_radius: float = 0.050         # hip within this of a lane center => there
    settle_vel: float = 0.20              # foot speed below this => "settled"
    abort_grace: float = 0.060            # min time in MOVING before a cancel is allowed
    move_timeout: float = 1.20            # un-confirmed move resyncs to nearest lane
    resync_on_cancel: bool = False        # emit opposite cmd on aborted move (keeps
                                          # avatar synced); off = spec-literal

    # Jump / duck suppression.
    jump_vy_threshold: float = 0.60       # hip upward speed (width/s) => airborne
    ankle_jump_vy_threshold: float = 0.80
    land_refractory: float = 0.250        # suppress intent this long after airborne
    duck_ratio: float = 0.70              # torso height < ratio*calibrated => ducking

    # Resting-position adaptation.
    rest_ema_alpha: float = 0.05

    # Camera resolution (passed to ThreadedCamera; pose model ignores this).
    frame_width: int = 1280
    frame_height: int = 720

    def lat(self, x: float) -> float:
        """Raw normalized x -> lateral axis (+ = player's right / positive lane)."""
        return (1.0 - x) if self.invert_x else x


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #
class OneEuroFilter:
    """1-D One Euro filter (Casiez et al.). Low lag while moving, smooth at rest."""

    def __init__(self, min_cutoff: float, beta: float, d_cutoff: float = 1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x_prev: Optional[float] = None
        self._dx_prev: float = 0.0
        self._t_prev: Optional[float] = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def reset(self) -> None:
        self._x_prev = None
        self._dx_prev = 0.0
        self._t_prev = None

    def __call__(self, t: float, x: float) -> float:
        if self._t_prev is None or t <= self._t_prev:
            self._t_prev, self._x_prev, self._dx_prev = t, x, 0.0
            return x
        dt = t - self._t_prev
        dx = (x - self._x_prev) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        edx = a_d * dx + (1 - a_d) * self._dx_prev
        cutoff = self.min_cutoff + self.beta * abs(edx)
        a = self._alpha(cutoff, dt)
        x_hat = a * x + (1 - a) * self._x_prev
        self._t_prev, self._x_prev, self._dx_prev = t, x_hat, edx
        return x_hat


class EmaFilter:
    """Exponential moving average."""

    def __init__(self, alpha: float):
        self.alpha = alpha
        self._y: Optional[float] = None

    def reset(self) -> None:
        self._y = None

    def __call__(self, x: float) -> float:
        if self._y is None:
            self._y = x
        else:
            self._y = self.alpha * x + (1 - self.alpha) * self._y
        return self._y


class VelocityEstimator:
    """Velocity (value/sec) from a rolling least-squares fit over a time window.

    Robust to single-frame jitter and independent of frame rate (uses timestamps).
    """

    def __init__(self, window_sec: float, min_samples: int):
        self.window_sec = window_sec
        self.min_samples = min_samples
        self._buf: Deque[Tuple[float, float]] = deque(maxlen=16)

    def reset(self) -> None:
        self._buf.clear()

    def add(self, t: float, value: float) -> None:
        self._buf.append((t, value))

    def velocity(self) -> float:
        if len(self._buf) < 2:
            return 0.0
        t_latest = self._buf[-1][0]
        pts = [(t, v) for (t, v) in self._buf if t >= t_latest - self.window_sec]
        if len(pts) < self.min_samples:
            # Low frame rate: too few samples landed inside the time window (e.g.
            # MediaPipe running at 15-30 fps vs. the 60 fps the window targets).
            # Fall back to the most recent `min_samples` frames so velocity still
            # tracks motion -- with a touch more lag -- instead of collapsing to
            # zero every frame (which would freeze intent detection entirely).
            pts = list(self._buf)[-self.min_samples:]
        n = len(pts)
        if n < 2:
            return 0.0
        t0 = pts[0][0]
        xs = [t - t0 for t, _ in pts]
        ys = [v for _, v in pts]
        mx = sum(xs) / n
        my = sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        if den <= 1e-9:
            return 0.0
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        return num / den


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #
@dataclass
class Calibration:
    """Player-specific geometry and thresholds learned during calibration.

    All x values are in the lateral axis (Config.lat applied), so they are
    directly comparable to the features the engine computes at runtime.
    """
    lane_centers: Dict[int, float]        # {-1: left, 0: center, 1: right} (lat-x)
    boundaries: Tuple[float, float]       # (left|center, center|right) lat-x
    neutral_ankle: Dict[str, float]       # {"left":lat-x, "right":lat-x} at center
    neutral_hip: float                    # hip-center lat-x at center
    step_distance: float                  # typical lateral step (lat-x)
    torso_height: float                   # |hip_y - shoulder_y| standing tall
    vel_threshold: float
    disp_threshold: float

    def nearest_lane(self, x: float) -> int:
        return min(self.lane_centers, key=lambda k: abs(self.lane_centers[k] - x))

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({**asdict(self),
                       "lane_centers": {str(k): v for k, v in self.lane_centers.items()},
                       "neutral_ankle": self.neutral_ankle}, fh, indent=2)

    @classmethod
    def load(cls, path: str) -> "Calibration":
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return cls(
            lane_centers={int(k): float(v) for k, v in d["lane_centers"].items()},
            boundaries=tuple(d["boundaries"]),
            neutral_ankle={k: float(v) for k, v in d["neutral_ankle"].items()},
            neutral_hip=float(d["neutral_hip"]),
            step_distance=float(d["step_distance"]),
            torso_height=float(d["torso_height"]),
            vel_threshold=float(d["vel_threshold"]),
            disp_threshold=float(d["disp_threshold"]),
        )

    @classmethod
    def default(cls, cfg: Config) -> "Calibration":
        """Reasonable un-calibrated fallback so the machine can start without a
        calibration pass. Real runs should calibrate; this does not assume the
        lanes are exact screen thirds, it is just a neutral starting geometry.
        Centers are already in the lateral axis."""
        centers = {-1: 0.25, 0: 0.5, 1: 0.75}
        step = abs(centers[0] - centers[-1])
        return cls(
            lane_centers=centers,
            boundaries=((centers[-1] + centers[0]) / 2, (centers[0] + centers[1]) / 2),
            neutral_ankle={"left": centers[0] - 0.05, "right": centers[0] + 0.05},
            neutral_hip=centers[0],
            step_distance=step,
            # Conservative standing torso baseline: low enough that an
            # uncalibrated normal stance never reads as a duck (only a real,
            # deep crouch does). Calibration replaces this with a measurement.
            torso_height=0.18,
            vel_threshold=cfg.vel_threshold,
            disp_threshold=cfg.disp_threshold,
        )


class CalibrationPhase(Enum):
    CENTER = "center"
    LEFT = "left"
    CENTER_RETURN = "center_return"
    RIGHT = "right"
    DONE = "done"


class Calibrator:
    """Collects per-phase samples and computes a Calibration.

    The app advances phases (e.g. on a key press once the player is in position);
    this class just accumulates lateral positions and reduces them.
    """

    ORDER = [CalibrationPhase.CENTER, CalibrationPhase.LEFT,
             CalibrationPhase.CENTER_RETURN, CalibrationPhase.RIGHT]

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.phase_idx = 0
        self._samples: Dict[CalibrationPhase, list] = {p: [] for p in self.ORDER}

    @property
    def phase(self) -> CalibrationPhase:
        if self.phase_idx >= len(self.ORDER):
            return CalibrationPhase.DONE
        return self.ORDER[self.phase_idx]

    def add(self, features: "Features") -> None:
        """Record one sample for the current phase (body_x, hips, ankles, torso)."""
        if self.phase is CalibrationPhase.DONE or not features.has_lower_body:
            return
        self._samples[self.phase].append((
            features.body_x, features.hip_x,
            features.left.x, features.right.x, features.torso_height,
        ))

    def next_phase(self) -> bool:
        """Advance to the next phase; returns True when calibration is complete."""
        self.phase_idx += 1
        return self.phase is CalibrationPhase.DONE

    @staticmethod
    def _mean(rows, idx) -> float:
        vals = [r[idx] for r in rows]
        return sum(vals) / len(vals) if vals else 0.0

    def compute(self) -> Calibration:
        center_rows = self._samples[CalibrationPhase.CENTER] + self._samples[CalibrationPhase.CENTER_RETURN]
        left_rows = self._samples[CalibrationPhase.LEFT]
        right_rows = self._samples[CalibrationPhase.RIGHT]
        if not (center_rows and left_rows and right_rows):
            raise ValueError("calibration incomplete: need center, left and right samples")

        center_x = self._mean(center_rows, 1)   # hip-center lat-x
        left_x = self._mean(left_rows, 1)
        right_x = self._mean(right_rows, 1)
        # Order-agnostic: whichever phase sits at smaller lat-x is the left lane.
        lo, hi = sorted([left_x, right_x])
        centers = {-1: lo, 0: center_x, 1: hi}

        step = max(abs(centers[0] - centers[-1]), abs(centers[1] - centers[0]))
        neutral_ankle = {
            "left": self._mean(center_rows, 2),
            "right": self._mean(center_rows, 3),
        }
        torso = self._mean(center_rows, 4)

        # Thresholds derived from the player's real step size. Commit once the foot
        # has displaced ~22% of a full step, and treat "fast" as covering a step in
        # ~0.8 s. These fire early and tolerate webcam frame rates; the velocity-OR-
        # displacement gate (see _try_intent) means a slow but committed step still
        # registers even when the measured velocity stays low.
        disp_threshold = max(0.02, 0.22 * step)
        vel_threshold = max(0.18, step / 0.8)

        return Calibration(
            lane_centers=centers,
            boundaries=((centers[-1] + centers[0]) / 2, (centers[0] + centers[1]) / 2),
            neutral_ankle=neutral_ankle,
            neutral_hip=center_x,
            step_distance=step,
            torso_height=torso if torso > 0 else 0.30,
            vel_threshold=vel_threshold,
            disp_threshold=disp_threshold,
        )


# --------------------------------------------------------------------------- #
# Feature extraction
# --------------------------------------------------------------------------- #
@dataclass
class FootFeature:
    x: float          # lateral position (+ = player's right)
    y: float          # raw image y (down = +)
    vx: float         # lateral velocity (width/s, + = right)
    vy: float         # vertical velocity (height/s, + = down)
    conf: float
    source: str       # which landmark provided the position
    reliable: bool    # confidence >= gate and source is foot-class


@dataclass
class Features:
    t: float
    left: FootFeature
    right: FootFeature
    knee_vx: Dict[str, float]
    knee_conf: Dict[str, float]
    hip_x: float
    hip_vx: float
    hip_vy: float
    hip_conf: float
    body_x: float
    body_conf: float
    torso_height: float
    has_lower_body: bool


class FeatureExtractor:
    """Turns a stream of landmark dicts into filtered, fused lower-body features.

    Holds one filter + velocity estimator per tracked signal. Feeds the velocity
    estimators the *filtered* positions so velocity is both smooth and low-lag.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        mk_pos = self._make_foot_filter
        self._foot_x = {"left": mk_pos(), "right": mk_pos()}
        self._foot_y = {"left": mk_pos(), "right": mk_pos()}
        self._foot_vx = {"left": self._mk_vel(), "right": self._mk_vel()}
        self._foot_vy = {"left": self._mk_vel(), "right": self._mk_vel()}
        self._foot_src = {"left": None, "right": None}
        self._knee_vx = {"left": self._mk_vel(), "right": self._mk_vel()}
        self._hip_x = EmaFilter(cfg.hip_ema_alpha)
        self._hip_vx = self._mk_vel()
        self._hip_vy = self._mk_vel()
        self._body_x = EmaFilter(cfg.body_ema_alpha)

    def _make_foot_filter(self):
        if self.cfg.use_oneeuro_for_feet:
            return OneEuroFilter(self.cfg.foot_oneeuro_min_cutoff, self.cfg.foot_oneeuro_beta)
        return EmaFilter(self.cfg.foot_ema_alpha)

    def _mk_vel(self) -> VelocityEstimator:
        return VelocityEstimator(self.cfg.velocity_window_sec, self.cfg.velocity_min_samples)

    def _apply_pos(self, flt, t, value):
        return flt(t, value) if isinstance(flt, OneEuroFilter) else flt(value)

    def _pick_foot(self, lm: Dict[str, Optional[Landmark]], side: str):
        """Confidence-gated fallback chain ankle -> toe -> heel -> knee."""
        gate = self.cfg.landmark_conf_min
        best = None
        for name in _FOOT_CHAIN[side]:
            p = lm.get(name)
            if p is None:
                continue
            if p.conf >= gate:
                return name, p, True          # first reliable point in priority order
            if best is None:
                best = (name, p)
        if best is not None:                   # nothing reliable: best-effort, flagged unreliable
            return best[0], best[1], False
        return None, None, False

    def update(self, lm: Dict[str, Optional[Landmark]], t: float,
               bbox: Optional[Tuple[float, float]] = None) -> Features:
        cfg = self.cfg
        gate = cfg.landmark_conf_min

        feet: Dict[str, FootFeature] = {}
        for side in ("left", "right"):
            name, p, reliable = self._pick_foot(lm, side)
            src = name or "none"
            if self._foot_src[side] != src:     # source switched -> avoid a velocity spike
                self._foot_vx[side].reset()
                self._foot_vy[side].reset()
                self._foot_x[side].reset() if hasattr(self._foot_x[side], "reset") else None
                self._foot_y[side].reset() if hasattr(self._foot_y[side], "reset") else None
                self._foot_src[side] = src
            if p is None:
                feet[side] = FootFeature(0.0, 0.0, 0.0, 0.0, 0.0, "none", False)
                continue
            lx = cfg.lat(p.x)
            fx = self._apply_pos(self._foot_x[side], t, lx)   # smoothed: position/disp/viz
            fy = self._apply_pos(self._foot_y[side], t, p.y)
            self._foot_vx[side].add(t, lx)                    # raw: lowest-lag velocity
            self._foot_vy[side].add(t, p.y)
            feet[side] = FootFeature(
                x=fx, y=fy,
                vx=self._foot_vx[side].velocity(),
                vy=self._foot_vy[side].velocity(),
                conf=p.conf, source=src, reliable=reliable,
            )

        knee_vx, knee_conf = {}, {}
        for side in ("left", "right"):
            p = lm.get(f"{side}_knee")
            if p is not None and p.conf >= gate:
                self._knee_vx[side].add(t, cfg.lat(p.x))      # raw for low-lag velocity
                knee_vx[side] = self._knee_vx[side].velocity()
                knee_conf[side] = p.conf
            else:
                knee_vx[side] = 0.0
                knee_conf[side] = p.conf if p else 0.0

        hip_x, hip_conf, hip_y_raw = self._midpoint(lm, "hip", gate, bbox)
        body_x = self._body_x_value(lm, gate, hip_x, bbox)
        shoulder_x, _, shoulder_y_raw = self._midpoint(lm, "shoulder", gate, bbox)

        hip_xf = self._hip_x(hip_x)            # smoothed: used for lane confirmation
        self._hip_vx.add(t, hip_x)             # raw: low-lag hip velocity (support signal)
        torso_height = abs(hip_y_raw - shoulder_y_raw) if (hip_y_raw is not None and shoulder_y_raw is not None) else 0.0
        if hip_y_raw is not None:
            self._hip_vy.add(t, hip_y_raw)

        body_xf = self._body_x(body_x)
        has_lower = feet["left"].source != "none" or feet["right"].source != "none" or hip_conf >= gate

        return Features(
            t=t,
            left=feet["left"], right=feet["right"],
            knee_vx=knee_vx, knee_conf=knee_conf,
            hip_x=hip_xf, hip_vx=self._hip_vx.velocity(), hip_vy=self._hip_vy.velocity(),
            hip_conf=hip_conf,
            body_x=body_xf, body_conf=hip_conf,
            torso_height=torso_height,
            has_lower_body=has_lower,
        )

    def _midpoint(self, lm, joint, gate, bbox):
        """Mean lateral x of left/right <joint>, with bbox fallback. Returns
        (lat_x, confidence, raw_y_mid)."""
        l = lm.get(f"left_{joint}")
        r = lm.get(f"right_{joint}")
        good = [p for p in (l, r) if p is not None and p.conf >= gate]
        if good:
            lat_x = sum(self.cfg.lat(p.x) for p in good) / len(good)
            y_mid = sum(p.y for p in good) / len(good)
            conf = sum(p.conf for p in good) / len(good)
            return lat_x, conf, y_mid
        if bbox is not None:
            return self.cfg.lat(bbox[0]), 0.0, None
        return 0.5, 0.0, None

    def _body_x_value(self, lm, gate, hip_x, bbox):
        """torso control point: 0.7*hip_mid + 0.3*shoulder_mid (lat). Falls back to
        shoulder mid, then hip-only, then bbox center."""
        hip_x_v, hip_conf, _ = self._midpoint(lm, "hip", gate, bbox)
        sh_x_v, sh_conf, _ = self._midpoint(lm, "shoulder", gate, bbox)
        if hip_conf >= gate and sh_conf >= gate:
            return 0.7 * hip_x_v + 0.3 * sh_x_v
        if sh_conf >= gate:
            return sh_x_v
        if hip_conf >= gate:
            return hip_x_v
        if bbox is not None:
            return self.cfg.lat(bbox[0])
        return hip_x


# --------------------------------------------------------------------------- #
# Two-stage lane state machine
# --------------------------------------------------------------------------- #
class State(Enum):
    STABLE_LEFT = -1
    STABLE_CENTER = 0
    STABLE_RIGHT = 1
    MOVING_LEFT = 10
    MOVING_RIGHT = 11

    @property
    def is_stable(self) -> bool:
        return self in (State.STABLE_LEFT, State.STABLE_CENTER, State.STABLE_RIGHT)


@dataclass
class Decision:
    """Result of one state-machine update (returned every frame)."""
    command: Optional[str]          # "left" | "right" | None
    state: State
    current_lane: int
    leading_foot: Optional[str]
    intent_confidence: float
    suppressed: bool                # airborne/duck suppression active
    reason: str                     # short human label for debugging
    # live intent diagnostics (lead foot), for the debug overlay / tuning
    lead_vx: float = 0.0            # |lead-foot horizontal velocity| (width/s)
    lead_disp: float = 0.0         # lead-foot signed displacement from rest
    vel_thr: float = 0.0           # current velocity threshold
    disp_thr: float = 0.0          # current displacement threshold
    controller: str = "feet"       # which controller produced this ("feet"|"position")
    body_x: float = 0.0            # tracked lateral body position (position mode)


_LANE_TO_STABLE = {-1: State.STABLE_LEFT, 0: State.STABLE_CENTER, 1: State.STABLE_RIGHT}


class LaneStateMachine:
    """Feet-first intent (stage 1) + hip-confirmed occupancy (stage 2).

    Emits a single "left"/"right" command the moment reliable lateral foot motion
    is detected — before the torso crosses a lane boundary — then uses the hips to
    confirm arrival (or cancel an aborted step) without re-emitting.
    """

    def __init__(self, cfg: Config, calib: Calibration, emit: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.calib = calib
        self._emit = emit or (lambda _cmd: None)

        self.state = State.STABLE_CENTER
        self.current_lane = 0
        self.last_command: Optional[str] = None
        self.last_command_t: float = -1e9
        self.intent_confidence: float = 0.0

        # rest positions per foot (lat-x), adapted slowly while standing still
        self.rest = dict(calib.neutral_ankle)

        # intent debounce
        self._pending_dir = 0
        self._pending_since = 0.0

        # re-arm gate: after landing in a lane the feet must settle once before a
        # new step counts (stops a step's return-stroke firing the opposite lane)
        self._armed = True

        # active move bookkeeping
        self._origin = 0
        self._target = 0
        self._move_start = 0.0

        # airborne / refractory
        self._airborne_until = 0.0

    # -- helpers ----------------------------------------------------------- #
    def _vel_thr(self) -> float:
        return self.calib.vel_threshold

    def _disp_thr(self) -> float:
        return self.calib.disp_threshold

    def _suppressed(self, f: Features, now: float) -> bool:
        cfg = self.cfg
        airborne = (f.hip_vy < -cfg.jump_vy_threshold or
                    (f.left.vy < -cfg.ankle_jump_vy_threshold and f.right.vy < -cfg.ankle_jump_vy_threshold))
        if airborne:
            self._airborne_until = now + cfg.land_refractory
        ducking = (self.calib.torso_height > 0 and f.torso_height > 0 and
                   f.torso_height < cfg.duck_ratio * self.calib.torso_height)
        return now < self._airborne_until or ducking

    def _lead_foot(self, f: Features):
        """Foot with the strongest *reliable* horizontal speed (direction by
        velocity, never by foot identity — supports crossover steps)."""
        cands = [(s, getattr(f, s)) for s in ("left", "right")]
        reliable = [(s, ff) for s, ff in cands if ff.reliable]
        pool = reliable if reliable else [(s, ff) for s, ff in cands if ff.source != "none"]
        if not pool:
            return None, None
        s, ff = max(pool, key=lambda t: abs(t[1].vx))
        return s, ff

    def _hip_track(self, f: Features) -> float:
        """Confirmation signal: hips, falling back to torso body_x."""
        return f.hip_x if f.hip_conf >= self.cfg.landmark_conf_min else f.body_x

    def _adapt_rest(self, f: Features) -> None:
        a = self.cfg.rest_ema_alpha
        for s in ("left", "right"):
            ff = getattr(f, s)
            if ff.source != "none" and abs(ff.vx) < self.cfg.settle_vel:
                self.rest[s] = (1 - a) * self.rest[s] + a * ff.x

    def _snap_rest(self, f: Features) -> None:
        for s in ("left", "right"):
            ff = getattr(f, s)
            if ff.source != "none":
                self.rest[s] = ff.x

    # -- main update ------------------------------------------------------- #
    def update(self, f: Features, now: float) -> Decision:
        cfg = self.cfg
        suppressed = self._suppressed(f, now)
        lead_side, lead = self._lead_foot(f)

        command: Optional[str] = None
        reason = ""

        if self.state.is_stable:
            self._adapt_rest(f)
            if suppressed:
                self._pending_dir = 0
                reason = "suppressed"
            else:
                command, reason = self._try_intent(f, lead_side, lead, now)
        else:
            reason = self._track_move(f, lead, now)

        # live diagnostics for the lead foot (mirrors the _try_intent gate inputs)
        lead_vx = abs(lead.vx) if lead is not None else 0.0
        lead_disp = 0.0
        if lead is not None and lead_side is not None:
            d = 1 if lead.vx > 0 else -1
            lead_disp = (lead.x - self.rest[lead_side]) * d

        return Decision(
            command=command,
            state=self.state,
            current_lane=self.current_lane,
            leading_foot=lead_side,
            intent_confidence=self.intent_confidence,
            suppressed=suppressed,
            reason=reason,
            lead_vx=lead_vx,
            lead_disp=lead_disp,
            vel_thr=self._vel_thr(),
            disp_thr=self._disp_thr(),
        )

    def _try_intent(self, f: Features, lead_side, lead, now: float):
        cfg = self.cfg
        if lead is None or lead.source == "none":
            self._pending_dir = 0
            self.intent_confidence = 0.0
            return None, "no foot"

        # must settle in the current lane before a new step is accepted
        if not self._armed:
            left_settled = f.left.source == "none" or abs(f.left.vx) < cfg.settle_vel
            right_settled = f.right.source == "none" or abs(f.right.vx) < cfg.settle_vel
            if left_settled and right_settled:
                self._armed = True
                self._snap_rest(f)
            else:
                self._pending_dir = 0
                return None, "settling"

        d = 1 if lead.vx > 0 else -1
        disp = (lead.x - self.rest[lead_side]) * d           # signed in motion dir
        vx = abs(lead.vx)
        vy = abs(lead.vy)

        # Strong foot signal: horizontal must dominate vertical, and the foot must
        # EITHER be moving decisively fast (velocity path => fires early, before the
        # foot has travelled far) OR have displaced past the commit threshold (a
        # slow, deliberate step still fires). Requiring *both* a high speed AND
        # displacement was too strict and froze intent at webcam frame rates.
        horiz_ok = vx >= cfg.horiz_dominance * vy
        fast = vx >= self._vel_thr()
        displaced = disp >= self._disp_thr()
        strong_foot = horiz_ok and (fast or displaced)

        # supporting signals (need >= 1): knee same dir, hip same dir, or a big
        # committed foot displacement (self-supports when knee/hip unreliable).
        knee_support = f.knee_vx.get(lead_side, 0.0) * d >= cfg.knee_vel_threshold
        hip_support = f.hip_vx * d >= cfg.hip_vel_threshold
        disp_support = disp >= cfg.strong_disp_threshold
        support = knee_support or hip_support or disp_support

        # intent confidence (display + soft gate)
        self.intent_confidence = _clamp01((
            min(vx / max(self._vel_thr(), 1e-6), 2.0) / 2.0 * 0.5
            + min(disp / max(self._disp_thr(), 1e-6), 2.0) / 2.0 * 0.3
            + (1.0 if support else 0.0) * 0.2
        )) if strong_foot else 0.0

        if not (strong_foot and support):
            self._pending_dir = 0
            return None, "weak" if lead.source != "none" else "no foot"

        # time-based debounce: the strong signal must persist in one direction
        if self._pending_dir != d:
            self._pending_dir = d
            self._pending_since = now
            return None, "arming"
        if now - self._pending_since < cfg.confirm_time:
            return None, "arming"
        if now - self.last_command_t < cfg.rearm_time:
            return None, "rearm"

        target = max(-1, min(1, self.current_lane + d))
        if target == self.current_lane:
            self._pending_dir = 0
            return None, "at edge"

        # ---- Stage 1: emit ONE command immediately on confirmed foot intent ----
        cmd = "right" if d > 0 else "left"
        self._origin = self.current_lane
        self._target = target
        self._move_start = now
        self.state = State.MOVING_RIGHT if d > 0 else State.MOVING_LEFT
        self.last_command = cmd
        self.last_command_t = now
        self._pending_dir = 0
        self._emit(cmd)
        return cmd, f"intent {cmd} (foot={lead_side})"

    def _track_move(self, f: Features, lead, now: float) -> str:
        cfg = self.cfg
        hip = self._hip_track(f)
        target_c = self.calib.lane_centers[self._target]
        origin_c = self.calib.lane_centers[self._origin]

        # ---- Stage 2a: confirm arrival in the target lane ----
        if abs(hip - target_c) <= cfg.confirm_radius:
            self.current_lane = self._target
            self.state = _LANE_TO_STABLE[self._target]
            self._snap_rest(f)
            self._armed = False
            return "confirmed"

        # ---- Stage 2b: cancel an aborted step (back at origin, feet settled) ----
        foot_settled = lead is None or abs(lead.vx) < cfg.settle_vel
        if (now - self._move_start >= cfg.abort_grace
                and abs(hip - origin_c) <= cfg.confirm_radius
                and foot_settled):
            self.current_lane = self._origin
            self.state = _LANE_TO_STABLE[self._origin]
            self._snap_rest(f)
            self._armed = False
            if cfg.resync_on_cancel and self.last_command:
                opp = "left" if self.last_command == "right" else "right"
                self.last_command = opp
                self.last_command_t = now
                self._emit(opp)
                return "cancelled+resync"
            return "cancelled"

        # ---- Timeout: resync internal state to nearest lane (no command) ----
        if now - self._move_start > cfg.move_timeout:
            lane = self.calib.nearest_lane(hip)
            self.current_lane = lane
            self.state = _LANE_TO_STABLE[lane]
            self._snap_rest(f)
            self._armed = False
            return "timeout-resync"

        return "moving"


class PositionLaneController:
    """Robust, position-based lane controller (the default).

    Where the two-stage ``LaneStateMachine`` infers an *early* lane-change intent
    from foot velocity (low latency, but direction-ambiguous and fragile: the lead
    foot flips mid-step and the per-foot displacement gate is asymmetric), this
    controller maps the player's **lateral body position** straight onto the
    nearest calibrated lane. It emits one "left"/"right" command per calibrated
    boundary crossed, with a hysteresis margin so standing on a line doesn't
    chatter. Symmetric by construction and easy to reason about: where your hips
    are *is* where the avatar is.

    Trade-off vs. feet mode: the command fires when your torso actually crosses
    the lane boundary rather than ~80 ms earlier on the first foot twitch. For a
    step-to-move game that is plenty responsive and far more reliable.
    """

    def __init__(self, cfg: Config, calib: Calibration,
                 emit: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.calib = calib
        self._emit = emit or (lambda _cmd: None)

        self.state = State.STABLE_CENTER
        self.current_lane = 0
        self.last_command: Optional[str] = None
        self.last_command_t: float = -1e9
        self.intent_confidence: float = 0.0

        self._airborne_until = 0.0
        # Must cross a boundary by this margin before the lane switches (kills
        # jitter when the player straddles a line). Scaled to the player's step.
        self._margin = max(0.02, 0.18 * calib.step_distance)

    # -- helpers (mirror LaneStateMachine so suppression behaves identically) -- #
    def _body_x(self, f: Features) -> float:
        return f.hip_x if f.hip_conf >= self.cfg.landmark_conf_min else f.body_x

    def _suppressed(self, f: Features, now: float) -> bool:
        cfg = self.cfg
        airborne = (f.hip_vy < -cfg.jump_vy_threshold or
                    (f.left.vy < -cfg.ankle_jump_vy_threshold and
                     f.right.vy < -cfg.ankle_jump_vy_threshold))
        if airborne:
            self._airborne_until = now + cfg.land_refractory
        ducking = (self.calib.torso_height > 0 and f.torso_height > 0 and
                   f.torso_height < cfg.duck_ratio * self.calib.torso_height)
        return now < self._airborne_until or ducking

    def _target_lane(self, bx: float) -> int:
        """Nearest lane for body-x ``bx`` with hysteresis around the current lane.

        Only crosses a boundary once ``bx`` is past it by ``self._margin``; a body
        far enough out can jump straight from a side lane through center.
        """
        b0, b1 = self.calib.boundaries          # (-1|0 boundary, 0|1 boundary)
        m = self._margin
        lane = self.current_lane
        # move toward +x (player's right) ...
        while lane < 1 and bx > (b0 if lane == -1 else b1) + m:
            lane += 1
        # ... or toward -x (player's left)
        while lane > -1 and bx < (b1 if lane == 1 else b0) - m:
            lane -= 1
        return lane

    def update(self, f: Features, now: float) -> Decision:
        suppressed = self._suppressed(f, now)
        bx = self._body_x(f)

        command: Optional[str] = None
        reason = "tracking"

        if suppressed:
            reason = "suppressed"
        else:
            target = self._target_lane(bx)
            if target != self.current_lane and now - self.last_command_t >= self.cfg.rearm_time:
                step = 1 if target > self.current_lane else -1
                self.current_lane += step               # advance one lane per frame
                command = "right" if step > 0 else "left"
                self.last_command = command
                self.last_command_t = now
                self._emit(command)
                reason = f"-> lane {self.current_lane:+d}"
            elif target != self.current_lane:
                reason = "rearm"

        self.state = _LANE_TO_STABLE[self.current_lane]

        # confidence = how far past the relevant boundary the body is (0..1 of a margin)
        b0, b1 = self.calib.boundaries
        edge = b0 if bx < self.calib.lane_centers[0] else b1
        self.intent_confidence = _clamp01(abs(bx - edge) / max(self._margin, 1e-6))

        return Decision(
            command=command,
            state=self.state,
            current_lane=self.current_lane,
            leading_foot=None,
            intent_confidence=self.intent_confidence,
            suppressed=suppressed,
            reason=reason,
            controller="position",
            body_x=bx,
            disp_thr=self._margin,
        )


def _clamp01(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1 else v
