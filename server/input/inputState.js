import { config } from "../config.js";
import { now } from "../utils/timing.js";

/**
 * Authoritative input state for one session, reconstructed from the phone's
 * heartbeat stream (PLAN.md §3 / §7).
 *
 * The phone sends classified intents at its own cadence; this class folds them
 * into a single current state, applying timeouts so that the *absence* of
 * heartbeats is itself meaningful:
 *   - running.intensity snaps to 0 if no running_state for RUNNING_TIMEOUT_MS
 *   - lane is HELD (a lane is a position) but warns if stale > LANE_TIMEOUT_WARN_MS
 *   - plank.active forced false if no plank_state for PLANK_TIMEOUT_MS (safety:
 *     never leave the jetpack on if the phone died)
 *
 * Timeouts are evaluated lazily in compute(now), so they apply correctly between
 * messages without needing their own timers.
 */
export class InputState {
  constructor(logger) {
    this.log = logger;
    this.reset();
  }

  /** Reset to neutral. Used on init and when the phone detaches. */
  reset() {
    // lastSeen = -Infinity means "never seen" -> immediately treated as stale.
    this.running = { intensity: 0, lastSeen: -Infinity };
    this.lane = { value: 0, lastSeen: -Infinity };
    this.plank = { active: false, lastSeen: -Infinity };
    this._laneWarned = false;
  }

  // ---- heartbeat ingestion ----

  updateRunning(intensity, t = now()) {
    this.running.intensity = intensity;
    this.running.lastSeen = t;
  }

  updateLane(lane, t = now()) {
    this.lane.value = lane;
    this.lane.lastSeen = t;
    this._laneWarned = false; // fresh heartbeat clears the stale-warning latch
  }

  updatePlank(active, t = now()) {
    this.plank.active = active;
    this.plank.lastSeen = t;
  }

  // ---- reconstruction ----

  /**
   * Compute the current authoritative state. Pure read w.r.t. heartbeat values;
   * only side effect is a one-shot stale-lane warning.
   *
   * @param {number} [t] monotonic ms (defaults to now()).
   * @returns {{ running: boolean, intensity: number, lane: -1|0|1, plank: boolean, serverT: number }}
   */
  compute(t = now()) {
    const intensity = t - this.running.lastSeen > config.RUNNING_TIMEOUT_MS ? 0 : this.running.intensity;

    // Lane: hold last value; warn once per stale episode (don't spam at 30Hz).
    if (t - this.lane.lastSeen > config.LANE_TIMEOUT_WARN_MS && this.lane.lastSeen !== -Infinity) {
      if (!this._laneWarned) {
        this.log?.warn(
          { staleMs: Math.round(t - this.lane.lastSeen), lane: this.lane.value },
          "lane heartbeat stale (holding last value)"
        );
        this._laneWarned = true;
      }
    }
    const lane = this.lane.value;

    const plank = t - this.plank.lastSeen > config.PLANK_TIMEOUT_MS ? false : this.plank.active;

    return {
      running: intensity > 0,
      intensity,
      lane,
      plank,
      serverT: t,
    };
  }
}
