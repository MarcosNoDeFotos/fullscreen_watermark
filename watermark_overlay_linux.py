import argparse
import os
import re
import signal
import sys
import threading
import ctypes
import ctypes.util
from dataclasses import dataclass

from PIL import Image


_IMAGE_ID_RE = re.compile(r"^(?P<prefix>.+)_(?P<id>\d+)$")
#Ajuste en px por si la imagen se corta por abajo
TOP_TILING = 30

def _next_image_by_id(current_image_path: str) -> str | None:
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

def _load_and_fit_image(image_path: str, screen_w: int, screen_h: int) -> Image.Image:
    img = Image.open(image_path).convert("RGBA")

    # Cover (sin bandas)
    scale = max(screen_w / img.width, screen_h / img.height)
    new_w = max(1, int(img.width * scale))
    new_h = max(1, int(img.height * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)

    left = (new_w - screen_w) // 2
    top = (new_h - screen_h) // 2 + TOP_TILING
    img = img.crop((left, top, left + screen_w, top + screen_h))
    return img


def _pil_to_qimage_rgba(img: Image.Image):
    # Import tardío para que --list-monitors funcione si falta Qt
    from PySide6 import QtGui

    if img.mode != "RGBA":
        img = img.convert("RGBA")

    data = img.tobytes("raw", "RGBA")
    qimg = QtGui.QImage(data, img.width, img.height, QtGui.QImage.Format_RGBA8888)
    # Copia profunda para que el buffer Python pueda liberarse
    return qimg.copy()


def _try_x11_click_through(window_id: int) -> bool:
    """Hace la ventana click-through en X11 usando XShape (input region vacío)."""

    if os.environ.get("XDG_SESSION_TYPE", "").lower() != "x11":
        return False

    x11_path = ctypes.util.find_library("X11") or "libX11.so.6"
    xext_path = ctypes.util.find_library("Xext") or "libXext.so.6"

    try:
        libX11 = ctypes.CDLL(x11_path)
        libXext = ctypes.CDLL(xext_path)
    except OSError:
        return False

    Display_p = ctypes.c_void_p
    Window = ctypes.c_ulong

    libX11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    libX11.XOpenDisplay.restype = Display_p
    libX11.XCloseDisplay.argtypes = [Display_p]
    libX11.XCloseDisplay.restype = ctypes.c_int
    libX11.XFlush.argtypes = [Display_p]
    libX11.XFlush.restype = ctypes.c_int

    # int XShapeCombineRectangles(Display *dpy, Window dest, int destKind,
    #   int xOff, int yOff, XRectangle *rectangles, int n_rectangles,
    #   int op, int ordering);
    libXext.XShapeCombineRectangles.argtypes = [
        Display_p,
        Window,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
    ]
    libXext.XShapeCombineRectangles.restype = ctypes.c_int

    dpy = libX11.XOpenDisplay(None)
    if not dpy:
        return False

    try:
        ShapeInput = 2
        ShapeSet = 0
        Unsorted = 0

        # Region de input vacía => click-through real
        libXext.XShapeCombineRectangles(
            dpy,
            Window(int(window_id)),
            ShapeInput,
            0,
            0,
            None,
            0,
            ShapeSet,
            Unsorted,
        )
        libX11.XFlush(dpy)
        return True
    finally:
        libX11.XCloseDisplay(dpy)


@dataclass(frozen=True)
class ScreenInfo:
    index: int
    name: str
    x: int
    y: int
    width: int
    height: int
    is_primary: bool


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Marca de agua fullscreen click-through (Linux/X11) con hotkeys globales."
    )
    parser.add_argument(
        "--image",
        "-i",
        default="images/example_0.png",
        help="Ruta a la imagen base (debe terminar en _0, _1, etc. para poder avanzar).",
    )
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
        default=1.0,
        help="Opacidad global (0..1). Se aplica a la ventana completa.",
    )
    args = parser.parse_args()

    image_path = os.path.abspath(args.image)
    if not os.path.isfile(image_path):
        print(f"No existe la imagen: {image_path}", file=sys.stderr)
        return 2

    try:
        from PySide6 import QtCore, QtGui, QtWidgets
    except ModuleNotFoundError:
        print(
            "Falta PySide6. Instala dependencias en Kali:\n"
            "  pip install -r requirements.txt\n",
            file=sys.stderr,
        )
        return 3

    # Qt app
    app = QtWidgets.QApplication(sys.argv)

    screens = app.screens()
    screen_infos: list[ScreenInfo] = []
    for i, s in enumerate(screens):
        g = s.geometry()
        screen_infos.append(
            ScreenInfo(
                index=i,
                name=s.name(),
                x=int(g.x()),
                y=int(g.y()),
                width=int(g.width()),
                height=int(g.height()),
                is_primary=(s == app.primaryScreen()),
            )
        )

    if args.list_monitors:
        for si in screen_infos:
            primary = " (PRIMARY)" if si.is_primary else ""
            print(f"[{si.index}] {si.name}{primary}: {si.width}x{si.height} @ ({si.x},{si.y})")
        return 0

    chosen: ScreenInfo | None = None
    if screen_infos:
        if args.monitor < 0:
            chosen = next((s for s in screen_infos if s.is_primary), screen_infos[0])
        elif 0 <= args.monitor < len(screen_infos):
            chosen = screen_infos[args.monitor]
        else:
            print(f"Monitor inválido: {args.monitor}. Usa --list-monitors.", file=sys.stderr)
            return 2

    if chosen is None:
        print("No se han podido enumerar monitores.", file=sys.stderr)
        return 2

    opacity = max(0.0, min(1.0, float(args.opacity)))

    # Overlay window
    flags = QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.Tool
    # Click-through (Qt6)
    if hasattr(QtCore.Qt, "WindowTransparentForInput"):
        flags |= QtCore.Qt.WindowTransparentForInput

    # En PySide6, QWidget no acepta keyword 'flags'. Se pasan como segundo argumento posicional.
    win = QtWidgets.QWidget(None, flags)
    # Asegurar flags/atributos de click-through también tras construir el QWidget.
    # En algunos setups no basta con pasarlo en el constructor.
    try:
        if hasattr(QtCore.Qt, "WindowTransparentForInput"):
            win.setWindowFlags(win.windowFlags() | QtCore.Qt.WindowTransparentForInput)
    except Exception:
        pass

    win.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
    win.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
    win.setFocusPolicy(QtCore.Qt.NoFocus)
    win.setWindowOpacity(opacity)
    win.setGeometry(chosen.x, chosen.y, chosen.width, chosen.height)

    label = QtWidgets.QLabel(win)
    label.setGeometry(0, 0, chosen.width, chosen.height)
    label.setScaledContents(True)
    label.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
    label.setFocusPolicy(QtCore.Qt.NoFocus)

    current_image_path = image_path

    def _render(path: str) -> None:
        img = _load_and_fit_image(path, chosen.width, chosen.height)
        qimg = _pil_to_qimage_rgba(img)
        pix = QtGui.QPixmap.fromImage(qimg)
        label.setPixmap(pix)

    _render(current_image_path)

    # Hotkeys (globales) con pynput
    class HotkeyBridge(QtCore.QObject):
        next_image = QtCore.Signal()
        exit_overlay = QtCore.Signal()

    bridge = HotkeyBridge()

    def _advance_image() -> None:
        nonlocal current_image_path
        nxt = _next_image_by_id(current_image_path)
        if nxt is None:
            return
        _render(nxt)
        current_image_path = nxt

    bridge.next_image.connect(_advance_image)
    bridge.exit_overlay.connect(app.quit)

    def _start_hotkey_listener() -> None:
        try:
            from pynput import keyboard
        except ModuleNotFoundError:
            print(
                "Falta pynput para hotkeys globales. Instala:\n"
                "  pip install -r requirements.txt\n",
                file=sys.stderr,
            )
            bridge.exit_overlay.emit()
            return

        pressed: set[object] = set()

        def _is_down(key_obj) -> bool:
            return key_obj in pressed

        def on_press(key):
            pressed.add(key)

            # Ctrl+Alt+W
            if (
                (_is_down(keyboard.Key.ctrl) or _is_down(keyboard.Key.ctrl_l) or _is_down(keyboard.Key.ctrl_r))
                and (_is_down(keyboard.Key.alt) or _is_down(keyboard.Key.alt_l) or _is_down(keyboard.Key.alt_r))
                and (key == keyboard.KeyCode.from_char("w") or key == keyboard.KeyCode.from_char("W"))
            ):
                bridge.exit_overlay.emit()

            # Ctrl+Shift+Right
            if (
                (_is_down(keyboard.Key.ctrl) or _is_down(keyboard.Key.ctrl_l) or _is_down(keyboard.Key.ctrl_r))
                and (_is_down(keyboard.Key.shift) or _is_down(keyboard.Key.shift_l) or _is_down(keyboard.Key.shift_r))
                and key == keyboard.Key.right
            ):
                bridge.next_image.emit()

        def on_release(key):
            try:
                pressed.remove(key)
            except KeyError:
                pass

        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            listener.join()

    threading.Thread(target=_start_hotkey_listener, daemon=True).start()

    # Ctrl+C / SIGINT
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    timer = QtCore.QTimer()
    timer.start(200)
    timer.timeout.connect(lambda: None)

    win.show()

    # Aplicar click-through X11 real (más fiable que los flags de Qt en algunos WM)
    try:
        app.processEvents()
        _try_x11_click_through(int(win.winId()))
    except Exception:
        pass
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
