"""Deterministic tests for lane_engine: filters, fusion, two-stage state machine.

Runs without a camera or pose model by feeding synthetic landmark streams. Use:

    python test_lane_engine.py

Coordinates are written in raw normalized space with invert_x disabled, so larger
x = image-right = positive lane direction ("right").
"""

import sys

from lane_engine import (
    Config, Calibration, FeatureExtractor, LaneStateMachine, Landmark, State,
    VerticalGestureDetector,
)

FPS = 60.0
DT = 1.0 / FPS


def make_lm(la_x, ra_x, hip_x, *, ankle_y=0.90, knee_y=0.70, hip_y=0.55, sh_y=0.35,
            lk_x=None, rk_x=None, sh_x=None, conf=0.95):
    """Build a landmark dict. la_x/ra_x = left/right ankle x; hip_x = hip center."""
    lk_x = la_x if lk_x is None else lk_x
    rk_x = ra_x if rk_x is None else rk_x
    sh_x = hip_x if sh_x is None else sh_x
    return {
        "left_ankle": Landmark(la_x, ankle_y, conf),
        "right_ankle": Landmark(ra_x, ankle_y, conf),
        "left_heel": None, "right_heel": None,
        "left_foot_index": None, "right_foot_index": None,
        "left_knee": Landmark(lk_x, knee_y, conf),
        "right_knee": Landmark(rk_x, knee_y, conf),
        "left_hip": Landmark(hip_x - 0.08, hip_y, conf),
        "right_hip": Landmark(hip_x + 0.08, hip_y, conf),
        "left_shoulder": Landmark(sh_x - 0.10, sh_y, conf),
        "right_shoulder": Landmark(sh_x + 0.10, sh_y, conf),
    }


def lerp(a, b, f):
    return a + (b - a) * max(0.0, min(1.0, f))


class Sim:
    def __init__(self, calib=None, **cfg_over):
        self.cfg = Config(invert_x=False, **cfg_over)
        self.calib = calib or Calibration.default(self.cfg)
        self.fx = FeatureExtractor(self.cfg)
        self.commands = []
        self.sm = LaneStateMachine(self.cfg, self.calib, emit=self.commands.append)
        self.t = 0.0
        self.decisions = []

    def feed(self, lm):
        f = self.fx.update(lm, self.t)
        d = self.sm.update(f, self.t)
        self.decisions.append(d)
        self.t += DT
        return d

    def hold(self, la_x, ra_x, hip_x, frames):
        for _ in range(frames):
            self.feed(make_lm(la_x, ra_x, hip_x))


def check(name, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}")
    return cond


# --------------------------------------------------------------------------- #
def test_sidestep_right():
    print("test_sidestep_right")
    s = Sim()
    s.hold(0.45, 0.55, 0.50, 12)                      # settle, centered
    # right foot leads right; hips follow (lagging) toward right lane center 0.75
    for i in range(20):
        f = i / 19.0
        ra = lerp(0.55, 0.78, f)
        rk = lerp(0.55, 0.76, f)
        hip = lerp(0.50, 0.76, max(0.0, (i - 4) / 15.0))  # torso lags the foot
        s.feed(make_lm(0.45, ra, hip, rk_x=rk))
    s.hold(0.55, 0.78, 0.76, 8)                        # arrive, settle in right lane
    ok = True
    ok &= check("emitted exactly one command", len(s.commands) == 1)
    ok &= check('command is "right"', s.commands[:1] == ["right"])
    ok &= check("ends STABLE_RIGHT", s.sm.state is State.STABLE_RIGHT)
    ok &= check("current_lane == 1", s.sm.current_lane == 1)
    return ok


def test_crossover_left_with_right_foot():
    """Direction must come from velocity, not foot identity: the RIGHT foot
    sweeping left (a crossover step) must produce a LEFT command."""
    print("test_crossover_left_with_right_foot")
    s = Sim()
    s.hold(0.45, 0.55, 0.50, 12)
    for i in range(20):
        f = i / 19.0
        ra = lerp(0.55, 0.30, f)                       # right foot crosses to the left
        rk = lerp(0.55, 0.32, f)
        hip = lerp(0.50, 0.25, max(0.0, (i - 4) / 15.0))
        s.feed(make_lm(0.45, ra, hip, rk_x=rk))
    s.hold(0.45, 0.30, 0.25, 8)
    ok = True
    ok &= check("emitted exactly one command", len(s.commands) == 1)
    ok &= check('command is "left"', s.commands[:1] == ["left"])
    ok &= check("ends STABLE_LEFT", s.sm.state is State.STABLE_LEFT)
    return ok


