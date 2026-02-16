import argparse
import os
import sys
import ctypes
import threading
import time
import re
import queue
from dataclasses import dataclass

from ctypes import wintypes

LRESULT = ctypes.c_ssize_t

from PIL import Image
from PIL import ImageChops


def _require_windows() -> None:
    if os.name != "nt":
        raise SystemExit("Este script está pensado para Windows (usa estilos de ventana Win32).")


def _try_set_dpi_awareness() -> None:
    """Evita offsets/tamaños incorrectos en setups con escalado (DPI)."""
    try:
        # Windows 8.1+
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass

    try:
        # Fallback (Windows 7/8)
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


@dataclass(frozen=True)
class Monitor:
    index: int
    left: int
    top: int
    right: int
    bottom: int
    is_primary: bool
    device: str

    @property
    def width(self) -> int:
        return int(self.right - self.left)

    @property
    def height(self) -> int:
        return int(self.bottom - self.top)


def _list_monitors() -> list[Monitor]:
    """Lista monitores con coordenadas absolutas del escritorio virtual."""
    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", ctypes.c_ulong),
            ("szDevice", ctypes.c_wchar * 32),
        ]

    MONITORINFOF_PRIMARY = 0x00000001

    monitors: list[Monitor] = []

    MonitorEnumProc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(RECT), ctypes.c_long)

    def _callback(hMonitor, hdcMonitor, lprcMonitor, dwData) -> int:
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hMonitor, ctypes.byref(info)):
            return 1
        rc = info.rcMonitor
        is_primary = bool(int(info.dwFlags) & MONITORINFOF_PRIMARY)
        device = str(info.szDevice)
        monitors.append(
            Monitor(
                index=-1,
                left=int(rc.left),
                top=int(rc.top),
                right=int(rc.right),
                bottom=int(rc.bottom),
                is_primary=is_primary,
                device=device,
            )
        )
        return 1

    if not user32.EnumDisplayMonitors(None, None, MonitorEnumProc(_callback), 0):
        return []

    # Orden: primario primero, luego por posición.
    monitors.sort(key=lambda m: (not m.is_primary, m.left, m.top))
    return [Monitor(i, m.left, m.top, m.right, m.bottom, m.is_primary, m.device) for i, m in enumerate(monitors)]


def _get_virtual_screen_geometry() -> tuple[int, int, int, int]:
    """(x, y, w, h) del escritorio virtual (todos los monitores)."""
    user32 = ctypes.windll.user32
    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79
    x = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    y = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    w = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    h = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    if w <= 0 or h <= 0:
        return (0, 0, 0, 0)
    return (x, y, w, h)


def _start_global_exit_hotkey(root) -> None:
    """Registra una hotkey global (Ctrl+Alt+W) para cerrar el overlay."""

    def worker() -> None:
        user32 = ctypes.windll.user32

        MOD_ALT = 0x0001
        MOD_CONTROL = 0x0002
        VK_W = 0x57
        WM_HOTKEY = 0x0312
        HOTKEY_ID = 1

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("message", ctypes.c_uint32),
                ("wParam", ctypes.c_void_p),
                ("lParam", ctypes.c_void_p),
                ("time", ctypes.c_uint32),
                ("pt_x", ctypes.c_long),
                ("pt_y", ctypes.c_long),
            ]

        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT, VK_W):
            return

        try:
            msg = MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if int(msg.message) == WM_HOTKEY and int(msg.wParam) == HOTKEY_ID:
                    root.after(0, root.destroy)
                    break
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    threading.Thread(target=worker, daemon=True).start()


def _start_global_hotkeys(event_queue: "queue.SimpleQueue[str]") -> None:
    """Registra hotkeys globales:

    - Ctrl+Alt+W: cerrar overlay
    - Ctrl+Shift+Right: siguiente imagen (id+1)
    """

    def worker() -> None:
        user32 = ctypes.windll.user32

        MOD_ALT = 0x0001
        MOD_CONTROL = 0x0002
        MOD_SHIFT = 0x0004

        VK_W = 0x57
        VK_RIGHT = 0x27
        WM_HOTKEY = 0x0312

        HOTKEY_EXIT = 1
        HOTKEY_NEXT = 2

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("message", ctypes.c_uint32),
                ("wParam", ctypes.c_void_p),
                ("lParam", ctypes.c_void_p),
                ("time", ctypes.c_uint32),
                ("pt_x", ctypes.c_long),
                ("pt_y", ctypes.c_long),
            ]

        ok_exit = bool(user32.RegisterHotKey(None, HOTKEY_EXIT, MOD_CONTROL | MOD_ALT, VK_W))
        ok_next = bool(user32.RegisterHotKey(None, HOTKEY_NEXT, MOD_CONTROL | MOD_SHIFT, VK_RIGHT))
        if not (ok_exit or ok_next):
            return

        try:
            msg = MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if int(msg.message) != WM_HOTKEY:
                    continue

                hotkey_id = int(msg.wParam)
                if hotkey_id == HOTKEY_EXIT:
                    event_queue.put("exit")
                    break
                if hotkey_id == HOTKEY_NEXT:
                    event_queue.put("next")
        finally:
            if ok_exit:
                user32.UnregisterHotKey(None, HOTKEY_EXIT)
            if ok_next:
                user32.UnregisterHotKey(None, HOTKEY_NEXT)

    threading.Thread(target=worker, daemon=True).start()


