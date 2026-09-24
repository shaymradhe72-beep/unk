# agent.py — runs on PC A (Windows, your personal PC)
#
# Captures the screen, streams JPEG frames to the relay server (server.py on
# PC B) over a WebSocket, and executes mouse/keyboard commands received back
# over that same connection.
#
# Run:
#   pip install aiohttp mss opencv-python numpy pyautogui
#   python agent.py

import asyncio
import json
import time
import mss
import numpy as np
import cv2
import pyautogui
import aiohttp

SERVER_URL = "ws://168.144.73.133:8080/ws/agent"   # <-- put your DigitalOcean IP here
AUTH_TOKEN = "alpha123"  # must match server.py

JPEG_QUALITY = 80
CAPTURE_FPS = 12
SCALE_FACTOR = 0.6   # must match the SCALE_FACTOR in server.py's HTML

pyautogui.FAILSAFE = False  # lab machine — don't emergency-abort on corner-of-screen moves

frame_queue = asyncio.Queue(maxsize=1)  # holds only the newest frame — old ones get dropped


def capture_frame(sct, monitor, encode_params):
    img = np.array(sct.grab(monitor))
    img_bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    if SCALE_FACTOR != 1.0:
        h, w = img_bgr.shape[:2]
        img_bgr = cv2.resize(img_bgr, (int(w * SCALE_FACTOR), int(h * SCALE_FACTOR)),
                              interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode('.jpg', img_bgr, encode_params)
    return encoded.tobytes() if ok else None


async def capture_loop(loop):
    encode_params = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
    frame_interval = 1.0 / CAPTURE_FPS

    with mss.mss() as sct:
        monitor = sct.monitors[1]
        while True:
            start = time.time()
            # mss/cv2 are blocking — run them in a thread so we don't stall the event loop
            data = await loop.run_in_executor(None, capture_frame, sct, monitor, encode_params)
            if data:
                if frame_queue.full():
                    try:
                        frame_queue.get_nowait()  # drop the stale frame, keep only the newest
                    except asyncio.QueueEmpty:
                        pass
                await frame_queue.put(data)

            elapsed = time.time() - start
            if elapsed < frame_interval:
                await asyncio.sleep(frame_interval - elapsed)


# Browser key names -> pyautogui key names, for the ones that differ
KEY_MAP = {
    "Enter": "enter", "Backspace": "backspace", "Tab": "tab",
    "Escape": "esc", "ArrowUp": "up", "ArrowDown": "down",
    "ArrowLeft": "left", "ArrowRight": "right", " ": "space",
    "Shift": "shift", "Control": "ctrl", "Alt": "alt",
    "CapsLock": "capslock", "Delete": "delete",
}


def execute_command(cmd):
    action = cmd.get("action")
    try:
        if action == "click":
            pyautogui.click(cmd["x"], cmd["y"], button=cmd.get("button", "left"))
        elif action == "scroll":
            pyautogui.scroll(cmd["amount"])
        elif action == "keydown":
            key = KEY_MAP.get(cmd["key"], cmd["key"].lower())
            if len(key) == 1 or key in pyautogui.KEYBOARD_KEYS:
                pyautogui.keyDown(key)
        elif action == "keyup":
            key = KEY_MAP.get(cmd["key"], cmd["key"].lower())
            if len(key) == 1 or key in pyautogui.KEYBOARD_KEYS:
                pyautogui.keyUp(key)
    except Exception as e:
        print(f"[-] Command failed: {e}")


async def send_loop(ws):
    while True:
        data = await frame_queue.get()
        await ws.send_bytes(data)


async def control_loop(ws):
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            try:
                cmd = json.loads(msg.data)
                execute_command(cmd)
            except Exception as e:
                print(f"[-] Bad command: {e}")
        elif msg.type == aiohttp.WSMsgType.ERROR:
            break


async def run():
    loop = asyncio.get_event_loop()
    asyncio.create_task(capture_loop(loop))

    url = f"{SERVER_URL}?token={AUTH_TOKEN}"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.ws_connect(url, max_msg_size=20 * 1024 * 1024, heartbeat=20) as ws:
                    print("[+] Connected to relay server")
                    sender = asyncio.create_task(send_loop(ws))
                    receiver = asyncio.create_task(control_loop(ws))
                    done, pending = await asyncio.wait(
                        [sender, receiver], return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                    print("[-] Disconnected from relay server")
            except Exception as e:
                print(f"[-] Connection lost ({e}), retrying in 3s...")
                await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run())
