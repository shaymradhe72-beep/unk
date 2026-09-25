# test_display_affinity.py — isolates whether SetWindowDisplayAffinity works
# AT ALL on this PC, independent of our Tkinter overlay.
#
# Steps:
#   1. Open Notepad (a fresh, untitled one) and leave it open.
#   2. Run: python test_display_affinity.py
#
import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowDisplayAffinity.restype = wintypes.BOOL

WDA_EXCLUDEFROMCAPTURE = 0x11

# Try a couple of common Notepad title variants
hwnd = None
for title in ("Untitled - Notepad", "*Untitled - Notepad"):
    h = user32.FindWindowW(None, title)
    if h:
        hwnd = h
        break

if not hwnd:
    print("Could not find a Notepad window. Open a fresh, unsaved Notepad window and try again.")
else:
    print(f"Notepad HWND = {hwnd}")
    ctypes.set_last_error(0)
    ok = user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    if ok:
        print("SUCCESS — SetWindowDisplayAffinity worked on this system.")
        affinity = wintypes.DWORD(0)
        user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity))
        print(f"Confirmed affinity now set to: {hex(affinity.value)}")
        print("Try taking a screenshot (Win+Shift+S) — Notepad should be MISSING from it,")
        print("while you can still see it normally on your actual screen.")
    else:
        err = ctypes.get_last_error()
        print(f"FAILED — error {err}: {ctypes.FormatError(err)}")
        print("This confirms the issue is system-wide, not specific to our Tkinter window.")