def _make_click_through(hwnd: int) -> None:
    """Hace que la ventana ignore el mouse (click-through) y quede arriba.

    Implementado con ctypes (sin pywin32).
    """

    from ctypes import wintypes

    user32 = ctypes.windll.user32

    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080

    HWND_TOPMOST = wintypes.HWND(-1)
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    SWP_FRAMECHANGED = 0x0020


    # Declare signatures to avoid ctypes guessing/conversion issues.
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.INT, wintypes.INT, wintypes.INT, wintypes.INT, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL


    if ctypes.sizeof(ctypes.c_void_p) == 8:
        GetWindowLongPtrW = user32.GetWindowLongPtrW
        SetWindowLongPtrW = user32.SetWindowLongPtrW
        GetWindowLongPtrW.argtypes = [wintypes.HWND, wintypes.INT]
        GetWindowLongPtrW.restype = ctypes.c_ssize_t
        SetWindowLongPtrW.argtypes = [wintypes.HWND, wintypes.INT, ctypes.c_ssize_t]
        SetWindowLongPtrW.restype = ctypes.c_ssize_t
    else:
        GetWindowLongPtrW = user32.GetWindowLongW
        SetWindowLongPtrW = user32.SetWindowLongW
        GetWindowLongPtrW.argtypes = [wintypes.HWND, wintypes.INT]
        GetWindowLongPtrW.restype = wintypes.LONG
        SetWindowLongPtrW.argtypes = [wintypes.HWND, wintypes.INT, wintypes.LONG]
        SetWindowLongPtrW.restype = wintypes.LONG

    hwnd_w = wintypes.HWND(hwnd)
    ex_style = int(GetWindowLongPtrW(hwnd_w, GWL_EXSTYLE) or 0)
    ex_style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
    SetWindowLongPtrW(hwnd_w, GWL_EXSTYLE, ex_style)

    # Apply style change + keep topmost without stealing focus.
    user32.SetWindowPos(
        hwnd_w,
        HWND_TOPMOST,
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED,
    )


_WNDPROCS: list[object] = []


def _create_layered_popup_window(x: int, y: int, width: int, height: int) -> int:
    """Crea una ventana Win32 WS_EX_LAYERED + click-through en (x,y) tamaño (w,h)."""

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_TOPMOST = 0x00000008

    WS_POPUP = 0x80000000

    SW_SHOWNOACTIVATE = 4

    WM_DESTROY = 0x0002
    WM_CLOSE = 0x0010

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.PostQuitMessage.argtypes = [wintypes.INT]
    user32.PostQuitMessage.restype = None

    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    def _wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    wndproc = WNDPROC(_wndproc)
    _WNDPROCS.append(wndproc)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HCURSOR),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.UpdateWindow.argtypes = [wintypes.HWND]
    user32.UpdateWindow.restype = wintypes.BOOL
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL

    class_name = "WatermarkOverlayWindow"
    hinstance = kernel32.GetModuleHandleW(None)

    wc = WNDCLASSW()
    wc.style = 0
    wc.lpfnWndProc = wndproc
    wc.cbClsExtra = 0
    wc.cbWndExtra = 0
    wc.hInstance = hinstance
    wc.hIcon = None
    wc.hCursor = None
    wc.hbrBackground = None
    wc.lpszMenuName = None
    wc.lpszClassName = class_name

    # RegisterClassW fails if already registered; ignore that case.
    atom = user32.RegisterClassW(ctypes.byref(wc))
    if not atom:
        err = ctypes.get_last_error()
        # 1410 = ERROR_CLASS_ALREADY_EXISTS
        if err != 1410:
            raise OSError(err, ctypes.FormatError(err))

    ex_style = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
    style = WS_POPUP

    hwnd = user32.CreateWindowExW(ex_style, class_name, "", style, x, y, width, height, None, None, hinstance, None)
    if not hwnd:
        err = ctypes.get_last_error()
        raise OSError(err, ctypes.FormatError(err))

    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    user32.UpdateWindow(hwnd)
    return int(hwnd)