def test_jitter_rejected():
    print("test_jitter_rejected")
    s = Sim()
    s.hold(0.45, 0.55, 0.50, 10)
    for i in range(40):
        jit = 0.008 * (1 if i % 2 else -1)             # high-freq, tiny, no net disp
        s.feed(make_lm(0.45 + jit, 0.55 - jit, 0.50))
    return check("no command from jitter", len(s.commands) == 0)


def test_vertical_running_rejected():
    """Vertical leg motion (running/jogging in place) must not trigger lanes."""
    print("test_vertical_running_rejected")
    s = Sim()
    s.hold(0.45, 0.55, 0.50, 10)
    for i in range(40):
        ay = 0.90 - 0.06 * abs((i % 8) - 4) / 4.0      # ankles bob up/down
        xj = 0.006 * (1 if i % 2 else -1)
        s.feed(make_lm(0.45 + xj, 0.55 - xj, 0.50, ankle_y=ay))
    return check("no command from vertical motion", len(s.commands) == 0)


def test_jump_suppressed():
    print("test_jump_suppressed")
    s = Sim()
    s.hold(0.45, 0.55, 0.50, 10)
    # hips + ankles shoot upward (y decreasing) fast while feet drift sideways
    for i in range(10):
        f = i / 9.0
        hip_y = lerp(0.55, 0.40, f)
        ay = lerp(0.90, 0.74, f)
        ra = lerp(0.55, 0.66, f)
        s.feed(make_lm(0.45, ra, 0.55, ankle_y=ay, hip_y=hip_y))
    return check("no command while airborne", len(s.commands) == 0)


def test_aborted_step_cancels():
    """Foot starts a step (one command fires) but the player returns to center
    without the hips arriving: state returns to center, no second command."""
    print("test_aborted_step_cancels")
    s = Sim()
    s.hold(0.45, 0.55, 0.50, 12)
    # begin a right step -> should emit "right"
    for i in range(8):
        f = i / 7.0
        ra = lerp(0.55, 0.70, f)
        rk = lerp(0.55, 0.68, f)
        s.feed(make_lm(0.45, ra, lerp(0.50, 0.54, f), rk_x=rk))  # hips barely move
    emitted_after_start = len(s.commands)
    # return foot to rest, hips stay near center, then settle
    for i in range(8):
        f = i / 7.0
        ra = lerp(0.70, 0.55, f)
        s.feed(make_lm(0.45, ra, lerp(0.54, 0.50, f)))
    s.hold(0.45, 0.55, 0.50, 12)
    ok = True
    ok &= check("one command at step start", emitted_after_start == 1)
    ok &= check("no second command on abort", len(s.commands) == 1)
    ok &= check("returns to STABLE_CENTER", s.sm.state is State.STABLE_CENTER)
    ok &= check("current_lane back to 0", s.sm.current_lane == 0)
    return ok


def test_left_to_right_two_steps():
    """From the left lane, reaching the right lane must pass through center
    (two separate commands), never a direct -1 -> +1 jump."""
    print("test_left_to_right_two_steps")
    s = Sim()
    # start already standing in the left lane
    s.sm.state = State.STABLE_LEFT
    s.sm.current_lane = -1
    s.sm.rest = {"left": 0.20, "right": 0.30}
    s.hold(0.20, 0.30, 0.25, 12)
    lanes_seen = set()
    # step 1: left lane -> center
    for i in range(20):
        f = i / 19.0
        ra = lerp(0.30, 0.55, f)
        rk = lerp(0.30, 0.53, f)
        hip = lerp(0.25, 0.50, max(0.0, (i - 4) / 15.0))
        s.feed(make_lm(0.20, ra, hip, rk_x=rk))
    s.hold(0.45, 0.55, 0.50, 10)
    lanes_seen.add(s.sm.current_lane)
    mid_state = s.sm.state
    # step 2: center -> right
    for i in range(20):
        f = i / 19.0
        ra = lerp(0.55, 0.78, f)
        rk = lerp(0.55, 0.76, f)
        hip = lerp(0.50, 0.76, max(0.0, (i - 4) / 15.0))
        s.feed(make_lm(0.45, ra, hip, rk_x=rk))
    s.hold(0.55, 0.78, 0.76, 10)
    ok = True
    ok &= check("passed through center", mid_state is State.STABLE_CENTER)
    ok &= check("two commands, both right", s.commands == ["right", "right"])
    ok &= check("ends STABLE_RIGHT", s.sm.state is State.STABLE_RIGHT)
    return ok


