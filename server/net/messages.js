import { z } from "zod";

// =====================================================================
// Message types. Every wire message is { type, data, t }. `type` is one
// of these namespaced strings.
// =====================================================================
export const MessageType = Object.freeze({
  // client -> server
  DISPLAY_CREATE_SESSION: "display:create_session",
  PHONE_JOIN: "phone:join",
  PHONE_RUNNING_STATE: "phone:running_state",
  PHONE_LANE_POSITION: "phone:lane_position",
  PHONE_PLANK_STATE: "phone:plank_state",
  PHONE_JUMP: "phone:jump",
  PHONE_DUCK: "phone:duck",
  PING: "ping",

  // server -> display
  SESSION_CREATED: "session:created",
  PHONE_STATUS: "phone:status",
  INPUT_STATE: "input:state",
  INPUT_EVENT: "input:event",

  // server -> phone
  SESSION_JOINED: "session:joined",

  // server -> either
  ERROR: "error",
  PONG: "pong",
});

// =====================================================================
// Error codes (see PLAN.md §8). INTERNAL is defensive-only: it is sent if a
// handler throws unexpectedly so the client gets *something* — the server
// never crashes and the socket stays open.
// =====================================================================
export const ErrorCode = Object.freeze({
  INVALID_PAYLOAD: "INVALID_PAYLOAD",
  SESSION_NOT_FOUND: "SESSION_NOT_FOUND",
  SESSION_FULL: "SESSION_FULL",
  NOT_IN_SESSION: "NOT_IN_SESSION",
  RATE_LIMITED: "RATE_LIMITED",
  INTERNAL: "INTERNAL",
});

// =====================================================================
// Shared field schemas
// =====================================================================
const Lane = z.union([z.literal(-1), z.literal(0), z.literal(1)]);
const Unit = z.number().min(0).max(1);
const Code = z.string().regex(/^\d{4}$/, "session code must be 4 digits");

// =====================================================================
// Envelope — validated first, before the type-specific `data`.
// `data` defaults to {} so callers may omit it for empty payloads.
// =====================================================================
export const Envelope = z.object({
  type: z.string().min(1),
  data: z.unknown().optional().default({}),
  t: z.number().finite().optional(),
});

// =====================================================================
// client -> server: per-type `data` schemas. `.strict()` rejects unknown keys
// so typos/garbage surface as INVALID_PAYLOAD instead of being silently ignored.
// =====================================================================
export const ClientData = Object.freeze({
  [MessageType.DISPLAY_CREATE_SESSION]: z.object({}).strict(),
  [MessageType.PHONE_JOIN]: z.object({ sessionCode: Code }).strict(),
  [MessageType.PHONE_RUNNING_STATE]: z.object({ intensity: Unit }).strict(),
  [MessageType.PHONE_LANE_POSITION]: z.object({ lane: Lane }).strict(),
  [MessageType.PHONE_PLANK_STATE]: z.object({ active: z.boolean() }).strict(),
  [MessageType.PHONE_JUMP]: z.object({}).strict(),
  [MessageType.PHONE_DUCK]: z.object({}).strict(),
  [MessageType.PING]: z.object({}).strict(),
});

/** @returns {import('zod').ZodTypeAny | undefined} schema for an inbound type, or undefined if unknown. */
export function getClientDataSchema(type) {
  return ClientData[type];
}

// =====================================================================
// server -> client: documented for reference. The server constructs these;
// they are not used to validate inbound traffic. Kept here so the contract
// lives in one file. (See PLAN.md §5.)
// =====================================================================
export const ServerData = Object.freeze({
  [MessageType.SESSION_CREATED]: z.object({ sessionCode: z.string() }),
  [MessageType.SESSION_JOINED]: z.object({ sessionCode: z.string() }),
  [MessageType.PHONE_STATUS]: z.object({ connected: z.boolean() }),
  [MessageType.INPUT_STATE]: z.object({
    running: z.boolean(),
    intensity: Unit,
    lane: Lane,
    plank: z.boolean(),
    serverT: z.number(),
  }),
  [MessageType.INPUT_EVENT]: z.object({
    type: z.enum(["jump", "duck"]),
    serverT: z.number(),
  }),
  [MessageType.ERROR]: z.object({ code: z.string(), message: z.string() }),
  [MessageType.PONG]: z.object({ serverT: z.number() }),
});

// =====================================================================
// Reserved for the game-logic teammate (PLAN.md §5 / §12). NOT implemented.
// Listed so nobody reuses these names for transport concerns.
// =====================================================================
export const ReservedGameTypes = Object.freeze(["game:state", "game:event", "game:over"]);
