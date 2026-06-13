/**
 * Game — STUB. Owned by the game-logic teammate.
 *
 * This is the single seam where game logic plugs into the transport layer
 * (PLAN.md §12). The Session constructs one Game per session and calls
 * onInput() then tick() every server tick (30Hz), so the wiring is proven and
 * exercised today even though every method is a no-op.
 *
 * DO NOT implement obstacles, scoring, collision, lives, or any game rules here
 * in the transport phase. When the game team takes over, they fill these in and
 * emit game:* events via ctx.emit() — no transport changes should be required.
 */
export class Game {
  /**
   * @param {object} ctx
   * @param {string} ctx.sessionCode
   * @param {(type: string, data: object) => void} ctx.emit
   *   Routes a message to the display. Provided now for the future game:state /
   *   game:event / game:over events; unused by the stub.
   * @param {import('pino').Logger} ctx.logger
   */
  constructor(ctx) {
    this.ctx = ctx;
  }

  /**
   * Called once per tick with the freshly reconstructed InputState, before tick().
   * Future: feed input into the simulation (move runner between lanes, set
   * jetpack from plank, apply running intensity to speed, queue jump/duck).
   *
   * @param {{ running: boolean, intensity: number, lane: -1|0|1, plank: boolean, serverT: number }} state
   */
  onInput(state) {
    // TODO(game-team): consume input. No-op in the transport phase.
  }

  /**
   * Called once per server tick (30Hz) after onInput().
   * Future: advance the simulation, detect collisions, update score, and emit
   *   this.ctx.emit("game:state" | "game:event" | "game:over", payload).
   */
  tick() {
    // TODO(game-team): advance simulation + emit game:* events. No-op today.
  }

  /**
   * Future: return a snapshot of the game world (for /debug or late-join).
   * @returns {object|null}
   */
  getState() {
    // TODO(game-team): return world snapshot. Null while stubbed.
    return null;
  }

  /**
   * One-shot relay of a discrete input event (jump/duck) into game logic, in
   * addition to the per-tick onInput(state). Future: trigger jump/roll animation
   * + physics. The transport layer already relays the event to the display
   * separately; this is purely for the simulation.
   * @param {"jump"|"duck"} type
   */
  onEvent(type) {
    // TODO(game-team): react to discrete events. No-op today.
  }

  /** Cleanup hook called on session teardown. */
  dispose() {
    // TODO(game-team): release any game resources. No-op today.
  }
}