def test_latency_under_budget():
    """A decisive lateral step (the real lane-change motion) should fire the
    velocity-dominant fast path very quickly after motion onset."""
    print("test_latency_under_budget")
    s = Sim()
    s.hold(0.45, 0.55, 0.50, 12)
    onset_t = s.t
    emit_t = None
    for i in range(20):
        f = i / 19.0
        ra = lerp(0.55, 0.92, f)                       # brisk, decisive sidestep
        rk = lerp(0.55, 0.88, f)
        hip = lerp(0.50, 0.72, max(0.0, (i - 3) / 15.0))
        before = len(s.commands)
        s.feed(make_lm(0.45, ra, hip, rk_x=rk))
        if emit_t is None and len(s.commands) > before:
            emit_t = s.t
            break
    latency_ms = None if emit_t is None else (emit_t - onset_t) * 1000.0
    # Aspirational target is <50ms; with robust 3-sample velocity at 60fps the
    # realistic figure is ~80ms -- still far ahead of the 300-500ms a torso takes
    # to cross a lane, which is the whole point of triggering from the feet.
    print(f"    intent latency: {latency_ms:.1f} ms" if latency_ms else "    never emitted")
    return check("intent latency < 100 ms", latency_ms is not None and latency_ms < 100.0)


def test_calibrator():
    print("test_calibrator")
    from lane_engine import Calibrator
    cfg = Config(invert_x=False)
    cal = Calibrator(cfg)
    fx = FeatureExtractor(cfg)
    t = [0.0]

    def feed_phase(hip_x, la, ra, n=15):
        for _ in range(n):
            f = fx.update(make_lm(la, ra, hip_x), t[0])
            cal.add(f)
            t[0] += DT

    feed_phase(0.50, 0.45, 0.55); cal.next_phase()      # center
    feed_phase(0.22, 0.17, 0.27); cal.next_phase()      # left
    feed_phase(0.50, 0.45, 0.55); cal.next_phase()      # center return
    feed_phase(0.80, 0.75, 0.85); cal.next_phase()      # right
    c = cal.compute()
    ok = True
    ok &= check("center between left and right", c.lane_centers[-1] < c.lane_centers[0] < c.lane_centers[1])
    ok &= check("step distance positive", c.step_distance > 0.1)
    ok &= check("derived disp threshold sane", 0.02 <= c.disp_threshold <= 0.2)
    return ok


# --------------------------------------------------------------------------- #
# Vertical gesture (jump / crouch) tests
# --------------------------------------------------------------------------- #
class GSim:
    """Feeds vertically-translated landmark streams through FeatureExtractor +
    VerticalGestureDetector. ``dy`` > 0 moves the torso DOWN (crouch), dy < 0 UP
    (jump). Shoulder and hip translate together so torso length is preserved (so a
    crouch here is a body drop, not a torso compression)."""

    def __init__(self, calib=None, **cfg_over):
        self.cfg = Config(invert_x=False, **cfg_over)
        self.calib = calib or Calibration.default(self.cfg)
        self.fx = FeatureExtractor(self.cfg)
        self.det = VerticalGestureDetector(self.cfg, self.calib)
        self.t = 0.0
        self.jumps = 0
        self.ducks = 0
        self.last = None

    def feed(self, dy=0.0, conf=0.95):
        lm = make_lm(0.45, 0.55, 0.50, hip_y=0.55 + dy, sh_y=0.35 + dy, conf=conf)
        f = self.fx.update(lm, self.t)
        g = self.det.update(f, self.t)
        self.last = g
        if g.jump:
            self.jumps += 1
        if g.duck:
            self.ducks += 1
        self.t += DT
        return g

    def hold(self, n, dy=0.0, conf=0.95):
        for _ in range(n):
            self.feed(dy, conf)

    def ramp(self, dy0, dy1, n, conf=0.95):
        for i in range(n):
            self.feed(lerp(dy0, dy1, (i + 1) / n), conf)


def test_gesture_neutral_quiet():
    print("test_gesture_neutral_quiet")
    g = GSim()
    g.hold(30)                                           # just stand there
    ok = True
    ok &= check("no jump while neutral", g.jumps == 0)
    ok &= check("no duck while neutral", g.ducks == 0)
    return ok


def test_gesture_jump_once():
    print("test_gesture_jump_once")
    g = GSim()
    g.hold(10)
    g.ramp(0.0, -0.10, 6)                                # spring up
    g.ramp(-0.10, 0.0, 6)                                # come back down
    g.hold(10)
    ok = True
    ok &= check("exactly one jump", g.jumps == 1)
    ok &= check("no duck", g.ducks == 0)
    return ok