def _run_win32_loop(hwnd: int, on_next_image) -> None:
    """Loop Win32: hotkeys + Ctrl+C -> WM_CLOSE."""

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    VK_W = 0x57
    VK_RIGHT = 0x27

    WM_HOTKEY = 0x0312
    WM_CLOSE = 0x0010

    HOTKEY_EXIT = 1
    HOTKEY_NEXT = 2

    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM),
            ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD),
            ("pt", _POINT),
        ]

    user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.restype = LRESULT

    # Ctrl+C handler -> close window
    CTRL_C_EVENT = 0
    HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def _ctrl_handler(ctrl_type: int) -> int:
        if int(ctrl_type) == CTRL_C_EVENT:
            user32.PostMessageW(wintypes.HWND(hwnd), WM_CLOSE, 0, 0)
            return 1
        return 0

    ctrl_handler = HandlerRoutine(_ctrl_handler)
    _WNDPROCS.append(ctrl_handler)

    kernel32.SetConsoleCtrlHandler.argtypes = [HandlerRoutine, wintypes.BOOL]
    kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
    kernel32.SetConsoleCtrlHandler(ctrl_handler, True)

    ok_exit = bool(user32.RegisterHotKey(None, HOTKEY_EXIT, MOD_CONTROL | MOD_ALT, VK_W))
    ok_next = bool(user32.RegisterHotKey(None, HOTKEY_NEXT, MOD_CONTROL | MOD_SHIFT, VK_RIGHT))

    try:
        msg = MSG()
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0:
                break
            if r == -1:
                err = ctypes.get_last_error()
                raise OSError(err, ctypes.FormatError(err))

            if int(msg.message) == WM_HOTKEY:
                hotkey_id = int(msg.wParam)
                if hotkey_id == HOTKEY_EXIT:
                    user32.PostMessageW(wintypes.HWND(hwnd), WM_CLOSE, 0, 0)
                elif hotkey_id == HOTKEY_NEXT:
                    on_next_image()
                continue

            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        if ok_exit:
            user32.UnregisterHotKey(None, HOTKEY_EXIT)
        if ok_next:
            user32.UnregisterHotKey(None, HOTKEY_NEXT)
        kernel32.SetConsoleCtrlHandler(ctrl_handler, False)


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte), ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


class _BITMAPV5HEADER(ctypes.Structure):
    _fields_ = [
        ("bV5Size", ctypes.c_uint32),
        ("bV5Width", ctypes.c_long),
        ("bV5Height", ctypes.c_long),
        ("bV5Planes", ctypes.c_uint16),
        ("bV5BitCount", ctypes.c_uint16),
        ("bV5Compression", ctypes.c_uint32),
        ("bV5SizeImage", ctypes.c_uint32),
        ("bV5XPelsPerMeter", ctypes.c_long),
        ("bV5YPelsPerMeter", ctypes.c_long),
        ("bV5ClrUsed", ctypes.c_uint32),
        ("bV5ClrImportant", ctypes.c_uint32),
        ("bV5RedMask", ctypes.c_uint32),
        ("bV5GreenMask", ctypes.c_uint32),
        ("bV5BlueMask", ctypes.c_uint32),
        ("bV5AlphaMask", ctypes.c_uint32),
        ("bV5CSType", ctypes.c_uint32),
        ("bV5Endpoints", ctypes.c_byte * 36),
        ("bV5GammaRed", ctypes.c_uint32),
        ("bV5GammaGreen", ctypes.c_uint32),
        ("bV5GammaBlue", ctypes.c_uint32),
        ("bV5Intent", ctypes.c_uint32),
        ("bV5ProfileData", ctypes.c_uint32),
        ("bV5ProfileSize", ctypes.c_uint32),
        ("bV5Reserved", ctypes.c_uint32),
    ]


