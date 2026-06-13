import asyncio
import json
import websockets

# Connected clients.  ws -> {"role": "screen"|"controller", "player": "p1"|"p2"|None}
clients = {}

# Key names the server accepts from controllers.
VALID_KEYS = {
    "left", "right", "up", "down",
    "space", "jump",
    "a", "d", "w", "s",
}

PLAYER_SLOTS = ("p1", "p2")


def assigned_players():
    """The set of player slots currently held by a controller."""
    return {info["player"] for info in clients.values() if info.get("player") in PLAYER_SLOTS}


async def send_json(websocket, obj):
    try:
        await websocket.send(json.dumps(obj))
    except Exception:
        pass  # socket going away; its handler's finally will clean it up


async def broadcast(payload, exclude=None):
    """Send a JSON string to every connected client except `exclude`."""
    for client in list(clients):
        if client is exclude:
            continue
        try:
            await client.send(payload)
        except Exception:
            pass


async def register(websocket):
    """Consume the first message and decide this connection's role / player.

    Protocol (first message):
      screen:      {"type":"register","role":"screen"}
      controller:  {"type":"register","role":"controller","player":"p1"|"p2"}
                   ("player" optional -> auto-assign the first free slot)

    Returns (role, player). `player` is None for screens, an unassigned
    controller, or a rejected join (slot_taken / game_full).
    """
    try:
        first_msg = await websocket.recv()
    except websockets.ConnectionClosed:
        return ("controller", None)

    role = "controller"
    requested = None
    try:
        data = json.loads(first_msg)
        role = data.get("role", "controller")
        requested = data.get("player")
    except (json.JSONDecodeError, AttributeError, TypeError):
        role = "controller"

    if role == "screen":
        clients[websocket] = {"role": "screen", "player": None}
        print("[server] Game screen connected.")
        return ("screen", None)

    # --- controller ---
    taken = assigned_players()

    if requested in PLAYER_SLOTS:
        if requested in taken:
            # Requested slot is busy — let the phone bounce back to the lobby.
            clients[websocket] = {"role": "controller", "player": None}
            await send_json(websocket, {"type": "error", "reason": "slot_taken", "player": requested})
            print(f"[server] controller requested {requested}, but it is taken.")
            return ("controller", None)
        player = requested
    else:
        # No valid request -> auto-assign the first free slot (back-compat).
        player = next((p for p in PLAYER_SLOTS if p not in taken), None)

    if player is None:
        clients[websocket] = {"role": "controller", "player": None}
        await send_json(websocket, {"type": "error", "reason": "game_full"})
        print("[server] controller rejected — game full.")
        return ("controller", None)

    clients[websocket] = {"role": "controller", "player": player}
    await send_json(websocket, {"type": "assign", "player": player})
    await broadcast(json.dumps({"type": "player_connected", "player": player}), exclude=websocket)
    print(f"[server] {player} connected. {len(clients)} client(s) total.")
    return ("controller", player)


async def handler(websocket):
    role, player = await register(websocket)

    try:
        async for message in websocket:
            # Only an assigned controller produces routable input.
            if player not in PLAYER_SLOTS:
                continue

            action = None
            key = None
            try:
                data = json.loads(message)
                action = str(data.get("action", "")).lower()
                key = str(data.get("key", "")).lower()
            except (json.JSONDecodeError, AttributeError, TypeError):
                raw = message.strip().lower()
                if raw in VALID_KEYS:
                    action, key = "press", raw

            if action not in ("press", "release") or key not in VALID_KEYS:
                continue

            await broadcast(json.dumps({
                "type": "input",     # so 1v1.html can route it
                "player": player,    # which player sent it
                "action": action,
                "key": key,
            }), exclude=websocket)

    except websockets.ConnectionClosed:
        pass
    finally:
        info = clients.pop(websocket, None)
        gone = info.get("player") if info else None
        if gone in PLAYER_SLOTS:
            print(f"[server] {gone} disconnected.")
            await broadcast(json.dumps({"type": "player_disconnected", "player": gone}))
        else:
            print(f"[server] {(info.get('role') if info else 'client')} disconnected.")


async def main():
    host = "0.0.0.0"  # 0.0.0.0 so phones on the same network can connect
    port = 8765
    print(f"[server] Starting WebSocket server on ws://{host}:{port}")
    print("[server] screen -> {'role':'screen'} ; controller -> {'role':'controller','player':'p1'|'p2'}")
    async with websockets.serve(handler, host, port):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
