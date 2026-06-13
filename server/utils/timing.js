import { performance } from "node:perf_hooks";

/**
 * Monotonic clock in milliseconds. Use this everywhere instead of Date.now()
 * for deltas, timeouts, and serverT stamps — it never jumps backwards.
 * @returns {number} ms since an arbitrary (process-relative) epoch.
 */
export function now() {
  return performance.now();
}

/**
 * Deadline-based fixed-step scheduler. Advances `nextDeadline` by a constant
 * step every tick so transient scheduling lateness self-corrects instead of
 * compounding into drift. If we ever fall more than one full interval behind
 * (e.g. after a GC pause / stall), we resync rather than firing a burst of
 * catch-up ticks.
 *
 * @param {object} opts
 * @param {number} opts.intervalMs   Tick period in ms.
 * @param {() => void} opts.onTick    Called once per tick (errors are caught + logged).
 * @param {import('pino').Logger} [opts.logger]
 * @param {string} [opts.label]      Identifier for log lines.
 * @returns {{ start: () => void, stop: () => void, isRunning: () => boolean }}
 */
export function createTickScheduler({ intervalMs, onTick, logger, label = "tick" }) {
  let timer = null;
  let running = false;
  let nextDeadline = 0;

  function loop() {
    if (!running) return;

    try {
      onTick();
    } catch (err) {
      logger?.error({ err, label }, "tick handler threw (loop continues)");
    }

    nextDeadline += intervalMs;
    let delay = nextDeadline - now();

    if (delay < -intervalMs) {
      // Fell badly behind — resync to avoid a catch-up burst.
      logger?.warn({ behindMs: Math.round(-delay), label }, "tick loop fell behind, resyncing");
      nextDeadline = now();
      delay = 0;
    }

    timer = setTimeout(loop, Math.max(0, delay));
  }

  return {
    start() {
      if (running) return;
      running = true;
      nextDeadline = now() + intervalMs;
      timer = setTimeout(loop, intervalMs);
    },
    stop() {
      running = false;
      if (timer) clearTimeout(timer);
      timer = null;
    },
    isRunning() {
      return running;
    },
  };
}
