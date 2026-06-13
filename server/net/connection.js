import { randomUUID } from "node:crypto";
import { WebSocket } from "ws";
import { serialize } from "./envelope.js";
import { MessageType, ErrorCode } from "./messages.js";
import { TokenBucket } from "./rateLimiter.js";
import { config } from "../config.js";
import { now } from "../utils/timing.js";

const ROLE = Object.freeze({ UNKNOWN: "unknown", PHONE: "phone", DISPLAY: "display" });

// Only flood-prone error codes are throttled. RATE_LIMITED would otherwise produce
// one error frame per dropped message during a flood. Everything else (e.g.
// INVALID_PAYLOAD) is sent every time so clients see each individual rejection.
const THROTTLED_CODES = new Set([ErrorCode.RATE_LIMITED]);
const ERROR_THROTTLE_MS = 1000;

/**
 * Wraps a raw `ws` socket with everything the rest of the server needs:
 * identity, role, session binding, a rate-limit bucket, liveness bookkeeping,
 * and safe send helpers. The raw socket is never touched outside this class.
 */
export class Connection {
  constructor(ws, logger) {
    this.id = randomUUID().slice(0, 8);
    this.ws = ws;
    this.role = ROLE.UNKNOWN;
    this.sessionCode = null;

    this.bucket = new TokenBucket({
      capacity: config.RATE_LIMIT_PER_SEC,
      refillPerSec: config.RATE_LIMIT_PER_SEC,
    });

    // Transport liveness (see heartbeat.js). Updated on every ws 'pong' frame.
    this.isAlive = true;
    this.lastPongAt = now();

    this.connectedAt = now();
    this.log = logger.child({ conn: this.id });

    this.#lastErrorAt = new Map(); // code -> ms
  }

  #lastErrorAt;

  get isPhone() {
    return this.role === ROLE.PHONE;
  }
  get isDisplay() {
    return this.role === ROLE.DISPLAY;
  }

  /** Send a typed message. No-op if the socket isn't open. */
  send(type, data = {}) {
    if (this.ws.readyState !== WebSocket.OPEN) return;
    try {
      this.ws.send(serialize(type, data));
    } catch (err) {
      this.log.warn({ err, type }, "send failed");
    }
  }

  /**
   * Send an error event. Flood-prone codes (RATE_LIMITED) are throttled so a
   * flood doesn't generate a matching flood of errors; all other codes send
   * every time. Never disconnects the client.
   */
  sendError(code, message) {
    if (THROTTLED_CODES.has(code)) {
      const t = now();
      const last = this.#lastErrorAt.get(code) ?? -Infinity;
      if (t - last < ERROR_THROTTLE_MS) return;
      this.#lastErrorAt.set(code, t);
    }
    this.send(MessageType.ERROR, { code, message });
  }

  /** Gracefully close the socket with an optional reason. */
  close(code = 1000, reason = "") {
    try {
      this.ws.close(code, reason);
    } catch {
      // already closing/closed — ignore.
    }
  }

  /** Force-kill a dead/half-open socket (used by the liveness watcher). */
  terminate() {
    try {
      this.ws.terminate();
    } catch {
      // ignore.
    }
  }
}

export { ROLE };
