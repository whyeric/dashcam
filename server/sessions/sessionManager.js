import { Session } from "./session.js";
import { ErrorCode } from "../net/messages.js";
import { config } from "../config.js";

/** Thrown by joinSession; carries an ErrorCode the dispatcher maps to an error event. */
export class SessionError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

/**
 * Owns the set of active sessions and the 4-digit pairing-code namespace
 * (PLAN.md §2). Single-player: one display + one phone per session.
 */
export class SessionManager {
  constructor(logger) {
    this.log = logger.child({ component: "sessions" });
    /** @type {Map<string, Session>} */
    this.sessions = new Map();
  }

  /** Create a new session owned by `displayConn`. Returns the Session. */
  createSession(displayConn) {
    const code = this.#generateUniqueCode();
    const session = new Session({
      code,
      display: displayConn,
      logger: this.log,
      onDestroy: (c) => this.sessions.delete(c),
    });
    this.sessions.set(code, session);
    session.start();
    return session;
  }

  /**
   * Attach a phone to an existing session.
   * @throws {SessionError} SESSION_NOT_FOUND | SESSION_FULL
   * @returns {Session}
   */
  joinSession(code, phoneConn) {
    const session = this.sessions.get(code);
    if (!session) {
      throw new SessionError(ErrorCode.SESSION_NOT_FOUND, `no session with code ${code}`);
    }
    if (session.hasPhone) {
      throw new SessionError(ErrorCode.SESSION_FULL, `session ${code} already has a phone`);
    }
    session.attachPhone(phoneConn);
    return session;
  }

  get(code) {
    return this.sessions.get(code);
  }

  /** Destroy a session by code (used on display disconnect). */
  destroy(code, reason) {
    const session = this.sessions.get(code);
    if (session) session.destroy(reason);
  }

  /** Snapshot of all sessions for the /debug route. */
  snapshot() {
    return {
      count: this.sessions.size,
      sessions: [...this.sessions.values()].map((s) => s.snapshot()),
    };
  }

  #generateUniqueCode() {
    const len = config.SESSION_CODE_LENGTH;
    const max = 10 ** len;
    // Tiny space (10^4) — loop with a collision check. Fine for a single booth.
    for (let attempt = 0; attempt < 10000; attempt++) {
      const code = String(Math.floor(Math.random() * max)).padStart(len, "0");
      if (!this.sessions.has(code)) return code;
    }
    // Pathological fallback (space exhausted): should never happen at a booth.
    throw new Error("could not allocate a unique session code");
  }
}
