# agent.py — PC A (Windows) remote-screen / remote-control agent
# =================================================================
# Streams this machine's screen to the relay server (server.py, running on
# PC B) over a single WebSocket, and executes mouse/keyboard commands sent
# back over that same connection. Also supports a "black screen" privacy
# mode — see BLACK SCREEN section below.
#
# Run:
#   pip install aiohttp mss opencv-python numpy pyautogui
#   python agent.py
#
# Optional: drop an agent_config.json next to this script (or the .exe) to
# override any setting below without editing code / rebuilding. Example:
#   {
#     "server_url": "ws://1.2.3.4:8080/ws/agent",
#     "auth_token": "some-long-random-string",
#     "jpeg_quality": 85,
#     "capture_fps": 15,
#     "scale_factor": 1.0
#   }

import sys
import os
import json
import time
import threading
import queue
import asyncio
import ctypes
from ctypes import wintypes

import tkinter as tk
import mss
import numpy as np
import cv2
import pyautogui
import aiohttp


# ---------------------------------------------------------------------------
# LOGGING — a PyInstaller --noconsole build has no stdout/stderr (they're
# None). Any print() call would then crash with "'NoneType' object has no
# attribute 'write'". Since prints sit inside exception handlers for mouse/
# keyboard commands, that crash is exactly what makes clicks silently "stop
# working" partway through a session. Redirect to a log file instead.
# ---------------------------------------------------------------------------

def _base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


if getattr(sys, "frozen", False) and sys.stdout is None:
    _log_path = os.path.join(_base_dir(), "agent_log.txt")
    _log_file = open(_log_path, "a", buffering=1, encoding="utf-8")
    sys.stdout = _log_file
    sys.stderr = _log_file


# ---------------------------------------------------------------------------
# CONFIG — defaults below, optionally overridden by agent_config.json
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "server_url": "ws://168.144.73.133:8080/ws/agent",
    "auth_token": "alpha123",
    "jpeg_quality": 85,
    "capture_fps": 15,
    "scale_factor": 1.0,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(_base_dir(), "agent_config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"[-] Failed to read agent_config.json, using defaults: {e}")
    return cfg


CONFIG = load_config()
SERVER_URL = CONFIG["server_url"]
AUTH_TOKEN = CONFIG["auth_token"]
JPEG_QUALITY = CONFIG["jpeg_quality"]
CAPTURE_FPS = CONFIG["capture_fps"]
SCALE_FACTOR = CONFIG["scale_factor"]

pyautogui.FAILSAFE = False  # lab machine — don't emergency-abort on corner-of-screen moves
pyautogui.PAUSE = 0         # pyautogui defaults to a 0.1s sleep after EVERY call, which
                            # would block the event loop long enough to look like a
                            # dropped connection — see control_loop, which also runs
                            # every command in a background thread as a second safeguard


# ---------------------------------------------------------------------------
# BLACK SCREEN — privacy mode. Blanks PC A's physical display and blocks its
# physical mouse/keyboard, while the remote viewer keeps seeing the real
# screen and keeps full control. Two Windows tricks make this possible:
#
#   1. SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) on the black overlay
#      window makes it invisible to screen-capture APIs (including mss), so
#      the remote viewer's stream shows the real desktop right through it —
#      while a person physically at the monitor just sees solid black.
#
#   2. A low-level keyboard/mouse hook blocks physical input (Windows tags
#      it as "not injected") but explicitly lets pyautogui's synthetic
#      ("injected") input through, so remote control keeps working.
#
# Safety valves, since this can block physical input on your own PC:
#   - Holding Ctrl+Alt+Q physically forces it off immediately.
#   - It auto-disables the moment the relay connection drops.
#   - Ctrl+Alt+Del is handled by Windows below the hook layer and can never
#     be blocked by this — always available to open Task Manager and kill
#     the process as a last resort.
#
# Requires Windows 10 build 2004 (May 2020 Update) or later for the display-
# affinity trick, AND requires this process to be running elevated (as
# Administrator) — both SetWindowsHookExW and SetWindowDisplayAffinity fail
# silently (return FALSE, no exception) when run as a normal user, which
# shows up as: physical keyboard/mouse still working, and/or the remote
# stream going black too instead of showing through. Run the script (or the
# built .exe) as Administrator, or build with `pyinstaller --uac-admin` so
# it always requests elevation automatically.
# ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_KEYUP = 0x0101
WM_SYSKEYUP = 0x0105
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01
WDA_EXCLUDEFROMCAPTURE = 0x11

HHOOK = wintypes.HANDLE
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


LowLevelProc = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, WPARAM, LPARAM)

