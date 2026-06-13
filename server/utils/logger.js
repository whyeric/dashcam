import pino from "pino";
import { config } from "../config.js";

// Structured logging. Default output is line-delimited JSON (good for piping/parsing).
// Set LOG_PRETTY=1 (or use `npm run dev`) for colorized human-readable logs in a terminal.
const options = { level: config.LOG_LEVEL };

if (config.LOG_PRETTY) {
  options.transport = {
    target: "pino-pretty",
    options: { colorize: true, translateTime: "HH:MM:ss.l", ignore: "pid,hostname" },
  };
}

export const logger = pino(options);