def _premultiply_alpha(img: Image.Image) -> Image.Image:
    """UpdateLayeredWindow espera RGB pre-multiplicado por alpha."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    r, g, b, a = img.split()
    r = ImageChops.multiply(r, a)
    g = ImageChops.multiply(g, a)
    b = ImageChops.multiply(b, a)
    return Image.merge("RGBA", (r, g, b, a))


def _set_layered_window_image(hwnd: int, img: Image.Image, x: int, y: int) -> None:
    """Pinta img (RGBA) en la ventana layered con alpha per-pixel."""

    from ctypes import wintypes

    # Use use_last_error=True for meaningful diagnostics.
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    ULW_ALPHA = 0x00000002
    AC_SRC_OVER = 0x00
    AC_SRC_ALPHA = 0x01
    DIB_RGB_COLORS = 0
    BI_BITFIELDS = 3

    # Ensure proper signatures (important on 64-bit for correct alpha/pointers)
    user32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND,
        wintypes.HDC,
        ctypes.POINTER(_POINT),
        ctypes.POINTER(_SIZE),
        wintypes.HDC,
        ctypes.POINTER(_POINT),
        wintypes.COLORREF,
        ctypes.POINTER(_BLENDFUNCTION),
        wintypes.DWORD,
    ]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL

    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = wintypes.INT

    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL

    # 2nd param is BITMAPINFO* but we'll pass a BITMAPV5HEADER buffer (size indicates header type).
    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC,
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP

    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL

    img = _premultiply_alpha(img)
    width, height = img.size
    src_bytes = img.tobytes("raw", "BGRA")

    # Screen DC
    hdc_screen = user32.GetDC(wintypes.HWND(0))
    if not hdc_screen:
        err = ctypes.get_last_error()
        print(f"Win32 GetDC failed: {err} {ctypes.FormatError(err)}", file=sys.stderr)
        return

    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    if not hdc_mem:
        err = ctypes.get_last_error()
        print(f"Win32 CreateCompatibleDC failed: {err} {ctypes.FormatError(err)}", file=sys.stderr)
        user32.ReleaseDC(0, hdc_screen)
        return

    bvh = _BITMAPV5HEADER()
    bvh.bV5Size = ctypes.sizeof(_BITMAPV5HEADER)
    bvh.bV5Width = width
    bvh.bV5Height = -height  # top-down
    bvh.bV5Planes = 1
    bvh.bV5BitCount = 32
    bvh.bV5Compression = BI_BITFIELDS
    bvh.bV5SizeImage = width * height * 4
    # BGRA (little-endian) masks
    bvh.bV5RedMask = 0x00FF0000
    bvh.bV5GreenMask = 0x0000FF00
    bvh.bV5BlueMask = 0x000000FF
    bvh.bV5AlphaMask = 0xFF000000

    ppv_bits = ctypes.c_void_p()
    hbitmap = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bvh), DIB_RGB_COLORS, ctypes.byref(ppv_bits), wintypes.HANDLE(0), 0)
    if not hbitmap:
        err = ctypes.get_last_error()
        print(f"Win32 CreateDIBSection failed: {err} {ctypes.FormatError(err)}", file=sys.stderr)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(wintypes.HWND(0), hdc_screen)
        return

    old_obj = gdi32.SelectObject(hdc_mem, hbitmap)
    try:
        ctypes.memmove(ppv_bits, src_bytes, len(src_bytes))

        pt_dst = _POINT(x, y)
        size = _SIZE(width, height)
        pt_src = _POINT(0, 0)
        blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)

        ok = user32.UpdateLayeredWindow(
            wintypes.HWND(hwnd),
            hdc_screen,
            ctypes.byref(pt_dst),
            ctypes.byref(size),
            hdc_mem,
            ctypes.byref(pt_src),
            0,
            ctypes.byref(blend),
            ULW_ALPHA,
        )
        if not ok:
            err = ctypes.get_last_error()
            print(f"Win32 UpdateLayeredWindow failed: {err} {ctypes.FormatError(err)}", file=sys.stderr)
    finally:
        if old_obj:
            gdi32.SelectObject(hdc_mem, old_obj)
        gdi32.DeleteObject(hbitmap)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(wintypes.HWND(0), hdc_screen)


def _load_and_fit_image(image_path: str, screen_w: int, screen_h: int, opacity: float) -> Image.Image:
    img = Image.open(image_path).convert("RGBA")

    # Ajuste para cubrir toda la pantalla (sin bandas). Si prefieres "contain", cambia a min().
    scale = max(screen_w / img.width, screen_h / img.height)
    new_w = max(1, int(img.width * scale))
    new_h = max(1, int(img.height * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)

    # Recorte centrado al tamaño exacto de pantalla
    left = (new_w - screen_w) // 2
    top = (new_h - screen_h) // 2
    img = img.crop((left, top, left + screen_w, top + screen_h))

    # Opacidad global de la imagen (0..1)
    opacity = max(0.0, min(1.0, opacity))
    if opacity < 1.0:
        r, g, b, a = img.split()
        a = a.point(lambda px: int(px * opacity))
        img = Image.merge("RGBA", (r, g, b, a))

    return img


_IMAGE_ID_RE = re.compile(r"^(?P<prefix>.+)_(?P<id>\d+)$")


def _next_image_by_id(current_image_path: str) -> str | None:
    """Si el nombre acaba en _<id>, intenta devolver el mismo nombre con id+1 (si existe)."""
    directory, filename = os.path.split(current_image_path)
    stem, ext = os.path.splitext(filename)
    match = _IMAGE_ID_RE.match(stem)
    if not match:
        return None

    prefix = match.group("prefix")
    current_id = int(match.group("id"))
    next_stem = f"{prefix}_{current_id + 1}"
    prev_stem = f"{prefix}_{current_id - 1}"
    candidate = os.path.join(directory, next_stem + ext)
    #el siguiente id será +1, pero si no existe, también pruebo con el id anterior por si el usuario se ha saltado un número (ej: 0,1,3). Si ninguno existe, no cambio la imagen.
    if os.path.isfile(candidate):
        return candidate
    else:
        candidate_prev = os.path.join(directory, prev_stem + ext)
        if os.path.isfile(candidate_prev):
            return candidate_prev
    return None


def main() -> int:
    _require_windows()
    _try_set_dpi_awareness()

    parser = argparse.ArgumentParser(
        description="Muestra una marca de agua a pantalla completa y permite click por detrás (Windows)."
    )
    parser.add_argument("--image", "-i", required=False, help="Ruta a la imagen (PNG/JPG, etc.).", default="images/example_0.png")
    parser.add_argument(
        "--monitor",
        "-m",
        type=int,
        default=-1,
        help="Índice de monitor donde mostrar el overlay (usa --list-monitors). Default: primario",
    )
    parser.add_argument(
        "--list-monitors",
        action="store_true",
        help="Lista monitores detectados y sale.",
    )
    parser.add_argument(
        "--opacity",
        "-o",
        type=float,
        default=0.25,
        help="Opacidad global de la marca de agua (0.0 a 1.0). Default: 0.25",
    )
    args = parser.parse_args()

    monitors = _list_monitors()
    if args.list_monitors:
        if not monitors:
            print("No se han podido enumerar monitores.")
        else:
            for m in monitors:
                primary = " (PRIMARY)" if m.is_primary else ""
                print(f"[{m.index}] {m.device}{primary}: {m.width}x{m.height} @ ({m.left},{m.top})")
        return 0

    image_path = os.path.abspath(args.image)
    if not os.path.isfile(image_path):
        print(f"No existe la imagen: {image_path}", file=sys.stderr)
        return 2

    # Tamaño: un único monitor (seleccionable)
    chosen = None
    if monitors:
        if args.monitor < 0:
            chosen = next((m for m in monitors if m.is_primary), monitors[0])
        elif 0 <= args.monitor < len(monitors):
            chosen = monitors[args.monitor]
        else:
            print(f"Monitor inválido: {args.monitor}. Usa --list-monitors.", file=sys.stderr)
            return 2

    if chosen is not None:
        vx, vy = chosen.left, chosen.top
        screen_w, screen_h = chosen.width, chosen.height
    else:
        # Fallback: escritorio virtual (todos los monitores)
        vx, vy, screen_w, screen_h = _get_virtual_screen_geometry()
        if screen_w <= 0 or screen_h <= 0:
            vx, vy = 0, 0
            user32 = ctypes.windll.user32
            screen_w = int(user32.GetSystemMetrics(0))  # SM_CXSCREEN
            screen_h = int(user32.GetSystemMetrics(1))  # SM_CYSCREEN

    # Crear ventana Win32 layered (sin Tk) y render inicial
    hwnd = _create_layered_popup_window(vx, vy, screen_w, screen_h)
    fitted = _load_and_fit_image(image_path, screen_w, screen_h, args.opacity)
    _set_layered_window_image(hwnd, fitted, vx, vy)

    current_image_path = image_path

    def _advance_image() -> None:
        nonlocal current_image_path
        next_path = _next_image_by_id(current_image_path)
        if next_path is None:
            return

        fitted_next = _load_and_fit_image(next_path, screen_w, screen_h, args.opacity)
        _set_layered_window_image(hwnd, fitted_next, vx, vy)
        current_image_path = next_path

    # Loop Win32: hotkeys + Ctrl+C
    try:
        _run_win32_loop(hwnd, _advance_image)
    except KeyboardInterrupt:
        # Fallback: si el usuario mata la consola
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
