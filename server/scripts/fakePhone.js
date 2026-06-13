#!/usr/bin/env node
import WebSocket from "ws";
import chalk from "chalk";
import { config } from "../config.js";

// ---------------------------------------------------------------------
// fakePhone — a CLI stand-in for the phone controller. Drives the server
// with scripted scenarios so you can test the transport without a real
// phone + pose detection. See README for the full list.
// ---------------------------------------------------------------------

const SCENARIOS = ["pair_only", "steady_run", "obstacle_course", "bad_payload", "disconnect_mid_run", "spam_actions"];

const args = parseArgs(process.argv.slice(2));
const url = args.url || `ws://localhost:${args.port || config.PORT}${config.WS_PATH}`;
const scenario = args.scenario || "pair_only";

if (args.help || !SCENARIOS.includes(scenario)) {
  printHelp();
  process.exit(args.help ? 0 : 1);
}

const needsCode = scenario !== "bad_payload";
if (needsCode && !args.code) {
  console.error(chalk.red("This scenario needs a --code (get it from fakeDisplay or the display page)."));
  printHelp();
  process.exit(1);
}

// ---- pretty logging --------------------------------------------------
const ts = () => chalk.gray(new Date().toLocaleTimeString("en-GB") + "." + String(Date.now() % 1000).padStart(3, "0"));
const banner = (s) => console.log(chalk.bold.bgMagenta.white(` ${s} `));
const info = (s) => console.log(`${ts()} ${chalk.gray(s)}`);
const sentLog = (type, data) =>
  console.log(`${ts()} ${chalk.cyan("→ SEND")} ${chalk.cyan.bold(type)} ${chalk.gray(JSON.stringify(data))}`);
const rawLog = (s) => console.log(`${ts()} ${chalk.yellow("→ RAW ")} ${chalk.yellow(s)}`);

function recvLog(msg) {
  const color =
    msg.type === "error" ? chalk.red.bold :
    msg.type === "session:joined" ? chalk.green.bold :
    msg.type === "pong" ? chalk.gray :
    chalk.white;
  console.log(`${ts()} ${chalk.magenta("← RECV")} ${color(msg.type)} ${chalk.gray(JSON.stringify(msg.data))}`);
}

// ---- connection ------------------------------------------------------
banner(`fakePhone · scenario=${scenario} · ${url}${needsCode ? ` · code=${args.code}` : ""}`);

const ws = new WebSocket(url);
const timers = new Set();
const every = (ms, fn) => { const id = setInterval(fn, ms); timers.add(id); return id; };
const after = (ms, fn) => { const id = setTimeout(fn, ms); timers.add(id); return id; };
const clearTimers = () => { for (const id of timers) { clearInterval(id); clearTimeout(id); } timers.clear(); };

function send(type, data = {}) {
  if (ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type, data, t: Date.now() }));
  sentLog(type, data);
}
// Send without logging — used by spam_actions so the flood doesn't drown the terminal.
function sendQuiet(type, data = {}) {
  if (ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type, data, t: Date.now() }));
}
function sendRaw(payload, label) {
  if (ws.readyState !== WebSocket.OPEN) return;
  ws.send(payload);
  rawLog(`${label}: ${payload}`);
}
function finish(reason = "scenario complete") {
  clearTimers();
  info(reason + " — closing");
  ws.close(1000, "done");
  after(250, () => process.exit(0));
}

ws.on("open", () => {
  info("socket open");
  if (needsCode) {
    send("phone:join", { sessionCode: String(args.code) });
  } else {
    runScenario(); // bad_payload runs without joining
  }
});

ws.on("message", (raw) => {
  let msg;
  try { msg = JSON.parse(raw.toString()); } catch { info("received non-JSON from server: " + raw); return; }
  recvLog(msg);

  if (msg.type === "session:joined") {
    info(chalk.green(`paired to session ${msg.data.sessionCode} — starting scenario`));
    runScenario();
  }
});

ws.on("close", (code, reason) => {
  info(`socket closed (${code}${reason?.length ? " " + reason : ""})`);
  clearTimers();
});
ws.on("error", (err) => console.error(`${ts()} ${chalk.red("socket error")} ${err.message}`));

process.on("SIGINT", () => finish("interrupted"));

// ---- scenarios -------------------------------------------------------
function runScenario() {
  switch (scenario) {
    case "pair_only": return scPairOnly();
    case "steady_run": return scSteadyRun();
    case "obstacle_course": return scObstacleCourse();
    case "bad_payload": return scBadPayload();
    case "disconnect_mid_run": return scDisconnectMidRun();
    case "spam_actions": return scSpamActions();
  }
}

function scPairOnly() {
  info("idling (paired, sending nothing). Ctrl+C to exit.");
  // Keep the process alive doing nothing — proves pairing + idle liveness.
}

function scSteadyRun() {
  const intensity = clamp01(args.intensity ?? 0.8);
  const durationMs = (args.duration ?? 30) * 1000;
  info(`running at intensity=${intensity} @10Hz for ${durationMs / 1000}s`);
  every(100, () => send("phone:running_state", { intensity }));
  after(durationMs, () => finish("steady_run duration elapsed"));
}

