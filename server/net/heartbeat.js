import { config } from "../config.js";
import { now } from "../utils/timing.js";

/**
 * Transport-level liveness via ws ping/pong FRAMES (PLAN.md §10).
 *
 * This is independent of application-level game heartbeats (running_state etc.)
 * and of the application-level `ping`/`pong` MESSAGES. A phone can legitimately
 * stop sending game heartbeats (player standing still) while its socket is fine,
 * so we need this separate signal to detect genuinely dead/half-open sockets.
 *
 * Every WS_PING_INTERVAL_MS we ping each socket; a socket that hasn't sent a pong
 * frame within WS_PING_TIMEOUT_MS is terminated, which triggers the normal close
 * lifecycle. lastPongAt lives on the Connection and is updated by index.js on the
 * raw socket's 'pong' event.
 *
 * @param {import('ws').WebSocketServer} wss
 * @param {(ws: import('ws').WebSocket) => import('./connection.js').Connection|undefined} getConn
 * @param {import('pino').Logger} logger
 * @returns {{ stop: () => void }}
 */
export function startHeartbeat(wss, getConn, logger) {
  const timer = setInterval(() => {
    const t = now();
    for (const ws of wss.clients) {
      const conn = getConn(ws);
      if (!conn) continue;

      if (t - conn.lastPongAt > config.WS_PING_TIMEOUT_MS) {
        logger.warn({ conn: conn.id, silentMs: Math.round(t - conn.lastPongAt) }, "no pong, terminating dead socket");
        conn.terminate();
        continue;
      }

      try {
        ws.ping();
      } catch {
        // socket already gone — the close handler will clean it up.
      }
    }
  }, config.WS_PING_INTERVAL_MS);

  // Don't let the heartbeat timer keep the event loop alive on shutdown.
  timer.unref?.();

  return {
    stop() {
      clearInterval(timer);
    },
  };
}
