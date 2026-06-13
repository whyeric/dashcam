import { now } from "../utils/timing.js";

/**
 * Lazy token bucket. No background timer — tokens are refilled on demand based
 * on elapsed time since the last check. One bucket per connection.
 *
 * With capacity == refillPerSec, a client may burst up to `capacity` messages
 * and then sustain `refillPerSec` msg/sec. Expected legitimate phone load is
 * ~15-20 msg/sec; default capacity is 50 (PLAN.md §9).
 */
export class TokenBucket {
  /**
   * @param {object} opts
   * @param {number} opts.capacity     Max tokens (burst size).
   * @param {number} opts.refillPerSec Tokens added per second.
   */
  constructor({ capacity, refillPerSec }) {
    this.capacity = capacity;
    this.refillPerSec = refillPerSec;
    this.tokens = capacity;
    this.last = now();
  }

  #refill() {
    const t = now();
    const elapsedSec = (t - this.last) / 1000;
    if (elapsedSec <= 0) return;
    this.tokens = Math.min(this.capacity, this.tokens + elapsedSec * this.refillPerSec);
    this.last = t;
  }

  /**
   * Try to consume `n` tokens.
   * @returns {boolean} true if allowed, false if the bucket is empty (rate limited).
   */
  tryRemove(n = 1) {
    this.#refill();
    if (this.tokens >= n) {
      this.tokens -= n;
      return true;
    }
    return false;
  }
}
