/**
 * emulatorInput.js — pure, DOM-free translation of the server's input:state /
 * input:event contract into abstract emulator key commands.
 *
 * This is the single source of truth for "how does interpreted input drive the
 * Subway-Surfers emulator", shared by BOTH:
 *   - the browser display (server/public/index.html), which dispatches the
 *     resulting commands as real KeyboardEvents on the Unity canvas, and
 *   - the server-side Game stub (server/game/game.js), whose onInput()/onEvent()
 *     delegate here so the seam runs real interpretation logic, not a no-op.
 *
 * It lives under public/ so the browser can import it (express only serves
 * public/); the Node server imports it via ../public/emulatorInput.js. No DOM,
 * no Node APIs — just data in, command names out.
 *
 * The emulator's only input surface is synthetic KeyboardEvents (Subway Surfers
 * is a compiled WASM build). Controls:
 *   ← / →  change lane (RELATIVE: one tap = one lane)
 *   ↑      jump
 *   ↓      roll / duck
 * `running`, `intensity` and `plank` have no keyboard analog (the avatar
 * auto-runs; jetpack is an in-game pickup), so they are surfaced in the HUD only
 * and produce no commands here.
 */

/** Command name -> KeyboardEvent fields. Used by the browser to dispatch. */
export const KEY_INFO = Object.freeze({
  left: { keyCode: 37, code: "ArrowLeft", key: "ArrowLeft" },
  right: { keyCode: 39, code: "ArrowRight", key: "ArrowRight" },
  up: { keyCode: 38, code: "ArrowUp", key: "ArrowUp" },
  down: { keyCode: 40, code: "ArrowDown", key: "ArrowDown" },
});

/**
 * Translates the interpreted input stream into discrete key "taps".
 *
 * The server reports lane as an ABSOLUTE position (-1 left, 0 center, 1 right),
 * but the emulator moves RELATIVELY (one ← / → tap shifts one lane). So we track
 * the lane we've already applied and emit the difference as taps.
 */
export class EmulatorInput {
  constructor() {
    /** The lane we have already steered the avatar to. Game starts centered. */
    this.appliedLane = 0;
  }

  /**
   * Fold one input:state into key commands.
   * @param {{ lane?: -1|0|1 }} state
   * @returns {Array<"left"|"right">} taps needed to reach state.lane (often []).
   */
  onState(state) {
    const cmds = [];
    const lane = state?.lane;
    if (lane === -1 || lane === 0 || lane === 1) {
      const delta = lane - this.appliedLane; // ∈ {-2,-1,0,1,2}
      const dir = delta > 0 ? "right" : "left";
      for (let i = 0; i < Math.abs(delta); i++) cmds.push(dir);
      this.appliedLane = lane;
    }
    return cmds;
  }

  /**
   * Translate a one-shot discrete event into a key tap.
   * @param {"jump"|"duck"} type
   * @returns {Array<"up"|"down">}
   */
  onEvent(type) {
    if (type === "jump") return ["up"];
    if (type === "duck") return ["down"];
    return [];
  }

  /** Snapshot for /debug. */
  getState() {
    return { appliedLane: this.appliedLane };
  }

  /** Reset to neutral (e.g. when the phone detaches and lane returns to center). */
  reset() {
    this.appliedLane = 0;
  }
}