user32.SetWindowsHookExW.restype = HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, LowLevelProc, wintypes.HINSTANCE, wintypes.DWORD]
user32.CallNextHookEx.restype = ctypes.c_long
user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, WPARAM, LPARAM]
user32.UnhookWindowsHookEx.argtypes = [HHOOK]
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL

blackscreen_state = {"active": False, "window": None}
blackscreen_queue = queue.Queue()   # thread-safe on/off requests, drained on the Tk thread
_hook_handles = {"kb": None, "mouse": None}
_panic_keys_down = set()
PANIC_CTRL = {0x11, 0xA2, 0xA3}    # VK_CONTROL, VK_LCONTROL, VK_RCONTROL
PANIC_ALT = {0x12, 0xA4, 0xA5}     # VK_MENU, VK_LMENU, VK_RMENU
PANIC_Q = 0x51                      # 'Q'


def _keyboard_hook_proc(nCode, wParam, lParam):
    if nCode == 0 and blackscreen_state["active"]:
        kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        if not (kb.flags & LLKHF_INJECTED):  # real physical key, not from pyautogui
            if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                _panic_keys_down.add(kb.vkCode)
                if (_panic_keys_down & PANIC_CTRL) and (_panic_keys_down & PANIC_ALT) \
                        and PANIC_Q in _panic_keys_down:
                    blackscreen_queue.put(False)  # emergency off
            elif wParam in (WM_KEYUP, WM_SYSKEYUP):
                _panic_keys_down.discard(kb.vkCode)
            return 1  # swallow the physical key
    return user32.CallNextHookEx(None, nCode, wParam, lParam)


def _mouse_hook_proc(nCode, wParam, lParam):
    if nCode == 0 and blackscreen_state["active"]:
        ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
        if not (ms.flags & LLMHF_INJECTED):  # real physical mouse, not from pyautogui
            return 1  # swallow the physical click/move
    return user32.CallNextHookEx(None, nCode, wParam, lParam)


_kb_proc_ref = LowLevelProc(_keyboard_hook_proc)      # keep references alive —
_mouse_proc_ref = LowLevelProc(_mouse_hook_proc)      # GC'd callbacks would crash


def _install_hooks():
    # MSDN: hMod is ignored for WH_KEYBOARD_LL / WH_MOUSE_LL — passing NULL is
    # the documented-correct approach and is more portable than
    # GetModuleHandleW(None), which can resolve incorrectly under some Python
    # launcher/stub setups (observed as error 126 "module not found").
    ctypes.set_last_error(0)
    _hook_handles["kb"] = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _kb_proc_ref, None, 0)
    if not _hook_handles["kb"]:
        err = ctypes.get_last_error()
        print(f"[-] Keyboard hook FAILED to install (error {err}: {ctypes.FormatError(err)}). "
              f"Physical keyboard will NOT be blocked.")

    ctypes.set_last_error(0)
    _hook_handles["mouse"] = user32.SetWindowsHookExW(WH_MOUSE_LL, _mouse_proc_ref, None, 0)
    if not _hook_handles["mouse"]:
        err = ctypes.get_last_error()
        print(f"[-] Mouse hook FAILED to install (error {err}: {ctypes.FormatError(err)}). "
              f"Physical mouse will NOT be blocked.")


