# test_display_affinity2.py — creates a plain native Win32 window in THIS
# same process (no Tkinter at all) and tries SetWindowDisplayAffinity on it.
# This isolates: is the error-87 problem specific to Tkinter's window, or
# does it happen even for the simplest possible same-process window?
#
# Run: python test_display_affinity2.py

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _wndproc(hwnd, msg, wparam, lparam):
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


WNDPROC_INSTANCE = WNDPROC(_wndproc)

user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL

hInstance = kernel32.GetModuleHandleW(None)

wc = WNDCLASSW()
wc.style = 0
wc.lpfnWndProc = WNDPROC_INSTANCE
wc.cbClsExtra = 0
wc.cbWndExtra = 0
wc.hInstance = hInstance
wc.hIcon = None
wc.hCursor = None
wc.hbrBackground = None
wc.lpszMenuName = None
wc.lpszClassName = "RawTestWindowClass"

atom = user32.RegisterClassW(ctypes.byref(wc))
if not atom:
    err = ctypes.get_last_error()
    print(f"RegisterClassW failed: {err}: {ctypes.FormatError(err)}")
    raise SystemExit(1)

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000

ctypes.set_last_error(0)
hwnd = user32.CreateWindowExW(
    0, "RawTestWindowClass", "RawTest", WS_POPUP | WS_VISIBLE,
    0, 0, 200, 200, None, None, hInstance, None,
)

if not hwnd:
    err = ctypes.get_last_error()
    print(f"CreateWindowExW failed: {err}: {ctypes.FormatError(err)}")
else:
    print(f"Raw same-process window HWND = {hwnd}")
    ctypes.set_last_error(0)
    ok = user32.SetWindowDisplayAffinity(hwnd, 0x11)
    if ok:
        print("SUCCESS — worked on a raw same-process window.")
        print("=> The bug is specific to how Tkinter creates its window, not a system-wide block.")
    else:
        err = ctypes.get_last_error()
        print(f"FAILED — error {err}: {ctypes.FormatError(err)}")
        print("=> Fails even for the simplest possible same-process window — this is system-wide.")
    user32.DestroyWindow(hwnd)
