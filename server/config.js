// Central tunables. Every value can be overridden by an env var of the same name.
// Plain JS, no framework — keep it boring and readable.

/** Parse an integer env var, falling back to a default. */
function intEnv(name, def) {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return def;
  const n = Number.parseInt(raw, 10);
  return Number.isFinite(n) ? n : def;
}

/** Parse a string env var, falling back to a default. */
function strEnv(name, def) {
  const raw = process.env[name];
  return raw === undefined || raw === "" ? def : raw;
}

export const config = {
  // --- HTTP / WS server ---
  HOST: strEnv("HOST", "0.0.0.0"),
  PORT: intEnv("PORT", 8080),

  // --- Tick loop ---
  TICK_HZ: intEnv("TICK_HZ", 30),

  // --- InputState reconstruction timeouts (ms) ---
  RUNNING_TIMEOUT_MS: intEnv("RUNNING_TIMEOUT_MS", 300), // no running_state -> intensity snaps to 0
  LANE_TIMEOUT_WARN_MS: intEnv("LANE_TIMEOUT_WARN_MS", 500), // warn-only; lane is held
  PLANK_TIMEOUT_MS: intEnv("PLANK_TIMEOUT_MS", 400), // no plank_state -> jetpack forced off

  // --- Rate limiting (per connection) ---
  RATE_LIMIT_PER_SEC: intEnv("RATE_LIMIT_PER_SEC", 50),

  // --- Transport liveness (ws ping/pong frames) ---
  WS_PING_INTERVAL_MS: intEnv("WS_PING_INTERVAL_MS", 10000),
  WS_PING_TIMEOUT_MS: intEnv("WS_PING_TIMEOUT_MS", 25000),

  // --- Sessions ---
  SESSION_CODE_LENGTH: intEnv("SESSION_CODE_LENGTH", 4),

  // --- Logging ---
  LOG_LEVEL: strEnv("LOG_LEVEL", "info"),
  LOG_PRETTY: strEnv("LOG_PRETTY", "") !== "", // set LOG_PRETTY=1 for human-readable logs

  // --- Paths ---
  WS_PATH: strEnv("WS_PATH", "/ws"),
};

// Derived (not env-overridable; computed from the above).
export const derived = {
  TICK_INTERVAL_MS: 1000 / config.TICK_HZ,
};