def _uninstall_hooks():
    for key in ("kb", "mouse"):
        if _hook_handles[key]:
            user32.UnhookWindowsHookEx(_hook_handles[key])
            _hook_handles[key] = None
    _panic_keys_down.clear()


def _apply_display_affinity(win, retry=True):
    hwnd = win.winfo_id()
    ctypes.set_last_error(0)
    ok = user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), WDA_EXCLUDEFROMCAPTURE)
    if not ok:
        err = ctypes.get_last_error()
        if err == 87 and retry:
            # overrideredirect can force Tk to recreate the underlying HWND;
            # winfo_id() right after that swap can be a beat too early on some
            # systems. One short wait + a fresh handle usually resolves it.
            win.update()
            time.sleep(0.05)
            return _apply_display_affinity(win, retry=False)
        return False, err
    return True, 0


def _show_blackscreen(root):
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg="black")
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"{sw}x{sh}+0+0")
    win.update()  # full update, not just update_idletasks() — ensures the real
                  # OS-level window exists (post overrideredirect re-creation)
                  # before we ask Windows for its handle

    print(f"[*] Black overlay HWND = {win.winfo_id()}")
    ok, err = _apply_display_affinity(win)
    if not ok:
        if err == 87:
            print("[-] SetWindowDisplayAffinity FAILED (error 87: parameter is incorrect), "
                  "even after retry. Windows 11 supports this API, so this points to something "
                  "specific to this window/session rather than the OS version. The remote view "
                  "will show black too while black screen is on.")
        else:
            print(f"[-] SetWindowDisplayAffinity FAILED (error {err}: {ctypes.FormatError(err)}). "
                  f"The remote view will show black too instead of the real screen.")

    blackscreen_state["window"] = win
    _install_hooks()
    blackscreen_state["active"] = True
    print("[*] Black screen ON — physical input blocked; Ctrl+Alt+Q forces it off")


def _hide_blackscreen():
    blackscreen_state["active"] = False
    _uninstall_hooks()
    win = blackscreen_state.get("window")
    if win is not None:
        try:
            win.destroy()
        except Exception:
            pass
        blackscreen_state["window"] = None
    print("[*] Black screen OFF")


def ui_thread_main():
    """Hidden Tk root: hosts the blackscreen Toplevel and pumps the Windows
    message loop the low-level input hooks need to fire on this thread."""
    root = tk.Tk()
    root.withdraw()

    def poll():
        try:
            while True:
                want_on = blackscreen_queue.get_nowait()
                if want_on and not blackscreen_state["active"]:
                    _show_blackscreen(root)
                elif not want_on and blackscreen_state["active"]:
                    _hide_blackscreen()
        except queue.Empty:
            pass
        root.after(100, poll)

    root.after(100, poll)
    root.mainloop()


# ---------------------------------------------------------------------------
# SCREEN CAPTURE
# ---------------------------------------------------------------------------

frame_queue = asyncio.Queue(maxsize=1)  # holds only the newest frame — old ones get dropped