function scObstacleCourse() {
  const state = { running: false, intensity: 0.8, lane: 0, plank: false };

  // Heartbeats reflect current state (PLAN.md §3).
  every(100, () => { if (state.running) send("phone:running_state", { intensity: state.intensity }); });
  every(200, () => send("phone:lane_position", { lane: state.lane }));
  every(200, () => { if (state.plank) send("phone:plank_state", { active: true }); });

  const setLane = (lane) => { state.lane = lane; send("phone:lane_position", { lane }); };
  const setPlank = (active) => { state.plank = active; send("phone:plank_state", { active }); };

  const timeline = [
    [0, "START running + center lane", () => { state.running = true; state.intensity = 0.8; }],
    [2000, "lean LEFT", () => setLane(-1)],
    [4000, "lean RIGHT", () => setLane(1)],
    [5000, "JUMP", () => send("phone:jump", {})],
    [6000, "back to CENTER", () => setLane(0)],
    [7000, "DUCK (roll)", () => send("phone:duck", {})],
    [8500, "PLANK on (jetpack)", () => setPlank(true)],
    [12000, "PLANK off", () => setPlank(false)],
    [13000, "JUMP", () => send("phone:jump", {})],
    [15000, "slow down (intensity 0.3)", () => { state.intensity = 0.3; }],
    [16500, "STOP running (heartbeats cease)", () => { state.running = false; }],
    [18000, "done", () => finish("obstacle_course complete")],
  ];
  for (const [at, label, fn] of timeline) {
    after(at, () => { info(chalk.bold.white("» " + label)); fn(); });
  }
}

function scBadPayload() {
  info("sending a battery of malformed messages — server should reject each WITHOUT crashing");
  const bad = [
    ["not JSON at all", "this is definitely not json {{{"],
    ["missing type", JSON.stringify({ data: {} })],
    ["unknown type", JSON.stringify({ type: "phone:teleport", data: {} })],
    ["intensity out of range", JSON.stringify({ type: "phone:running_state", data: { intensity: 5 } })],
    ["invalid lane value", JSON.stringify({ type: "phone:lane_position", data: { lane: 7 } })],
    ["extra/unknown keys", JSON.stringify({ type: "phone:jump", data: { hax: true } })],
    ["wrong data type", JSON.stringify({ type: "phone:plank_state", data: { active: "yes" } })],
  ];
  bad.forEach(([label, payload], i) => after(i * 300, () => sendRaw(payload, label)));

  // After the garbage, prove the server is still alive and responsive.
  after(bad.length * 300 + 400, () => {
    info("now sending a VALID ping — expect a pong back (server survived)");
    send("ping", {});
  });
  after(bad.length * 300 + 1200, () => finish("bad_payload complete — server survived"));
}

function scDisconnectMidRun() {
  info("running at 0.8 @10Hz, then HARD-dropping the socket after 1.5s");
  info(chalk.yellow("watch fakeDisplay / /debug: running should time out to 0 within ~300ms"));
  every(100, () => send("phone:running_state", { intensity: 0.8 }));
  after(1500, () => {
    clearTimers();
    info(chalk.red("terminating socket NOW (no clean close frame)"));
    ws.terminate();
    after(300, () => process.exit(0));
  });
}

function scSpamActions() {
  info("flooding running_state in bursts (~500/sec) — expect RATE_LIMITED (limit 50/sec)");
  info(chalk.gray("(individual sends are not logged; RATE_LIMITED error frames are throttled to ~1/sec by design)"));
  let n = 0;
  every(20, () => { for (let i = 0; i < 10; i++) { sendQuiet("phone:running_state", { intensity: 0.8 }); n++; } });
  every(1000, () => info(chalk.gray(`… flooded ~${n} messages so far`)));
  after(3000, () => { info(`sent ~${n} messages total`); finish("spam_actions complete"); });
}

// ---- helpers ---------------------------------------------------------
function clamp01(v) { return Math.max(0, Math.min(1, Number(v))); }

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--help" || a === "-h") out.help = true;
    else if (a.startsWith("--")) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("--")) out[key] = true;
      else { out[key] = isNaN(Number(next)) ? next : Number(next); i++; }
    }
  }
  return out;
}

function printHelp() {
  console.log(`
${chalk.bold("fakePhone")} — scripted phone controller

${chalk.bold("Usage:")}
  node server/scripts/fakePhone.js --scenario <name> --code <4-digits> [options]
  npm run fake:phone -- --scenario steady_run --code 4821

${chalk.bold("Scenarios:")}
  ${chalk.cyan("pair_only")}           join then idle (no input)
  ${chalk.cyan("steady_run")}          running_state @10Hz intensity=0.8 for 30s
  ${chalk.cyan("obstacle_course")}     scripted run: lanes + jump + duck + plank, then stop
  ${chalk.cyan("bad_payload")}         malformed messages; server must reject without crashing (no --code needed)
  ${chalk.cyan("disconnect_mid_run")} run, then hard-drop the socket (running should time out to 0)
  ${chalk.cyan("spam_actions")}        200Hz flood to trip the rate limiter

${chalk.bold("Options:")}
  --code <n>        session code (required except bad_payload)
  --scenario <s>    one of the above (default: pair_only)
  --intensity <f>   running intensity 0..1 (steady_run, default 0.8)
  --duration <s>    seconds (steady_run, default 30)
  --url <ws://…>    full WS url (default ws://localhost:${config.PORT}${config.WS_PATH})
  --port <n>        port shortcut if using localhost
`);
}
