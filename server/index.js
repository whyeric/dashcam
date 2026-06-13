import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import express from "express";
import { WebSocketServer } from "ws";

import { config } from "./config.js";
import { logger } from "./utils/logger.js";
import { now } from "./utils/timing.js";
import { Connection, ROLE } from "./net/connection.js";
import { dispatch } from "./net/handlers.js";
import { startHeartbeat } from "./net/heartbeat.js";
import { SessionManager } from "./sessions/sessionManager.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(__dirname, "public");

const manager = new SessionManager(logger);

// Registry of every live socket -> its Connection wrapper.
/** @type {Map<import('ws').WebSocket, Connection>} */
const connections = new Map();

// ---------------------------------------------------------------------
// HTTP (express): static display page + /debug.
// ---------------------------------------------------------------------
const app = express();
app.use(express.static(PUBLIC_DIR));

// Live snapshot of sessions + reconstructed InputState (PLAN.md §8 / §13).
app.get("/debug", (_req, res) => {
  res.json({
    serverT: Math.round(now()),
    connections: connections.size,
    config: {
      TICK_HZ: config.TICK_HZ,
      RUNNING_TIMEOUT_MS: config.RUNNING_TIMEOUT_MS,
      LANE_TIMEOUT_WARN_MS: config.LANE_TIMEOUT_WARN_MS,
      PLANK_TIMEOUT_MS: config.PLANK_TIMEOUT_MS,
      RATE_LIMIT_PER_SEC: config.RATE_LIMIT_PER_SEC,
    },
    ...manager.snapshot(),
  });
});

const server = http.createServer(app);

// ---------------------------------------------------------------------
// WebSocket: share the HTTP server, only accept upgrades on WS_PATH.
// ---------------------------------------------------------------------
const wss = new WebSocketServer({ noServer: true });

server.on("upgrade", (req, socket, head) => {
  let pathname;
  try {
    pathname = new URL(req.url, "http://localhost").pathname;
  } catch {
    socket.destroy();
    return;
  }
  if (pathname !== config.WS_PATH) {
    socket.destroy();
    return;
  }
  wss.handleUpgrade(req, socket, head, (ws) => wss.emit("connection", ws, req));
});

wss.on("connection", (ws) => {
  const conn = new Connection(ws, logger);
  connections.set(ws, conn);
  conn.log.info("socket connected");

  ws.on("message", (raw) => {
    // dispatch() has its own try/catch, but guard the boundary too — the server
    // must never crash on inbound data.
    try {
      dispatch(conn, raw, manager);
    } catch (err) {
      conn.log.error({ err }, "unexpected error in dispatch boundary");
    }
  });

  ws.on("pong", () => {
    conn.isAlive = true;
    conn.lastPongAt = now();
  });

  ws.on("close", (code, reason) => {
    handleDisconnect(conn, `close ${code} ${reason?.toString?.() ?? ""}`.trim());
  });

  ws.on("error", (err) => {
    conn.log.warn({ err }, "socket error");
    // 'close' will follow and run cleanup.
  });
});

/** Clean up after a socket goes away. Display disconnect tears down its session. */
function handleDisconnect(conn, reason) {
  if (!connections.has(conn.ws)) return; // already handled
  connections.delete(conn.ws);
  conn.log.info({ role: conn.role, reason }, "socket disconnected");

  if (conn.role === ROLE.DISPLAY && conn.sessionCode) {
    // Display owns the session: no display, no game.
    manager.destroy(conn.sessionCode, "display disconnected");
  } else if (conn.role === ROLE.PHONE && conn.sessionCode) {
    // Phone leaving: session survives, runner times out to neutral.
    const session = manager.get(conn.sessionCode);
    if (session && session.phone === conn) session.detachPhone();
  }
}

// ---------------------------------------------------------------------
// Liveness watcher + boot.
// ---------------------------------------------------------------------
const heartbeat = startHeartbeat(wss, (ws) => connections.get(ws), logger);

server.listen(config.PORT, config.HOST, () => {
  logger.info(
    { host: config.HOST, port: config.PORT, wsPath: config.WS_PATH, tickHz: config.TICK_HZ },
    `server up — display: http://localhost:${config.PORT}/  ws: ws://localhost:${config.PORT}${config.WS_PATH}  debug: http://localhost:${config.PORT}/debug`
  );
});

// ---------------------------------------------------------------------
// Graceful shutdown.
// ---------------------------------------------------------------------
function shutdown(signal) {
  logger.info({ signal }, "shutting down");
  heartbeat.stop();
  for (const conn of connections.values()) conn.close(1001, "server shutting down");
  wss.close();
  server.close(() => process.exit(0));
  // Hard exit if something hangs.
  setTimeout(() => process.exit(0), 2000).unref();
}
process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));
