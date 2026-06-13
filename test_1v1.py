"""End-to-end check of the 1v1 relay (input_server.py) without any phones.

Spawns the relay as a subprocess and drives it exactly like the real clients
(one screen + two controllers), asserting the register / assign / fan-out /
input-routing / late-join-state / reject / disconnect behavior.

Run with the venv interpreter (it has the `websockets` package):

    .venv/Scripts/python.exe test_1v1.py

Exit code 0 = all checks passed.
"""
import asyncio
import json
import os
import subprocess
import sys
import time

import websockets

HOST = "localhost"
# Use a private port (not the default 8765) so this test runs against its own
# freshly-spawned relay and never collides with an instance you have running.
PORT = 8766
URI = f"ws://{HOST}:{PORT}"
HERE = os.path.dirname(os.path.abspath(__file__))

_passed = 0


def check(cond, label):
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        raise AssertionError(label)


async def recv_json(ws, timeout=3.0):
    raw = await asyncio.wait_for(ws.recv(), timeout)
    return json.loads(raw)


async def register(ws, role, player=None):
    msg = {"type": "register", "role": role}
    if player is not None:
        msg["player"] = player
    await ws.send(json.dumps(msg))


async def wait_for_relay(timeout=8.0):
    """Poll-connect until the relay accepts a socket (or give up)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            async with websockets.connect(URI):
                return True
        except OSError:
            await asyncio.sleep(0.1)
    return False


async def run_checks():
    opened = []

    async def open_ws():
        ws = await websockets.connect(URI)
        opened.append(ws)
        return ws

    try:
        # 1. Screen connects first -> empty roster snapshot.
        screen = await open_ws()
        await register(screen, "screen")
        msg = await recv_json(screen)
        check(msg.get("type") == "state" and msg.get("players") == [],
              "screen gets initial state with empty roster")

        # 2. P1 joins -> assign on p1, player_connected on screen.
        p1 = await open_ws()
        await register(p1, "controller", "p1")
        check(await recv_json(p1) == {"type": "assign", "player": "p1"},
              "p1 receives assign p1")
        check(await recv_json(screen) == {"type": "player_connected", "player": "p1"},
              "screen sees player_connected p1")

        # 3. P2 joins.
        p2 = await open_ws()
        await register(p2, "controller", "p2")
        check(await recv_json(p2) == {"type": "assign", "player": "p2"},
              "p2 receives assign p2")
        check(await recv_json(screen) == {"type": "player_connected", "player": "p2"},
              "screen sees player_connected p2")

        # 4. P1 input is fanned out to the screen, tagged with the right player.
        await p1.send(json.dumps({"action": "press", "key": "left"}))
        check(await recv_json(screen) ==
              {"type": "input", "player": "p1", "action": "press", "key": "left"},
              "screen receives p1 press left (routes to P1 panel)")
        await p1.send(json.dumps({"action": "release", "key": "left"}))
        check(await recv_json(screen) ==
              {"type": "input", "player": "p1", "action": "release", "key": "left"},
              "screen receives p1 release left")

        # 5. Late-join: a screen connecting now learns both players (Bug 1 fix).
        screen2 = await open_ws()
        await register(screen2, "screen")
        msg = await recv_json(screen2)
        check(msg.get("type") == "state" and msg.get("players") == ["p1", "p2"],
              "late-joining screen gets state with [p1, p2]")

        # 6. Requesting a taken slot is rejected.
        c3 = await open_ws()
        await register(c3, "controller", "p1")
        msg = await recv_json(c3)
        check(msg.get("type") == "error" and msg.get("reason") == "slot_taken",
              "third controller requesting p1 -> slot_taken")

        # 7. No free slot -> game_full.
        c4 = await open_ws()
        await register(c4, "controller")  # auto-assign, but both taken
        msg = await recv_json(c4)
        check(msg.get("type") == "error" and msg.get("reason") == "game_full",
              "controller with no free slot -> game_full")

        # 8. Disconnect is broadcast so overlays come back.
        await p2.close()
        opened.remove(p2)
        check(await recv_json(screen) ==
              {"type": "player_disconnected", "player": "p2"},
              "screen sees player_disconnected p2 after p2 closes")

    finally:
        for ws in opened:
            try:
                await ws.close()
            except Exception:
                pass


def main():
    env = dict(os.environ, RELAY_HOST=HOST, RELAY_PORT=str(PORT))
    relay = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "input_server.py")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=HERE,
        env=env,
    )
    try:
        if not asyncio.run(wait_for_relay()):
            # Relay never came up — surface why (e.g. port already in use).
            relay.terminate()
            err = b""
            try:
                _, err = relay.communicate(timeout=3)
            except Exception:
                pass
            print("ERROR: relay did not start on", URI)
            if err:
                print(err.decode(errors="replace"))
            print("Is another input_server.py already bound to port", PORT, "?")
            return 1

        print(f"Relay up on {URI}. Running checks...\n")
        asyncio.run(run_checks())
        print(f"\nAll {_passed} checks passed.")
        return 0
    except AssertionError:
        print("\nTEST FAILED.")
        return 1
    finally:
        relay.terminate()
        try:
            relay.wait(timeout=3)
        except Exception:
            relay.kill()


if __name__ == "__main__":
    sys.exit(main())