def capture_frame(sct, monitor, encode_params):
    img = np.array(sct.grab(monitor))
    img_bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    if SCALE_FACTOR != 1.0:
        h, w = img_bgr.shape[:2]
        img_bgr = cv2.resize(img_bgr, (int(w * SCALE_FACTOR), int(h * SCALE_FACTOR)),
                              interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", img_bgr, encode_params)
    return encoded.tobytes() if ok else None


async def capture_loop(loop):
    encode_params = [
        cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY,
        cv2.IMWRITE_JPEG_SAMPLING_FACTOR, cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444,  # sharper text/edges
    ]
    frame_interval = 1.0 / CAPTURE_FPS

    with mss.MSS() as sct:
        monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
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


# ---------------------------------------------------------------------------
# INPUT CONTROL — mouse/keyboard commands received from the viewer
# ---------------------------------------------------------------------------

# Browser key names -> pyautogui key names, for the ones that differ.
# Anything not listed here just gets lowercased (works for letters, digits,
# and function keys like "F1" -> "f1").
KEY_MAP = {
    "Enter": "enter", "Backspace": "backspace", "Tab": "tab",
    "Escape": "esc", "ArrowUp": "up", "ArrowDown": "down",
    "ArrowLeft": "left", "ArrowRight": "right", " ": "space",
    "Shift": "shift", "Control": "ctrl", "Alt": "alt",
    "CapsLock": "capslock", "Delete": "delete",
}

pressed_keys = set()
pressed_keys_lock = threading.Lock()


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
                with pressed_keys_lock:
                    pressed_keys.add(key)
        elif action == "keyup":
            key = KEY_MAP.get(cmd["key"], cmd["key"].lower())
            if len(key) == 1 or key in pyautogui.KEYBOARD_KEYS:
                pyautogui.keyUp(key)
                with pressed_keys_lock:
                    pressed_keys.discard(key)
        elif action == "blackscreen":
            blackscreen_queue.put(bool(cmd.get("state")))
    except Exception as e:
        print(f"[-] Command failed: {e}")


def release_all_keys():
    """Safety valve: if a keyup ever gets lost (tab loses focus mid-press, a
    dropped message, a disconnect), a modifier like Shift/Ctrl/Alt can be
    left 'stuck' held down — every click after that behaves oddly (e.g.
    Ctrl+Click instead of a plain click). Call this on every disconnect."""
    with pressed_keys_lock:
        keys = list(pressed_keys)
        pressed_keys.clear()
    for k in keys:
        try:
            pyautogui.keyUp(k)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# NETWORKING
# ---------------------------------------------------------------------------

async def send_loop(ws):
    while True:
        data = await frame_queue.get()
        await ws.send_bytes(data)


async def control_loop(ws, loop):
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    cmd = json.loads(msg.data)
                    # Run in executor: pyautogui calls are blocking and must
                    # never stall the event loop (a stall here is what can
                    # make the connection look like it silently dropped).
                    loop.run_in_executor(None, execute_command, cmd)
                except Exception as e:
                    print(f"[-] Bad command: {e}")
            elif msg.type == aiohttp.WSMsgType.ERROR:
                break
    finally:
        release_all_keys()


async def run():
    loop = asyncio.get_event_loop()
    asyncio.create_task(capture_loop(loop))

    url = f"{SERVER_URL}?token={AUTH_TOKEN}"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # No `heartbeat=` — aiohttp's built-in ping/pong heartbeat has
                # known races that can crash with InvalidStateError and drop
                # the connection for no real reason. Our own reconnect loop,
                # plus constant frame traffic keeping the socket alive, is
                # enough on its own.
                async with session.ws_connect(url, max_msg_size=20 * 1024 * 1024) as ws:
                    print("[+] Connected to relay server")
                    # Tell the viewer what scale we're capturing at, so its
                    # coordinate math doesn't rely on a hardcoded constant
                    # that has to be kept in sync by hand.
                    await ws.send_str(json.dumps({"type": "meta", "scale": SCALE_FACTOR}))

                    sender = asyncio.create_task(send_loop(ws))
                    receiver = asyncio.create_task(control_loop(ws, loop))
                    done, pending = await asyncio.wait(
                        [sender, receiver], return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                    # Wait for cancellation to actually finish before looping —
                    # a half-cancelled task can also trigger InvalidStateError.
                    await asyncio.gather(*pending, return_exceptions=True)
                    print("[-] Disconnected from relay server")
            except Exception as e:
                print(f"[-] Connection lost ({e}), retrying in 3s...")
                blackscreen_queue.put(False)  # never leave PC A blacked out if we lose the link
                await asyncio.sleep(3)


if __name__ == "__main__":
    threading.Thread(target=ui_thread_main, daemon=True).start()
    asyncio.run(run())
