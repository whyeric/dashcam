import { parseInbound } from "./envelope.js";
import { MessageType, ErrorCode } from "./messages.js";
import { ROLE } from "./connection.js";
import { SessionError } from "../sessions/sessionManager.js";
import { now } from "../utils/timing.js";

/**
 * Top-level inbound dispatch. Pipeline (PLAN.md §8):
 *   1. rate limit (count every frame)
 *   2. parse + validate (JSON -> envelope -> per-type data schema)
 *   3. route to a handler inside try/catch
 * Any failure sends an `error` event and returns — the socket stays open and
 * the process never crashes.
 *
 * @param {import('./connection.js').Connection} conn
 * @param {string|Buffer} raw
 * @param {import('../sessions/sessionManager.js').SessionManager} manager
 */
export function dispatch(conn, raw, manager) {
  // 1. Rate limit.
  if (!conn.bucket.tryRemove(1)) {
    conn.sendError(ErrorCode.RATE_LIMITED, "too many messages");
    conn.log.debug("rate limited inbound message");
    return;
  }

  // 2. Parse + validate.
  const parsed = parseInbound(raw);
  if (!parsed.ok) {
    conn.sendError(ErrorCode.INVALID_PAYLOAD, parsed.reason);
    conn.log.warn({ reason: parsed.reason }, "rejected invalid inbound message");
    return;
  }

  // 3. Route. parseInbound guarantees the type is a known ClientData key.
  const handler = HANDLERS[parsed.type];
  try {
    handler(conn, parsed.data, manager);
  } catch (err) {
    conn.log.error({ err, type: parsed.type }, "handler threw (socket kept open)");
    conn.sendError(ErrorCode.INTERNAL, "internal error");
  }
}

// =====================================================================
// Per-type handlers. One function per inbound MessageType.
// =====================================================================

function onCreateSession(conn, _data, manager) {
  // Idempotent: a display that re-sends create_session just gets its code back.
  if (conn.isDisplay && conn.sessionCode && manager.get(conn.sessionCode)) {
    conn.send(MessageType.SESSION_CREATED, { sessionCode: conn.sessionCode });
    return;
  }
  if (conn.role !== ROLE.UNKNOWN) {
    conn.sendError(ErrorCode.INVALID_PAYLOAD, "connection already bound to a session");
    return;
  }

  const session = manager.createSession(conn);
  conn.role = ROLE.DISPLAY;
  conn.sessionCode = session.code;
  conn.log.info({ session: session.code }, "display created session");
  conn.send(MessageType.SESSION_CREATED, { sessionCode: session.code });
}

function onJoin(conn, data, manager) {
  if (conn.role !== ROLE.UNKNOWN) {
    conn.sendError(ErrorCode.INVALID_PAYLOAD, "connection already bound to a session");
    return;
  }

  try {
    const session = manager.joinSession(data.sessionCode, conn);
    conn.role = ROLE.PHONE;
    conn.sessionCode = session.code;
    conn.log.info({ session: session.code }, "phone joined session");
    conn.send(MessageType.SESSION_JOINED, { sessionCode: session.code });
  } catch (err) {
    if (err instanceof SessionError) {
      conn.sendError(err.code, err.message);
      conn.log.warn({ code: err.code, attempted: data.sessionCode }, "phone join rejected");
      return;
    }
    throw err; // unexpected -> bubble to dispatch's try/catch
  }
}

function onRunningState(conn, data, manager) {
  const session = requirePhoneSession(conn, manager);
  if (!session) return;
  session.inputState.updateRunning(data.intensity, now());
}

function onLanePosition(conn, data, manager) {
  const session = requirePhoneSession(conn, manager);
  if (!session) return;
  session.inputState.updateLane(data.lane, now());
}

function onPlankState(conn, data, manager) {
  const session = requirePhoneSession(conn, manager);
  if (!session) return;
  session.inputState.updatePlank(data.active, now());
}

function onJump(conn, _data, manager) {
  const session = requirePhoneSession(conn, manager);
  if (!session) return;
  session.relayEvent("jump");
}

function onDuck(conn, _data, manager) {
  const session = requirePhoneSession(conn, manager);
  if (!session) return;
  session.relayEvent("duck");
}

function onPing(conn) {
  conn.send(MessageType.PONG, { serverT: now() });
}

/**
 * Resolve the session a phone message belongs to, or send NOT_IN_SESSION.
 * @returns {import('../sessions/session.js').Session | null}
 */
function requirePhoneSession(conn, manager) {
  if (!conn.isPhone || !conn.sessionCode) {
    conn.sendError(ErrorCode.NOT_IN_SESSION, "join a session before sending input");
    return null;
  }
  const session = manager.get(conn.sessionCode);
  if (!session) {
    conn.sendError(ErrorCode.NOT_IN_SESSION, "session no longer exists");
    return null;
  }
  return session;
}

const HANDLERS = Object.freeze({
  [MessageType.DISPLAY_CREATE_SESSION]: onCreateSession,
  [MessageType.PHONE_JOIN]: onJoin,
  [MessageType.PHONE_RUNNING_STATE]: onRunningState,
  [MessageType.PHONE_LANE_POSITION]: onLanePosition,
  [MessageType.PHONE_PLANK_STATE]: onPlankState,
  [MessageType.PHONE_JUMP]: onJump,
  [MessageType.PHONE_DUCK]: onDuck,
  [MessageType.PING]: onPing,
});
