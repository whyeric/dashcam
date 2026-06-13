import { InputState } from "../input/inputState.js";
import { Game } from "../game/game.js";
import { MessageType } from "../net/messages.js";
import { createTickScheduler, now } from "../utils/timing.js";
import { config, derived } from "../config.js";

/**
 * One single-player session: exactly one display (the owner) and at most one
 * phone. Holds the reconstructed InputState, the Game stub, and the per-session
 * 30Hz tick loop that broadcasts input:state to the display (PLAN.md §6).
 */
export class Session {
  /**
   * @param {object} opts
   * @param {string} opts.code
   * @param {import('../net/connection.js').Connection} opts.display
   * @param {import('pino').Logger} opts.logger
   * @param {(code: string) => void} opts.onDestroy  Called once when torn down.
   */
  constructor({ code, display, logger, onDestroy }) {
    this.code = code;
    this.display = display;
    this.phone = null;
    this.createdAt = now();
    this.onDestroy = onDestroy;
    this.destroyed = false;

    this.log = logger.child({ session: code });
    this.inputState = new InputState(this.log);

    this.game = new Game({
      sessionCode: code,
      emit: (type, data) => this.broadcastToDisplay(type, data),
      logger: this.log.child({ component: "game" }),
    });

    this.tick = createTickScheduler({
      intervalMs: derived.TICK_INTERVAL_MS,
      onTick: () => this.#onTick(),
      logger: this.log,
      label: `session:${code}`,
    });
  }

  get hasPhone() {
    return this.phone !== null;
  }

  /** Begin the 30Hz broadcast loop. Called by the manager right after creation. */
  start() {
    this.tick.start();
    this.log.info({ tickHz: config.TICK_HZ }, "session created, tick loop started");
  }

  // ---- phone attach / detach ----

  attachPhone(conn) {
    this.phone = conn;
    this.inputState.reset();
    this.log.info({ phoneConn: conn.id }, "phone attached");
    this.broadcastToDisplay(MessageType.PHONE_STATUS, { connected: true });
  }

  detachPhone() {
    if (!this.phone) return;
    this.log.info({ phoneConn: this.phone.id }, "phone detached");
    this.phone = null;
    this.inputState.reset(); // runner returns to neutral immediately
    this.broadcastToDisplay(MessageType.PHONE_STATUS, { connected: false });
  }

  // ---- the tick (PLAN.md §6) ----

  #onTick() {
    const state = this.inputState.compute(now());

    // The central server->display contract: authoritative interpreted input.
    this.broadcastToDisplay(MessageType.INPUT_STATE, state);

    // Game seam — no-ops today, but wired so integration is proven.
    this.game.onInput(state);
    this.game.tick();
  }

  // ---- one-shot discrete event relay (jump / duck) ----

  relayEvent(type) {
    this.broadcastToDisplay(MessageType.INPUT_EVENT, { type, serverT: now() });
    this.game.onEvent(type);
  }

  // ---- output ----

  broadcastToDisplay(type, data) {
    if (this.display) this.display.send(type, data);
  }

  // ---- teardown ----

  destroy(reason = "destroyed") {
    if (this.destroyed) return;
    this.destroyed = true;
    this.tick.stop();
    this.game.dispose();

    // Close the phone socket if still attached — no display means no game.
    if (this.phone) {
      this.phone.close(1001, "session ended");
      this.phone = null;
    }
    this.log.info({ reason }, "session destroyed");
    this.onDestroy?.(this.code);
  }

  /** Snapshot for the /debug route. */
  snapshot() {
    return {
      code: this.code,
      createdAt: Math.round(this.createdAt),
      display: this.display ? this.display.id : null,
      phone: this.phone ? this.phone.id : null,
      inputState: this.inputState.compute(now()),
      game: this.game.getState(),
    };
  }
}