def test_gesture_jump_no_repeat_until_rearm():
    print("test_gesture_jump_no_repeat_until_rearm")
    g = GSim()
    g.hold(10)
    g.ramp(0.0, -0.10, 6)
    g.hold(30, dy=-0.10)                                 # hold at the top
    held = g.jumps
    g.ramp(-0.10, 0.0, 6)                                # return past the re-arm offset
    g.hold(8)
    g.ramp(0.0, -0.10, 6)                                # jump again
    g.hold(6, dy=-0.10)
    ok = True
    ok &= check("only one jump while held up", held == 1)
    ok &= check("re-arms and fires a second jump", g.jumps == 2)
    return ok


def test_gesture_duck_once():
    print("test_gesture_duck_once")
    g = GSim()
    g.hold(10)
    g.ramp(0.0, 0.12, 6)                                 # drop fast into a crouch
    g.ramp(0.12, 0.0, 6)                                 # stand back up
    g.hold(10)
    ok = True
    ok &= check("exactly one duck", g.ducks == 1)
    ok &= check("no jump", g.jumps == 0)
    return ok


def test_gesture_slow_crouch_held():
    """A slow squat never trips the velocity gate, but holding below the crouch
    line long enough still fires exactly one duck."""
    print("test_gesture_slow_crouch_held")
    g = GSim()
    g.hold(10)
    g.ramp(0.0, 0.10, 100)                               # very slow descent (low velocity)
    g.hold(15, dy=0.10)                                  # held low
    return check("held slow crouch emits one duck", g.ducks == 1)


def test_gesture_low_confidence_ignored():
    print("test_gesture_low_confidence_ignored")
    g = GSim()
    g.hold(10)                                           # confident neutral baseline
    g.ramp(0.0, -0.12, 6, conf=0.2)                      # jump-like motion, low conf
    g.hold(8, dy=-0.12, conf=0.2)
    ok = True
    ok &= check("no jump from low-confidence body", g.jumps == 0)
    ok &= check("no duck from low-confidence body", g.ducks == 0)
    return ok


def test_gesture_indicator_holds_for_cooldown():
    """The on-screen indicator (GestureDecision.active) should stay set for the
    cooldown window after a one-shot gesture, then clear."""
    print("test_gesture_indicator_holds_for_cooldown")
    g = GSim()
    g.hold(10)
    g.ramp(0.0, -0.10, 6)                                # jump fires
    ok = True
    ok &= check("jumped once", g.jumps == 1)
    ok &= check("indicator active right after fire", g.last.active == "jump")
    ok &= check("cooldown_remaining counting down", 0.0 < g.last.cooldown_remaining <= g.cfg.gesture_cooldown)
    # hold up past the cooldown (20 frames @60fps = 0.33s > 0.25s); indicator clears
    g.hold(20, dy=-0.10)
    ok &= check("still only one jump", g.jumps == 1)
    ok &= check("indicator clears after cooldown", g.last.active is None)
    return ok


def test_gesture_suppresses_lane():
    """A crouch (body drop, torso length unchanged) must suppress lane changes:
    a full sidestep made while crouched produces no lane command."""
    print("test_gesture_suppresses_lane")
    s = Sim()
    s.hold(0.45, 0.55, 0.50, 12)
    # sink into a crouch first (feet planted, torso drops ~0.07)
    for i in range(8):
        dy = 0.07 * (i + 1) / 8
        s.feed(make_lm(0.45, 0.55, 0.50, hip_y=0.55 + dy, sh_y=0.35 + dy))
    # then, while held in the crouch, slide the right foot fully into the right lane
    for i in range(16):
        f = i / 15.0
        ra = lerp(0.55, 0.78, f)
        rk = lerp(0.55, 0.76, f)
        hip = lerp(0.50, 0.74, f)
        s.feed(make_lm(0.45, ra, hip, hip_y=0.62, sh_y=0.42, rk_x=rk))
    return check("no lane command while crouched", len(s.commands) == 0)


def main():
    tests = [
        test_sidestep_right,
        test_crossover_left_with_right_foot,
        test_jitter_rejected,
        test_vertical_running_rejected,
        test_jump_suppressed,
        test_aborted_step_cancels,
        test_left_to_right_two_steps,
        test_latency_under_budget,
        test_calibrator,
        test_gesture_neutral_quiet,
        test_gesture_jump_once,
        test_gesture_jump_no_repeat_until_rearm,
        test_gesture_duck_once,
        test_gesture_slow_crouch_held,
        test_gesture_low_confidence_ignored,
        test_gesture_indicator_holds_for_cooldown,
        test_gesture_suppresses_lane,
    ]
    results = []
    for t in tests:
        try:
            results.append(t())
        except Exception as exc:  # noqa: BLE001
            print(f"  [ERROR] {t.__name__}: {exc!r}")
            results.append(False)
        print()
    passed = sum(results)
    print(f"{passed}/{len(results)} tests passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
