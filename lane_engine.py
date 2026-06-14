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

import csv
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
    rearm_time: float = 0.110             # min gap between emitted commands

    # Hybrid lane detector: feet are predicted slightly forward for low-latency
    # intent, while body/COM confirms and recent lane state gives mild hysteresis.
    hybrid_foot_prediction_sec: float = 0.11
    hybrid_enter_score: float = 1.08
    hybrid_center_score: float = 1.05
    hybrid_fast_foot_score: float = 1.35
    hybrid_body_score: float = 0.85
    hybrid_both_feet_score: float = 1.25
    hybrid_one_foot_score: float = 0.38
    hybrid_velocity_score: float = 0.34
    hybrid_hold_score: float = 0.28
    hybrid_lead_lock_sec: float = 0.34
    hybrid_lead_display_sec: float = 0.24
    hybrid_trail_ignore_sec: float = 0.42
    hybrid_spent_lead_sec: float = 0.58
    hybrid_center_settle_sec: float = 0.28
    hybrid_confirmation_hold_sec: float = 0.34
    hybrid_body_shift_ratio: float = 0.12
    hybrid_body_velocity_ratio: float = 0.55
    hybrid_com_shift_ratio: float = 0.08
    hybrid_com_velocity_ratio: float = 0.30
    hybrid_center_return_shift_ratio: float = 0.24
    hybrid_center_return_velocity_ratio: float = 0.38

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

    # Vertical gesture (jump / crouch) detection. All offsets/velocities are in
    # torso-lengths from the calibrated neutral chest height. Image-y increases
    # DOWNWARD, so a jump is a NEGATIVE offset/velocity and a crouch is POSITIVE.
    jump_offset: float = -0.15            # chest this far up (torso-lengths) => jump
    jump_vel: float = -0.85               # ...with at least this much upward speed (torso/s)
    duck_offset: float = 0.35             # chest this far down => crouch
    duck_vel: float = 0.75                # ...with at least this much downward speed (torso/s)
    duck_hold_time: float = 0.06          # or just held below duck_offset this long (slow squat)
    gesture_cooldown: float = 0.25        # min gap between emitted gestures
    jump_rearm_offset: float = -0.12      # offset must rise above this to re-arm jump
    duck_rearm_offset: float = 0.16       # offset must fall below this to re-arm crouch

    # Resting-position adaptation.
    rest_ema_alpha: float = 0.05

    # Camera resolution (passed to ThreadedCamera; pose model ignores this).
    frame_width: int = 960
    frame_height: int = 540

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
    neutral_body_y: float                 # chest control-point image-y standing neutral
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
            # Backwards-compatible: older calibration files predate vertical
            # gestures. Fall back to a sensible standing chest height.
            neutral_body_y=float(d.get("neutral_body_y", 0.42)),
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
            # Neutral chest height for vertical gestures; replaced by calibration.
            neutral_body_y=0.42,
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
        """Record one sample for the current phase (body_x, hips, ankles, torso,
        chest-y)."""
        if self.phase is CalibrationPhase.DONE or not features.has_lower_body:
            return
        self._samples[self.phase].append((
            features.body_x, features.hip_x,
            features.left.x, features.right.x, features.torso_height,
            features.body_y,
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
        neutral_body_y = self._mean(center_rows, 5)

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
            neutral_body_y=neutral_body_y if neutral_body_y > 0 else 0.42,
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
    # vertical (jump / crouch) signals -- image-y, down = +
    hip_y: float = 0.0            # hip-midpoint image-y (raw)
    shoulder_y: float = 0.0      # shoulder-midpoint image-y (raw)
    body_y: float = 0.0          # chest control point (0.65*shoulder + 0.35*hip), filtered
    body_vy: float = 0.0         # chest vertical velocity (height/s, + = down)
    body_y_conf: float = 0.0     # confidence backing body_y (0 => untrusted)


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
        self._body_y = EmaFilter(cfg.body_ema_alpha)   # chest height: smoothed for offset
        self._body_vy = self._mk_vel()                 # chest height: raw for low-lag velocity

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
        shoulder_x, shoulder_conf, shoulder_y_raw = self._midpoint(lm, "shoulder", gate, bbox)

        hip_xf = self._hip_x(hip_x)            # smoothed: used for lane confirmation
        self._hip_vx.add(t, hip_x)             # raw: low-lag hip velocity (support signal)
        torso_height = abs(hip_y_raw - shoulder_y_raw) if (hip_y_raw is not None and shoulder_y_raw is not None) else 0.0
        if hip_y_raw is not None:
            self._hip_vy.add(t, hip_y_raw)

        # Chest control point for vertical (jump / crouch) gestures.
        body_y_raw, body_y_conf = self._body_y_value(hip_y_raw, hip_conf, shoulder_y_raw, shoulder_conf, gate)
        if body_y_raw is not None:
            body_yf = self._body_y(body_y_raw)   # smoothed: offset / threshold crossing
            self._body_vy.add(t, body_y_raw)     # raw: low-lag vertical velocity
            body_vy = self._body_vy.velocity()
        else:
            body_yf, body_vy = 0.0, 0.0          # untrusted frame: conf 0 => detector ignores

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
            hip_y=hip_y_raw if hip_y_raw is not None else 0.0,
            shoulder_y=shoulder_y_raw if shoulder_y_raw is not None else 0.0,
            body_y=body_yf, body_vy=body_vy, body_y_conf=body_y_conf,
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

    def _body_y_value(self, hip_y, hip_conf, sh_y, sh_conf, gate):
        """Chest control-point image-y: 0.65*shoulder + 0.35*hip when both are
        confident, else the available upper-body point. Returns ``(y, conf)`` with
        conf 0 (and y None) when neither shoulder nor hip is trustworthy."""
        have_sh = sh_conf >= gate and sh_y is not None
        have_hip = hip_conf >= gate and hip_y is not None
        if have_sh and have_hip:
            return 0.65 * sh_y + 0.35 * hip_y, min(sh_conf, hip_conf)
        if have_sh:
            return sh_y, sh_conf
        if have_hip:
            return hip_y, hip_conf
        return None, 0.0


# --------------------------------------------------------------------------- #
# Vertical gesture (jump / crouch) detection
# --------------------------------------------------------------------------- #
@dataclass
class GestureDecision:
    """Result of one VerticalGestureDetector update (returned every frame)."""
    jump: bool                      # one-shot: True on the frame a jump fires
    duck: bool                      # one-shot: True on the frame a crouch fires
    vertical_offset: float          # chest offset from neutral, torso-lengths (+ = down)
    vertical_velocity: float        # chest vertical velocity, torso/s (+ = down)
    jump_armed: bool                # ready to fire a new jump
    duck_armed: bool                # ready to fire a new crouch
    reason: str                     # short human label for debugging
    active: Optional[str] = None    # gesture to *display* now: "jump"/"duck", held for the
                                    # cooldown window after it fired, else None
    cooldown_remaining: float = 0.0  # seconds left in the cooldown (0 once elapsed)


def vertical_chest_signal(cfg: "Config", calib: "Calibration", f: "Features"):
    """(offset, velocity) of the chest from the calibrated neutral height, both in
    torso-lengths (image-y down = +, so up/jump is negative). Returns ``(0.0, 0.0)``
    when the upper body is not trustworthy this frame so callers treat it as quiet."""
    if f.body_y_conf < cfg.landmark_conf_min:
        return 0.0, 0.0
    th = max(calib.torso_height, 0.12)
    return (f.body_y - calib.neutral_body_y) / th, f.body_vy / th


def vertical_gesture_active(cfg: "Config", calib: "Calibration", f: "Features") -> bool:
    """True while a jump or crouch is in progress, used to suppress lane changes so
    side movement is ignored during the vertical gesture window. Spans the whole arc
    of a gesture: fast vertical velocity (onset) or the chest still displaced past
    the re-arm bands (held), so a sidestep mid-gesture can't slip a lane through."""
    offset, vel = vertical_chest_signal(cfg, calib, f)
    return (offset <= cfg.jump_rearm_offset or offset >= cfg.duck_rearm_offset
            or vel <= cfg.jump_vel or vel >= cfg.duck_vel)


class VerticalGestureDetector:
    """Detects one-shot jump / crouch gestures from the chest control point.

    Pure logic (no IO): feed the same ``Features`` the lane controllers use plus a
    timestamp. A jump fires on a fast upward chest displacement; a crouch fires on a
    fast downward one OR simply holding low (a slow squat). Each gesture fires once
    and must re-arm (chest back near neutral) before firing again; a shared cooldown
    stops jump/crouch bouncing. Jump and crouch are mutually exclusive in a frame —
    the sign of the vertical offset picks which one is even eligible.
    """

    def __init__(self, cfg: "Config", calib: "Calibration"):
        self.cfg = cfg
        self.calib = calib
        self._jump_armed = True
        self._duck_armed = True
        self._last_fire_t = -1e9
        self._last_gesture: Optional[str] = None     # which gesture last fired (for the indicator)
        self._duck_hold_since: Optional[float] = None

    def reset(self) -> None:
        self._jump_armed = True
        self._duck_armed = True
        self._last_fire_t = -1e9
        self._last_gesture = None
        self._duck_hold_since = None

    def _display_state(self, now: float):
        """(active_gesture, cooldown_remaining) for the on-screen indicator: the
        last-fired gesture is 'held' for the whole cooldown window, then clears."""
        remaining = self.cfg.gesture_cooldown - (now - self._last_fire_t)
        if remaining > 0.0 and self._last_gesture is not None:
            return self._last_gesture, remaining
        return None, 0.0

    def update(self, f: "Features", now: float) -> GestureDecision:
        cfg = self.cfg
        offset, vel = vertical_chest_signal(cfg, self.calib, f)

        if f.body_y_conf < cfg.landmark_conf_min:
            # Upper body untrusted: emit nothing, keep arm state, reset hold timer.
            self._duck_hold_since = None
            active, cd = self._display_state(now)
            return GestureDecision(False, False, 0.0, 0.0,
                                   self._jump_armed, self._duck_armed, "no-body",
                                   active, cd)

        # Re-arm each gesture once the chest has returned toward neutral.
        if offset > cfg.jump_rearm_offset:
            self._jump_armed = True
        if offset < cfg.duck_rearm_offset:
            self._duck_armed = True

        # Track how long the chest has been held below the crouch line (slow squat).
        if offset >= cfg.duck_offset:
            if self._duck_hold_since is None:
                self._duck_hold_since = now
        else:
            self._duck_hold_since = None

        jump = duck = False
        reason = "neutral"
        if (now - self._last_fire_t) < cfg.gesture_cooldown:
            reason = "cooldown"
        elif offset <= cfg.jump_offset:                       # eligible for a jump
            if self._jump_armed and vel <= cfg.jump_vel:
                jump = True
            else:
                reason = "jump-wait" if self._jump_armed else "jump-spent"
        elif offset >= cfg.duck_offset:                       # eligible for a crouch
            held = (self._duck_hold_since is not None and
                    now - self._duck_hold_since >= cfg.duck_hold_time)
            if self._duck_armed and (vel >= cfg.duck_vel or held):
                duck = True
            else:
                reason = "duck-wait" if self._duck_armed else "duck-spent"

        if jump:
            self._jump_armed = False
            self._last_fire_t = now
            self._last_gesture = "jump"
            reason = "jump"
        elif duck:
            self._duck_armed = False
            self._last_fire_t = now
            self._last_gesture = "duck"
            reason = "duck"

        active, cd = self._display_state(now)
        return GestureDecision(jump, duck, offset, vel,
                               self._jump_armed, self._duck_armed, reason,
                               active, cd)


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
    command: Optional[str]          # legacy "left" | "right" | None
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
    controller: str = "feet"       # which controller produced this
    body_x: float = 0.0            # tracked lateral body position


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
        # Also ignore side movement while a vertical jump/crouch gesture is underway.
        return (now < self._airborne_until or ducking
                or vertical_gesture_active(cfg, self.calib, f))

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


class HybridLaneController:
    """Hybrid feet + body lane detector.

    The fast path predicts both feet a short time forward and scores the side
    they are moving into. The stable path scores the body/hip lane and both-foot
    agreement. This avoids the old feet controller's per-frame lead-foot latch,
    which could get stuck left or flip between legs during rightward movement.
    """

    _CSV_FIELDS = [
        "t",
        # body / CoM
        "body_x", "body_region", "com_direction", "hip_vx", "hip_x", "hip_conf",
        # left foot
        "lf_x", "lf_vx", "lf_vy", "lf_pred", "lf_region", "lf_src", "lf_reliable",
        # right foot
        "rf_x", "rf_vx", "rf_vy", "rf_pred", "rf_region", "rf_src", "rf_reliable",
        # knee velocities
        "knee_vx_left", "knee_vx_right",
        # fast-path intent
        "fast_intent", "lead_side", "body_support",
        # scores per lane
        "score_L", "score_C", "score_R",
        # score-driven decision
        "side_candidate", "side_score", "side_body_support",
        "both_feet_region", "center_return_support", "center_foot_gate",
        "confirmation_hold",
        # guards / flags
        "suppressed", "ignored_trail_side", "ignored_spent_side",
        "center_settle_blocked",
        # outcome
        "prev_lane", "next_lane", "reason",
    ]

    def __init__(self, cfg: Config, calib: Calibration, log_path: Optional[str] = None):
        self.cfg = cfg
        self.calib = calib

        self.state = State.STABLE_CENTER
        self.current_lane = 0
        self.last_lane_change_t: float = -1e9
        self.intent_confidence: float = 0.0
        self._airborne_until = 0.0
        self._lead_lock_side: Optional[str] = None
        self._lead_lock_direction = 0
        self._lead_lock_until = -1e9
        self._display_lead_side: Optional[str] = None
        self._display_lead_until = -1e9
        self._trail_ignore_side: Optional[str] = None
        self._trail_ignore_direction = 0
        self._trail_ignore_until = -1e9
        self._spent_lead_side: Optional[str] = None
        self._spent_lead_direction = 0
        self._spent_lead_until = -1e9
        self._center_settle_direction = 0
        self._center_settle_until = -1e9
        self._confirm_lane: Optional[int] = None
        self._confirm_until = -1e9

        self._csv_fh = None
        self._csv_writer = None
        if log_path:
            self._csv_fh = open(log_path, "w", newline="", encoding="utf-8")
            self._csv_writer = csv.DictWriter(self._csv_fh, fieldnames=self._CSV_FIELDS)
            self._csv_writer.writeheader()

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
        # Also ignore side movement while a vertical jump/crouch gesture is underway.
        return (now < self._airborne_until or ducking
                or vertical_gesture_active(cfg, self.calib, f))

    def _region(self, x: float) -> int:
        b0, b1 = self.calib.boundaries
        if x < b0:
            return -1
        if x > b1:
            return 1
        return 0

    def _velocity_side(self, vx: float) -> int:
        deadzone = max(0.06, 0.35 * self.calib.vel_threshold)
        if abs(vx) <= deadzone:
            return 0
        return 1 if vx > 0 else -1

    def _foot_prediction(self, foot: FootFeature) -> float:
        return max(0.0, min(1.0, foot.x + foot.vx * self.cfg.hybrid_foot_prediction_sec))

    def _foot_active(self, foot: FootFeature) -> bool:
        return foot.source != "none" and foot.conf >= self.cfg.landmark_conf_min * 0.75

    def _preferred_lead_side(self, direction: int) -> Optional[str]:
        if direction > 0:
            return "right"
        if direction < 0:
            return "left"
        return None

    def _com_direction(self, f: Features, body_x: float) -> int:
        velocity_threshold = max(0.035, self.cfg.hybrid_com_velocity_ratio * self.cfg.hip_vel_threshold)
        if f.hip_vx >= velocity_threshold:
            return 1
        if f.hip_vx <= -velocity_threshold:
            return -1

        center = self.calib.lane_centers[self.current_lane]
        shift_threshold = max(0.012, self.cfg.hybrid_com_shift_ratio * self.calib.step_distance)
        shift = body_x - center
        if shift >= shift_threshold:
            return 1
        if shift <= -shift_threshold:
            return -1
        return 0

    def _ankle_motion_allowed(self, side: str, direction: int, com_direction: int) -> bool:
        return (
            direction != 0 and
            direction == com_direction and
            side == self._preferred_lead_side(com_direction)
        )

    def _movement_lead(self, f: Features, direction: int, now: float,
                       allow_guarded: bool = False):
        preferred = self._preferred_lead_side(direction)
        if preferred is None:
            return None, None

        preferred_foot = getattr(f, preferred)
        if self._foot_active(preferred_foot):
            if allow_guarded or not (
                self._trail_velocity_ignored(preferred, preferred_foot, now) or
                self._spent_lead_velocity_ignored(preferred, preferred_foot, now)
            ):
                return preferred, preferred_foot
        return None, None

    def _fast_intent(self, f: Features, left_region: int, right_region: int,
                     com_direction: int, now: float):
        candidates = []
        for side, foot, region in (
            ("left", f.left, left_region),
            ("right", f.right, right_region),
        ):
            if not self._foot_active(foot):
                continue
            if self._trail_velocity_ignored(side, foot, now):
                continue
            if self._spent_lead_velocity_ignored(side, foot, now):
                continue
            velocity_side = self._velocity_side(foot.vx)
            if not self._ankle_motion_allowed(side, velocity_side, com_direction):
                continue
            if velocity_side != 0 and region == velocity_side:
                candidates.append((abs(foot.vx), velocity_side, side, foot))

        if self._lead_lock_active(now):
            locked_foot = getattr(f, self._lead_lock_side)
            if self._lead_lock_side != self._preferred_lead_side(com_direction):
                self._clear_lead_lock()
            elif com_direction != self._lead_lock_direction:
                self._clear_lead_lock()
            elif self._velocity_side(locked_foot.vx) == -self._lead_lock_direction:
                self._clear_lead_lock()
            else:
                for _, direction, side, foot in candidates:
                    if side == self._lead_lock_side and direction == self._lead_lock_direction:
                        return direction, side, foot
                return 0, self._lead_lock_side, getattr(f, self._lead_lock_side)

        if not candidates:
            self._clear_lead_lock()
            return 0, None, None

        _, direction, side, foot = max(candidates, key=lambda item: item[0])
        self._set_lead_lock(side, direction, now)
        return direction, side, foot

    def _lead_lock_active(self, now: float) -> bool:
        return (
            self._lead_lock_side is not None and
            self._lead_lock_direction != 0 and
            now <= self._lead_lock_until
        )

    def _set_lead_lock(self, side: str, direction: int, now: float) -> None:
        self._lead_lock_side = side
        self._lead_lock_direction = direction
        self._lead_lock_until = now + self.cfg.hybrid_lead_lock_sec

    def _clear_lead_lock(self) -> None:
        self._lead_lock_side = None
        self._lead_lock_direction = 0
        self._lead_lock_until = -1e9

    def _set_display_lead(self, side: Optional[str], now: float) -> None:
        if side not in ("left", "right"):
            return
        self._display_lead_side = side
        self._display_lead_until = now + self.cfg.hybrid_lead_display_sec

    def _display_lead(self, f: Features, now: float, com_direction: int = 0):
        if self._display_lead_side is None or now > self._display_lead_until:
            self._display_lead_side = None
            self._display_lead_until = -1e9
            return None, None
        if (
            com_direction != 0 and
            self._display_lead_side != self._preferred_lead_side(com_direction)
        ):
            self._display_lead_side = None
            self._display_lead_until = -1e9
            return None, None
        foot = getattr(f, self._display_lead_side)
        if not self._foot_active(foot):
            return None, None
        return self._display_lead_side, foot

    def _set_trail_ignore(self, lead_side: Optional[str], direction: int, now: float) -> None:
        if lead_side not in ("left", "right") or direction == 0:
            return
        self._trail_ignore_side = "right" if lead_side == "left" else "left"
        self._trail_ignore_direction = direction
        self._trail_ignore_until = now + self.cfg.hybrid_trail_ignore_sec

    def _clear_trail_ignore(self) -> None:
        self._trail_ignore_side = None
        self._trail_ignore_direction = 0
        self._trail_ignore_until = -1e9

    def _trail_ignore_active(self, now: float) -> bool:
        if self._trail_ignore_side is None or self._trail_ignore_direction == 0:
            return False
        if now <= self._trail_ignore_until:
            return True
        self._clear_trail_ignore()
        return False

    def _trail_velocity_ignored(self, side: str, foot: FootFeature, now: float) -> bool:
        return (
            self._trail_ignore_active(now) and
            side == self._trail_ignore_side and
            self._foot_active(foot) and
            self._velocity_side(foot.vx) == self._trail_ignore_direction
        )

    def _set_spent_lead(self, lead_side: Optional[str], direction: int, now: float) -> None:
        if lead_side not in ("left", "right") or direction == 0:
            return
        self._spent_lead_side = lead_side
        self._spent_lead_direction = direction
        self._spent_lead_until = now + self.cfg.hybrid_spent_lead_sec

    def _clear_spent_lead(self) -> None:
        self._spent_lead_side = None
        self._spent_lead_direction = 0
        self._spent_lead_until = -1e9

    def _spent_lead_active(self, now: float) -> bool:
        if self._spent_lead_side is None or self._spent_lead_direction == 0:
            return False
        if now <= self._spent_lead_until:
            return True
        self._clear_spent_lead()
        return False

    def _spent_lead_velocity_ignored(self, side: str, foot: FootFeature, now: float) -> bool:
        if not self._spent_lead_active(now) or side != self._spent_lead_side:
            return False
        if not self._foot_active(foot):
            return False
        velocity_side = self._velocity_side(foot.vx)
        if velocity_side == -self._spent_lead_direction:
            self._clear_spent_lead()
            return False
        neutral = self.calib.neutral_ankle.get(side, self.calib.neutral_hip)
        near_neutral = abs(foot.x - neutral) <= max(0.04, 0.24 * self.calib.step_distance)
        settled = abs(foot.vx) <= max(0.055, 0.28 * self.calib.vel_threshold)
        if near_neutral and settled:
            self._clear_spent_lead()
            return False
        return velocity_side == self._spent_lead_direction

    def _set_center_settle(self, old_lane: int, new_lane: int, now: float) -> None:
        if old_lane == 0 or new_lane != 0:
            return
        self._center_settle_direction = -old_lane
        self._center_settle_until = now + self.cfg.hybrid_center_settle_sec

    def _clear_center_settle(self) -> None:
        self._center_settle_direction = 0
        self._center_settle_until = -1e9

    def _center_settle_active(self, now: float) -> bool:
        if self._center_settle_direction == 0:
            return False
        if now <= self._center_settle_until:
            return True
        self._clear_center_settle()
        return False

    def _center_settle_blocks(self, lane: int, now: float) -> bool:
        return (
            self.current_lane == 0 and
            lane == self._center_settle_direction and
            self._center_settle_active(now)
        )

    def _body_supports_intent(self, f: Features, body_x: float, direction: int) -> bool:
        if direction == 0:
            return False
        current_center = self.calib.lane_centers[self.current_lane]
        body_shift = (body_x - current_center) * direction
        min_shift = max(0.015, self.cfg.hybrid_body_shift_ratio * self.calib.step_distance)
        min_velocity = max(0.04, self.cfg.hybrid_body_velocity_ratio * self.cfg.hip_vel_threshold)
        return (
            body_shift >= min_shift or
            f.hip_vx * direction >= min_velocity or
            self._region(body_x) == direction
        )

    def _body_supports_center_return(self, f: Features, body_x: float) -> bool:
        if self.current_lane == 0:
            return False
        direction = -self.current_lane
        # If body is actively moving away from center, don't claim it supports returning.
        # This prevents a fast-foot early trigger (before body crosses the boundary) from
        # being immediately undone by center_return seeing "body/feet still in center".
        if f.hip_vx * direction < -max(0.03, 0.15 * self.cfg.hip_vel_threshold):
            return False
        current_center = self.calib.lane_centers[self.current_lane]
        body_shift = (body_x - current_center) * direction
        min_shift = max(0.055, self.cfg.hybrid_center_return_shift_ratio * self.calib.step_distance)
        min_velocity = max(0.05, self.cfg.hybrid_center_return_velocity_ratio * self.cfg.hip_vel_threshold)
        return (
            body_shift >= min_shift or
            f.hip_vx * direction >= min_velocity or
            self._region(body_x) == 0
        )

    def _feet_support_center_return(self, f: Features, left_region: int,
                                    right_region: int, direction: int,
                                    com_direction: int) -> bool:
        if self.current_lane == 0 or direction == 0:
            return True

        preferred = self._preferred_lead_side(direction)
        if preferred is None:
            return False

        foot = getattr(f, preferred)
        if not self._foot_active(foot):
            return True

        preferred_region = left_region if preferred == "left" else right_region
        if preferred_region == 0:
            # Foot is already at center, but only trust that if the body isn't
            # actively moving away from center (which would mean mid-outward-step).
            if com_direction != 0 and com_direction != direction:
                return False
            return True

        if com_direction != direction:
            return False

        min_velocity = max(0.055, 0.28 * self.calib.vel_threshold)
        return foot.vx * direction >= min_velocity

    def _body_confirms_lane(self, body_x: float, lane: int) -> bool:
        return (
            self._region(body_x) == lane or
            abs(body_x - self.calib.lane_centers[lane]) <= self.cfg.confirm_radius
        )

    def _start_confirmation_hold(self, lane: int, body_x: float, now: float) -> None:
        if self._body_confirms_lane(body_x, lane):
            self._confirm_lane = None
            self._confirm_until = -1e9
            return
        self._confirm_lane = lane
        self._confirm_until = now + self.cfg.hybrid_confirmation_hold_sec

    def _confirmation_hold_active(self, body_x: float, now: float) -> bool:
        if self._confirm_lane is None:
            return False
        if self._body_confirms_lane(body_x, self._confirm_lane):
            self._confirm_lane = None
            self._confirm_until = -1e9
            return False
        if now <= self._confirm_until:
            return True
        self._confirm_lane = None
        self._confirm_until = -1e9
        return False

    def update(self, f: Features, now: float) -> Decision:
        cfg = self.cfg
        suppressed = self._suppressed(f, now)
        prev_lane = self.current_lane

        body_x = self._body_x(f)
        body_region = self._region(body_x)
        com_direction = self._com_direction(f, body_x)
        left_pred = self._foot_prediction(f.left)
        right_pred = self._foot_prediction(f.right)
        left_region = self._region(left_pred) if self._foot_active(f.left) else 0
        right_region = self._region(right_pred) if self._foot_active(f.right) else 0
        left_com_ignored = com_direction > 0 and self._foot_active(f.left)
        right_com_ignored = com_direction < 0 and self._foot_active(f.right)
        left_used = self._foot_active(f.left) and not left_com_ignored
        right_used = self._foot_active(f.right) and not right_com_ignored
        left_scored_region = left_region if left_used else None
        right_scored_region = right_region if right_used else None
        confirmation_hold = self._confirmation_hold_active(body_x, now)
        if confirmation_hold:
            ignored_trail_side = None
            if self._trail_velocity_ignored("left", f.left, now):
                ignored_trail_side = "left"
            elif self._trail_velocity_ignored("right", f.right, now):
                ignored_trail_side = "right"
            ignored_spent_side = None
            if self._spent_lead_velocity_ignored("left", f.left, now):
                ignored_spent_side = "left"
            elif self._spent_lead_velocity_ignored("right", f.right, now):
                ignored_spent_side = "right"
            trail_note = f"/trail-ignore={ignored_trail_side}" if ignored_trail_side else ""
            spent_note = f"/spent-lead={ignored_spent_side}" if ignored_spent_side else ""
            com_note = f"/com={com_direction:+d}" if com_direction != 0 else "/com=0"
            display_lead_side, display_lead_foot = self._display_lead(f, now, com_direction)
            self.state = _LANE_TO_STABLE[self.current_lane]
            self.intent_confidence = max(self.intent_confidence, 0.5)
            return Decision(
                command=None,
                state=self.state,
                current_lane=self.current_lane,
                leading_foot=display_lead_side,
                intent_confidence=self.intent_confidence,
                suppressed=suppressed,
                reason=f"hold-confirm L{left_region:+d}/R{right_region:+d}/B{body_region:+d}/body=--"
                       f"{com_note}{trail_note}{spent_note}",
                lead_vx=abs(display_lead_foot.vx) if display_lead_foot is not None else 0.0,
                lead_disp=0.0,
                vel_thr=max(0.06, 0.35 * self.calib.vel_threshold),
                disp_thr=self.calib.disp_threshold,
                controller="hybrid",
                body_x=body_x,
            )
        fast_intent, lead_side, lead_foot = self._fast_intent(
            f,
            left_region if left_used else 0,
            right_region if right_used else 0,
            com_direction,
            now,
        )
        body_support = self._body_supports_intent(f, body_x, fast_intent)
        ignored_trail_side = None
        if self._trail_velocity_ignored("left", f.left, now):
            ignored_trail_side = "left"
        elif self._trail_velocity_ignored("right", f.right, now):
            ignored_trail_side = "right"
        ignored_spent_side = None
        if self._spent_lead_velocity_ignored("left", f.left, now):
            ignored_spent_side = "left"
        elif self._spent_lead_velocity_ignored("right", f.right, now):
            ignored_spent_side = "right"
        both_feet_region = (
            left_scored_region
            if (
                left_scored_region is not None and
                right_scored_region is not None and
                left_scored_region == right_scored_region
            )
            else 0
        )
        left_raw_velocity_side = self._velocity_side(f.left.vx)
        right_raw_velocity_side = self._velocity_side(f.right.vx)
        left_com_gated = (
            left_raw_velocity_side != 0 and
            not self._ankle_motion_allowed("left", left_raw_velocity_side, com_direction)
        )
        right_com_gated = (
            right_raw_velocity_side != 0 and
            not self._ankle_motion_allowed("right", right_raw_velocity_side, com_direction)
        )
        left_velocity_side = (
            0 if (
                left_com_ignored or
                ignored_trail_side == "left" or
                ignored_spent_side == "left"
            )
            else (0 if left_com_gated else left_raw_velocity_side)
        )
        right_velocity_side = (
            0 if (
                right_com_ignored or
                ignored_trail_side == "right" or
                ignored_spent_side == "right"
            )
            else (0 if right_com_gated else right_raw_velocity_side)
        )
        center_direction = -self.current_lane if self.current_lane != 0 else 0
        left_actual_region = self._region(f.left.x) if left_used else 0
        right_actual_region = self._region(f.right.x) if right_used else 0
        center_foot_support = self._feet_support_center_return(
            f, left_actual_region, right_actual_region, center_direction, com_direction)
        center_foot_gate = self.current_lane != 0 and body_region == 0 and not center_foot_support
        com_leg_gated = (
            ("L" if left_com_gated or left_com_ignored else "") +
            ("R" if right_com_gated or right_com_ignored else "")
        )

        scores = {-1: 0.0, 0: 0.0, 1: 0.0}
        scores[body_region] += cfg.hybrid_body_score

        if left_scored_region is not None and right_scored_region is not None and both_feet_region != 0:
            scores[both_feet_region] += cfg.hybrid_both_feet_score
        elif left_scored_region == 0 and right_scored_region == 0:
            scores[0] += cfg.hybrid_both_feet_score
        else:
            if left_scored_region is not None:
                if left_scored_region != 0:
                    scores[left_scored_region] += cfg.hybrid_one_foot_score
                else:
                    scores[0] += cfg.hybrid_one_foot_score * 0.55
            if right_scored_region is not None:
                if right_scored_region != 0:
                    scores[right_scored_region] += cfg.hybrid_one_foot_score
                else:
                    scores[0] += cfg.hybrid_one_foot_score * 0.55

        if fast_intent != 0 and body_support:
            scores[fast_intent] += cfg.hybrid_fast_foot_score
        if left_velocity_side != 0 and left_scored_region == left_velocity_side:
            scores[left_velocity_side] += cfg.hybrid_velocity_score
        if right_velocity_side != 0 and right_scored_region == right_velocity_side:
            scores[right_velocity_side] += cfg.hybrid_velocity_score
        if self.current_lane != 0:
            scores[self.current_lane] += cfg.hybrid_hold_score

        if (
            self.current_lane != 0 and
            body_region == 0 and
            center_foot_support and
            (fast_intent == -self.current_lane or both_feet_region == 0)
        ):
            scores[0] += cfg.hybrid_fast_foot_score

        side_candidate = -1 if scores[-1] > scores[1] else 1
        side_score = scores[side_candidate]
        side_body_support = self._body_supports_intent(f, body_x, side_candidate)
        center_return_support = (
            self._body_supports_center_return(f, body_x) and center_foot_support
        )
        next_lane = self.current_lane
        reason = "hold"
        resolved_lead_side = None
        resolved_lead_foot = None

        if suppressed:
            reason = "suppressed"
        elif center_return_support:
            next_lane = 0
            reason = "return-center"
        elif fast_intent != 0 and not body_support:
            reason = f"body-gate {fast_intent:+d}"
            if not self._lead_lock_active(now):
                self._clear_lead_lock()
        elif fast_intent != 0 and side_score >= cfg.hybrid_enter_score:
            next_lane = fast_intent
            reason = f"fast-foot {fast_intent:+d}"
        elif side_score >= cfg.hybrid_enter_score and side_score > scores[0] + 0.12 and side_body_support:
            next_lane = side_candidate
            reason = "confirmed" if body_region == side_candidate else "feet"
        elif side_score >= cfg.hybrid_enter_score and side_score > scores[0] + 0.12:
            reason = f"body-gate {side_candidate:+d}"
        elif scores[0] >= cfg.hybrid_center_score and scores[0] >= side_score:
            if self.current_lane == 0 or center_foot_support:
                next_lane = 0
                reason = "center-com" if body_region == 0 else "center-feet"
            else:
                reason = "center-foot-gate"

        if (
            ignored_trail_side is not None and
            next_lane != self.current_lane and
            (next_lane - self.current_lane) * self._trail_ignore_direction > 0 and
            body_region != next_lane and
            not (fast_intent != 0 and lead_side != ignored_trail_side)
        ):
            next_lane = self.current_lane
            reason = f"trail-ignore {ignored_trail_side}"

        if (
            ignored_spent_side is not None and
            next_lane != self.current_lane and
            (next_lane - self.current_lane) * self._spent_lead_direction > 0 and
            body_region != next_lane
        ):
            next_lane = self.current_lane
            reason = f"spent-lead {ignored_spent_side}"

        center_settle_blocked = False
        if next_lane != self.current_lane and self._center_settle_blocks(next_lane, now):
            center_settle_blocked = True
            next_lane = self.current_lane
            reason = f"center-settle {self._center_settle_direction:+d}"

        if next_lane != self.current_lane:
            change_dir = 1 if next_lane > self.current_lane else -1
            resolved_lead_side, resolved_lead_foot = self._movement_lead(
                f, change_dir, now, allow_guarded=True)
            if resolved_lead_side is None and fast_intent == change_dir and body_support:
                resolved_lead_side, resolved_lead_foot = lead_side, lead_foot

        command: Optional[str] = None
        if not suppressed and next_lane != self.current_lane:
            if now - self.last_lane_change_t >= cfg.rearm_time:
                old_lane = self.current_lane
                change_dir = 1 if next_lane > old_lane else -1
                self.current_lane = next_lane
                self.last_lane_change_t = now
                self._start_confirmation_hold(next_lane, body_x, now)
                self._set_center_settle(old_lane, next_lane, now)
                self._set_trail_ignore(resolved_lead_side, change_dir, now)
                self._set_spent_lead(resolved_lead_side, change_dir, now)
                self._set_display_lead(resolved_lead_side, now)
                reason += f" -> lane {next_lane:+d}"
                self._clear_lead_lock()
            else:
                reason = "rearm"
        elif body_region == self.current_lane and fast_intent == 0:
            self._clear_lead_lock()

        self.state = _LANE_TO_STABLE[self.current_lane]
        best_score = max(scores.values())
        self.intent_confidence = _clamp01(best_score / 3.0)

        display_lead_side = resolved_lead_side
        display_lead_foot = resolved_lead_foot
        if display_lead_side is None and fast_intent != 0 and body_support:
            display_lead_side = lead_side
            display_lead_foot = lead_foot
        if display_lead_side is None:
            display_lead_side, display_lead_foot = self._display_lead(f, now, com_direction)
        lead_vx = abs(display_lead_foot.vx) if display_lead_foot is not None else 0.0
        lead_disp = 0.0
        if display_lead_foot is not None and display_lead_side is not None:
            lead_disp = abs(self._foot_prediction(display_lead_foot) - self.calib.neutral_ankle[display_lead_side])

        if self._csv_writer is not None:
            self._csv_writer.writerow({
                "t": f"{now:.4f}",
                "body_x": f"{body_x:.4f}",
                "body_region": body_region,
                "com_direction": com_direction,
                "hip_vx": f"{f.hip_vx:.4f}",
                "hip_x": f"{f.hip_x:.4f}",
                "hip_conf": f"{f.hip_conf:.3f}",
                "lf_x": f"{f.left.x:.4f}",
                "lf_vx": f"{f.left.vx:.4f}",
                "lf_vy": f"{f.left.vy:.4f}",
                "lf_pred": f"{left_pred:.4f}",
                "lf_region": left_region,
                "lf_src": f.left.source,
                "lf_reliable": int(f.left.reliable),
                "rf_x": f"{f.right.x:.4f}",
                "rf_vx": f"{f.right.vx:.4f}",
                "rf_vy": f"{f.right.vy:.4f}",
                "rf_pred": f"{right_pred:.4f}",
                "rf_region": right_region,
                "rf_src": f.right.source,
                "rf_reliable": int(f.right.reliable),
                "knee_vx_left": f"{f.knee_vx.get('left', 0.0):.4f}",
                "knee_vx_right": f"{f.knee_vx.get('right', 0.0):.4f}",
                "fast_intent": fast_intent,
                "lead_side": lead_side or "",
                "body_support": int(body_support),
                "score_L": f"{scores[-1]:.3f}",
                "score_C": f"{scores[0]:.3f}",
                "score_R": f"{scores[1]:.3f}",
                "side_candidate": side_candidate,
                "side_score": f"{side_score:.3f}",
                "side_body_support": int(side_body_support),
                "both_feet_region": both_feet_region,
                "center_return_support": int(center_return_support),
                "center_foot_gate": int(center_foot_gate),
                "confirmation_hold": int(confirmation_hold),
                "suppressed": int(suppressed),
                "ignored_trail_side": ignored_trail_side or "",
                "ignored_spent_side": ignored_spent_side or "",
                "center_settle_blocked": int(center_settle_blocked),
                "prev_lane": prev_lane,
                "next_lane": self.current_lane,
                "reason": reason,
            })
            self._csv_fh.flush()

        return Decision(
            command=command,
            state=self.state,
            current_lane=self.current_lane,
            leading_foot=display_lead_side,
            intent_confidence=self.intent_confidence,
            suppressed=suppressed,
            reason=f"{reason} L{left_region:+d}/R{right_region:+d}/B{body_region:+d}"
                   f"/body={'OK' if (body_support or side_body_support or center_return_support) else '--'}"
                   f"/com={com_direction:+d}"
                   f"{f'/com-leg-gate={com_leg_gated}' if com_leg_gated else ''}"
                   f"{'/center-foot-gate' if center_foot_gate else ''}"
                   f"{f'/center-settle={self._center_settle_direction:+d}' if center_settle_blocked else ''}"
                   f"{f'/trail-ignore={ignored_trail_side}' if ignored_trail_side else ''}"
                   f"{f'/spent-lead={ignored_spent_side}' if ignored_spent_side else ''}",
            lead_vx=lead_vx,
            lead_disp=lead_disp,
            vel_thr=max(0.06, 0.35 * self.calib.vel_threshold),
            disp_thr=self.calib.disp_threshold,
            controller="hybrid",
            body_x=body_x,
        )


class PositionLaneController:
    """Robust, position-based lane controller.

    Where the two-stage ``LaneStateMachine`` infers an *early* lane-change intent
    from foot velocity (low latency, but direction-ambiguous and fragile: the lead
    foot flips mid-step and the per-foot displacement gate is asymmetric), this
    controller maps the player's **lateral body position** straight onto the
    nearest calibrated lane. It updates one lane per calibrated boundary crossed,
    with a hysteresis margin so standing on a line doesn't
    chatter. Symmetric by construction and easy to reason about: where your hips
    are *is* where the avatar is.

    Trade-off vs. feet mode: the lane changes when your torso actually crosses
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
        # Also ignore side movement while a vertical jump/crouch gesture is underway.
        return (now < self._airborne_until or ducking
                or vertical_gesture_active(cfg, self.calib, f))

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
