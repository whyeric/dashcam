#!/usr/bin/env node
import WebSocket from "ws";
import chalk from "chalk";
import { config } from "../config.js";

// ---------------------------------------------------------------------
// fakeDisplay — a CLI stand-in for the display/renderer. Creates a session,
// prints the pairing code, then shows the live input:state (one rewriting
// status line) plus a log of discrete input:event / phone:status messages.
// Lets you test the server without the real game page.
// ---------------------------------------------------------------------

const args = parseArgs(process.argv.slice(2));
const url = args.url || `ws://localhost:${args.port || config.PORT}${config.WS_PATH}`;
const verbose = !!args.verbose; // print every input:state on its own line instead of a live line

const ts = () => chalk.gray(new Date().toLocaleTimeString("en-GB") + "." + String(Date.now() % 1000).padStart(3, "0"));

let phoneConnected = false;
let lastState = null;
let haveStatusLine = false;

const ws = new WebSocket(url);

ws.on("open", () => {
  line(chalk.gray(`socket open → ${url}`));
  send("display:create_session", {});
});

ws.on("message", (raw) => {
  let msg;
  try { msg = JSON.parse(raw.toString()); } catch { return; }
  handle(msg);
});

ws.on("close", (code) => line(chalk.red(`socket closed (${code})`)));
ws.on("error", (err) => line(chalk.red("socket error: " + err.message)));
process.on("SIGINT", () => { line(chalk.gray("bye")); ws.close(); setTimeout(() => process.exit(0), 100); });

function handle(msg) {
  switch (msg.type) {
    case "session:created":
      printCode(msg.data.sessionCode);
      break;
    case "phone:status":
      phoneConnected = msg.data.connected;
      line(msg.data.connected ? chalk.green.bold("● phone CONNECTED") : chalk.red.bold("○ phone DISCONNECTED — runner will stop"));
      break;
    case "input:state":
      onState(msg.data);
      break;
    case "input:event":
      onEvent(msg.data);
      break;
    case "error":
      line(`${chalk.red.bold("ERROR")} ${chalk.red(msg.data.code)} ${chalk.gray(msg.data.message)}`);
      break;
    case "pong":
      break;
  }
}

function onState(s) {
  lastState = s;
  if (verbose) {
    line(`${ts()} ${statusText(s)}`);
  } else {
    drawStatus();
  }
}

function onEvent(ev) {
  const tag = ev.type === "jump" ? chalk.bgBlue.white.bold("  JUMP  ") : chalk.bgMagenta.white.bold("  DUCK  ");
  line(`${ts()} ${tag} ${chalk.gray("serverT=" + Math.round(ev.serverT))}`);
}

// ---- rendering -------------------------------------------------------

// Print a discrete line without destroying the live status line.
function line(text) {
  if (haveStatusLine) process.stdout.write("\r\x1b[K"); // clear the live status line
  console.log(text);
  if (haveStatusLine && !verbose) drawStatus();
}

function drawStatus() {
  if (!lastState) return;
  haveStatusLine = true;
  process.stdout.write("\r\x1b[K" + statusText(lastState));
}

function statusText(s) {
  const run = s.running
    ? chalk.green.bold("RUN ") + bar(s.intensity) + " " + chalk.green(s.intensity.toFixed(2))
    : chalk.gray("RUN ") + chalk.gray("░░░░░░░░░░ 0.00");
  const lane = chalk.gray("LANE ") + laneIndicator(s.lane);
  const plank = s.plank ? chalk.cyan.bold("PLANK on ") : chalk.gray("PLANK off");
  const phone = phoneConnected ? chalk.green("phone✓") : chalk.red("phone✗");
  return `${run}  ${lane}  ${plank}  ${phone}`;
}

function bar(intensity) {
  const n = Math.round(Math.max(0, Math.min(1, intensity)) * 10);
  return chalk.green("█".repeat(n)) + chalk.gray("░".repeat(10 - n));
}

function laneIndicator(lane) {
  const cell = (pos, label) =>
    lane === pos ? chalk.bgGreen.black.bold(` ${label} `) : chalk.gray(` ${label} `);
  return chalk.gray("[") + cell(-1, "L") + cell(0, "C") + cell(1, "R") + chalk.gray("]");
}

function printCode(code) {
  const w = code.length + 18;
  line("");
  line(chalk.yellow("╔" + "═".repeat(w) + "╗"));
  line(chalk.yellow("║") + chalk.bold.white("    PAIRING CODE: ") + chalk.bold.cyan(code) + "    " + chalk.yellow("║"));
  line(chalk.yellow("╚" + "═".repeat(w) + "╝"));
  line(chalk.gray(`pair a phone with:  npm run fake:phone -- --code ${code} --scenario obstacle_course`));
  line("");
}

// ---- io --------------------------------------------------------------
function send(type, data = {}) {
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type, data, t: Date.now() }));
}

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--verbose" || a === "-v") out.verbose = true;
    else if (a.startsWith("--")) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("--")) out[key] = true;
      else { out[key] = isNaN(Number(next)) ? next : Number(next); i++; }
    }
  }
  return out;
}
