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
                player = data.get("player")  # optional: "p1"/"p2" for 1v1 routing

                # Running/intensity indicator message for 1v1 dashboards. The
                # Unity game has no running input analog in the legacy path, but
                # the split-screen host can still display the live detector state.
                if data.get("type") in ("running", "running_state"):
                    try:
                        intensity = max(0.0, min(1.0, float(data.get("intensity", 0))))
                    except (TypeError, ValueError):
                        print(f"[server] Invalid intensity: {data.get('intensity')!r}")
                        continue
                    out = {
                        "type": "running",
                        "intensity": intensity,
                        "running": bool(data.get("running", intensity > 0.05)),
                    }
                    if player:
                        out["player"] = player
                    websockets.broadcast(clients - {websocket}, json.dumps(out))
                    continue

                # Absolute-lane message (WiFi-robust): the camera PC re-asserts an
                # absolute lane (-1|0|1) on a timer so a dropped packet self-heals.
                # Pass it straight through; the browser folds it into left/right taps.
                if data.get("type") == "lane":
                    lane = data.get("lane")
                    if lane not in (-1, 0, 1):
                        print(f"[server] Invalid lane: {lane!r}")
                        continue
                    out = {"type": "lane", "lane": lane}
                    if player:
                        out["player"] = player
                    websockets.broadcast(clients - {websocket}, json.dumps(out))
                    continue

                action = data.get("action", "").lower()
                key = data.get("key", "").lower()

                if action not in ("press", "release"):
                    print(f"[server] Invalid action: {action}")
                    continue

                if key not in VALID_KEYS:
                    print(f"[server] Unknown key: {key}")
                    continue

                # Broadcast to all connected game clients. The player tag (if any)
                # is passed through so each game panel can filter to its own player.
                out = {"action": action, "key": key}
                if player:
                    out["player"] = player
                websockets.broadcast(clients - {websocket}, json.dumps(out))

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
    host = "0.0.0.0"  # bind all interfaces so remote camera PCs can connect (1v1)
    port = 8765

    print(f"[server] Starting WebSocket server on ws://{host}:{port}")
    print(f"[server] Valid keys: {', '.join(sorted(VALID_KEYS))}")
    print(f'[server] Format: {{"action": "press|release", "key": "left|right|..."}}')
    print()

    async with websockets.serve(handler, host, port):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
