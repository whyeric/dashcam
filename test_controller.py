import asyncio
import json
import websockets

async def test():
    async with websockets.connect("ws://localhost:8765") as ws:
        # Test 1: Move left
        print("Sending: LEFT press")
        await ws.send(json.dumps({"action": "press", "key": "left"}))
        await asyncio.sleep(0.5)
        
        print("Sending: LEFT release")
        await ws.send(json.dumps({"action": "release", "key": "left"}))
        await asyncio.sleep(0.5)
        
        # Test 2: Jump
        print("Sending: SPACE press (jump)")
        await ws.send(json.dumps({"action": "press", "key": "space"}))
        await asyncio.sleep(0.5)
        
        print("Sending: SPACE release")
        await ws.send(json.dumps({"action": "release", "key": "space"}))
        await asyncio.sleep(0.5)
        
        # Test 3: Move right
        print("Sending: RIGHT press")
        await ws.send(json.dumps({"action": "press", "key": "right"}))
        await asyncio.sleep(0.5)
        
        print("Sending: RIGHT release")
        await ws.send(json.dumps({"action": "release", "key": "right"}))
        await asyncio.sleep(0.5)
        
        # Test 4: Roll
        print("Sending: DOWN press (roll)")
        await ws.send(json.dumps({"action": "press", "key": "down"}))
        await asyncio.sleep(0.5)
        
        print("Sending: DOWN release")
        await ws.send(json.dumps({"action": "release", "key": "down"}))
        await asyncio.sleep(0.5)
        
        print("All test inputs sent successfully!")

asyncio.run(test())
