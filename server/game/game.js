/**
 * Game — the integration seam between the transport layer and the emulator.
 *
 * This is the single seam where game logic plugs into the transport layer
 * (PLAN.md §12). The Session constructs one Game per session and calls
 * onInput() then tick() every server tick (30Hz).
 *
 * In this project the *renderer* is a browser-side Unity WASM build (the display
 * page), which can only be driven by synthetic KeyboardEvents and only runs in
 * the browser — so the authoritative simulation does NOT live in Node. What we
 * CAN run server-side is the **input-interpretation layer**: turning the
 * reconstructed InputState / discrete events into the abstract emulator key
 * commands that actually drive Subway Surfers. onInput()/onEvent() delegate to
 * the shared EmulatorInput module (the same one the display imports), so this is
 * real, exercised logic — a server-authoritative mirror of what the display is
 * doing — rather than a no-op. It is surfaced in getState() (-> /debug).
 *
 * It deliberately does NOT implement obstacles, scoring, collision, or lives,
 * and does NOT emit any new messages: the display computes its own keys from the
 * input:state it already receives, so the wire protocol is untouched. A future
 * game team can either replace this with a real simulation or have it emit
 * game:* events via ctx.emit() — no transport changes required either way.
 */
import { EmulatorInput } from "../public/emulatorInput.js";

export class Game {
  /**
   * @param {object} ctx
   * @param {string} ctx.sessionCode
   * @param {(type: string, data: object) => void} ctx.emit
   *   Routes a message to the display. Provided for future game:state /
   *   game:event / game:over events; unused by this build (protocol untouched).
   * @param {import('pino').Logger} ctx.logger
   */
  constructor(ctx) {
    this.ctx = ctx;
    /** Shared interpretation logic — same module the display runs. */
    this.emu = new EmulatorInput();
    /** Key commands accumulated this tick, drained in tick(). */
    this.pendingKeys = [];
    /** Last input:state seen, for /debug visibility. */
    this.lastState = null;
  }

  /**
   * Called once per tick with the freshly reconstructed InputState, before tick().
   * Delegates to the shared emulator-input logic: lane changes become relative
   * key taps. running / intensity / plank have no keyboard analog and are HUD-only.
   *
   * @param {{ running: boolean, intensity: number, lane: -1|0|1, plank: boolean, serverT: number }} state
   */
  onInput(state) {
    this.lastState = state;
    const cmds = this.emu.onState(state);
    if (cmds.length) this.pendingKeys.push(...cmds);
  }

  /**
   * Called once per server tick (30Hz) after onInput(). Drains the interpreted
   * key commands. The display performs the actual KeyboardEvent dispatch from the
   * input:state it receives; here we only flush the mirror (logged at debug) so
   * getState() reflects current interpretation rather than stale data. A future
   * game team would advance a real simulation here and emit game:* via ctx.emit().
   */
  tick() {
    if (this.pendingKeys.length) {
      this.ctx.logger.debug({ keys: this.pendingKeys }, "interpreted emulator key commands");
      this.pendingKeys.length = 0;
    }
  }

  /**
   * Snapshot of the interpreted input state (for /debug or late-join).
   * @returns {object}
   */
  getState() {
    return { emulator: this.emu.getState(), lastInput: this.lastState };
  }

  /**
   * One-shot relay of a discrete input event (jump/duck), in addition to the
   * per-tick onInput(state). Delegates to the shared logic (jump -> ↑, duck -> ↓).
   * The transport layer relays the event to the display separately; this keeps the
   * server-side mirror in sync.
   * @param {"jump"|"duck"} type
   */
  onEvent(type) {
    const cmds = this.emu.onEvent(type);
    if (cmds.length) this.pendingKeys.push(...cmds);
  }

  /** Cleanup hook called on session teardown. */
  dispose() {
    this.pendingKeys.length = 0;
    this.lastState = null;
  }
}
