import asyncio
import json
import websockets

# Connected controller clients
clients = {}

# Key mappings the server understands
VALID_KEYS = {
    "left", "right", "up", "down",
    "space", "jump",
    "a", "d", "w", "s",
}


async def handler(websocket):
    # Assign player slot on connect
    connected_players = [v["player"] for v in clients.values()]
    
    if "p1" not in connected_players:
        player = "p1"
    elif "p2" not in connected_players:
        player = "p2"
    else:
        player = "spectator"

    clients[websocket] = {"player": player}
    
    # Tell the client who they are
    await websocket.send(json.dumps({"type": "assign", "player": player}))
    print(f"[server] {player} connected. {len(clients)} client(s) total.")

    # after assigning player slot and sending "assign":
    payload = json.dumps({"type": "player_connected", "player": player})
    for client in clients:
        if client != websocket:
            await client.send(payload)
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                action = data.get("action", "").lower()
                key = data.get("key", "").lower()

                if action not in ("press", "release") or key not in VALID_KEYS:
                    continue

                # REPLACE: was `websockets.broadcast(clients - {websocket}, payload)`
                payload = json.dumps({
                    "type": "input",      # ADD: type field so 1v1.html can route it
                    "player": player,     # ADD: tag with which player sent it
                    "action": action,
                    "key": key
                })

                for client in clients:
                    if client != websocket:
                        await client.send(payload)

            except json.JSONDecodeError:
                raw = message.strip().lower()
                if raw in VALID_KEYS:
                    payload = json.dumps({
                        "type": "input",   # ADD
                        "player": player,  # ADD
                        "action": "press",
                        "key": raw
                    })
                    for client in clients:
                        if client != websocket:
                            await client.send(payload)
                else:
                    print(f"[server] Ignored invalid message: {message!r}")

    except websockets.ConnectionClosed:
        pass
    finally:
        # REPLACE: was `clients.discard(websocket)`
        del clients[websocket]
        print(f"[server] {player} disconnected.")

async def main():
    # REPLACE: was `host = "localhost"` — changed to 0.0.0.0 so phones on the network can connect
    host = "0.0.0.0"
    port = 8765
    print(f"[server] Starting WebSocket server on ws://{host}:{port}")
    async with websockets.serve(handler, host, port):
        await asyncio.Future()

asyncio.run(main())
