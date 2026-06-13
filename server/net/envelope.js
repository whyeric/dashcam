import { Envelope, getClientDataSchema } from "./messages.js";
import { now } from "../utils/timing.js";

/**
 * Parse + validate a raw inbound WS frame.
 *
 * Pipeline: JSON.parse -> Envelope schema -> per-type `data` schema. Any failure
 * returns { ok: false, reason } instead of throwing — the caller turns that into
 * an INVALID_PAYLOAD error. The server must never crash on bad input.
 *
 * @param {string|Buffer} raw
 * @returns {{ ok: true, type: string, data: object, t: number|undefined }
 *          | { ok: false, reason: string }}
 */
export function parseInbound(raw) {
  let json;
  try {
    json = JSON.parse(typeof raw === "string" ? raw : raw.toString("utf8"));
  } catch {
    return { ok: false, reason: "malformed JSON" };
  }

  const env = Envelope.safeParse(json);
  if (!env.success) {
    return { ok: false, reason: `bad envelope: ${formatIssues(env.error)}` };
  }

  const { type, data, t } = env.data;

  const dataSchema = getClientDataSchema(type);
  if (!dataSchema) {
    return { ok: false, reason: `unknown message type: ${type}` };
  }

  const parsedData = dataSchema.safeParse(data);
  if (!parsedData.success) {
    return { ok: false, reason: `bad data for ${type}: ${formatIssues(parsedData.error)}` };
  }

  return { ok: true, type, data: parsedData.data, t };
}

/**
 * Build an outbound wire message. Stamps the envelope `t` with serverT (monotonic
 * ms). Payloads that the contract says carry their own `serverT` (input:state,
 * input:event, pong) should set it explicitly in `data` before calling this.
 *
 * @param {string} type
 * @param {object} [data]
 * @returns {string} JSON string ready for ws.send()
 */
export function serialize(type, data = {}) {
  return JSON.stringify({ type, data, t: now() });
}

function formatIssues(zodError) {
  return zodError.issues
    .map((i) => `${i.path.join(".") || "(root)"}: ${i.message}`)
    .join("; ");
}
