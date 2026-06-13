import asyncio
import json
import websockets

# Connected controller clients
clients = set()

# Key mappings the server understands
VALID_KEYS = {
    "left", "right", "up", "down",
    "space", "jump",
    "a", "d", "w", "s",
}


async def handler(websocket):
    clients.add(websocket)
    print(f"[server] Controller connected. {len(clients)} client(s) total.")

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                action = data.get("action", "").lower()
                key = data.get("key", "").lower()

                if action not in ("press", "release"):
                    print(f"[server] Invalid action: {action}")
                    continue

                if key not in VALID_KEYS:
                    print(f"[server] Unknown key: {key}")
                    continue

                # Broadcast to all connected game clients
                payload = json.dumps({"action": action, "key": key})
                websockets.broadcast(clients - {websocket}, payload)

            except json.JSONDecodeError:
                # Accept raw key name as convenience (e.g. "left" -> press)
                raw = message.strip().lower()
                if raw in VALID_KEYS:
                    payload = json.dumps({"action": "press", "key": raw})
                    websockets.broadcast(clients - {websocket}, payload)
                else:
                    print(f"[server] Ignored invalid message: {message!r}")

    except websockets.ConnectionClosed:
        pass
    finally:
        clients.discard(websocket)
        print(f"[server] Controller disconnected. {len(clients)} client(s) remaining.")


async def main():
    host = "localhost"
    port = 8765

    print(f"[server] Starting WebSocket server on ws://{host}:{port}")
    print(f"[server] Valid keys: {', '.join(sorted(VALID_KEYS))}")
    print(f'[server] Format: {{"action": "press|release", "key": "left|right|..."}}')
    print()

    async with websockets.serve(handler, host, port):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
