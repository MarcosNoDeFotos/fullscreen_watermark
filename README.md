# Marca de agua fullscreen (Windows + Linux/X11)

Overlay a pantalla completa para mostrar una marca de agua (imagen) **por encima de todo** y con **click-through** (puedes seguir usando las ventanas que hay debajo).

Incluye hotkeys globales:
- `Ctrl+Alt+W` → cerrar el overlay
- `Ctrl+Shift+Right` → cambiar a la siguiente imagen (`*_0.png → *_1.png → ...`)
- `Ctrl+C` en la consola → cerrar (si lo lanzas desde terminal)

## Archivos incluidos en el repositorio
- `watermark_overlay_windows.py`
- `watermark_overlay_linux.py`
- `requirements.txt`
- `images/example_0.png`
- `images/example_1.png`

## Requisitos
- Python 3.x
- Dependencias Python (ver `requirements.txt`):
  - Común: `Pillow`
  - Windows: `pywin32`
  - Linux: `PySide6` + `pynput`

### Nota para Linux (Kali)
- El click-through “real” en Linux está implementado para **X11**.
  - Comprueba: `echo $XDG_SESSION_TYPE` (debería decir `x11`).
  - En Wayland, los hotkeys globales y/o el click-through pueden no funcionar según el entorno.

## Instalación

### Windows
```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

### Kali / Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Uso

### 1) Listar monitores

#### Windows
```powershell
python .\watermark_overlay_windows.py --list-monitors
```

#### Linux
```bash
python3 watermark_overlay_linux.py --list-monitors
```

### 2) Lanzar el overlay en un monitor

#### Windows
```powershell
python .\watermark_overlay_windows.py --image .\images\example_0.png --monitor 0 --opacity 0.25
```

#### Linux (X11)
```bash
python3 watermark_overlay_linux.py --image ./images/example_0.png --monitor 0 --opacity 0.25
```

Parámetros:
- `--image` / `-i`: ruta de la imagen base.
- `--monitor` / `-m`: índice del monitor.
  - `-1` (default) = monitor primario.
- `--opacity` / `-o`: opacidad global (0..1).

## Hotkeys (globales)
- `Ctrl+Alt+W`: cerrar overlay.
- `Ctrl+Shift+Right`: cargar la siguiente imagen por ID.
  - Si estás en `images/example_0.png` intentará `images/example_1.png`.
  - Si no existe `+1`, prueba con `-1` (por si hay saltos de numeración).

## Convención de nombres de imágenes
Para que el cambio de imagen funcione, el nombre debe terminar en `_<id>` (antes de la extensión):
- `example_0.png`, `example_1.png`, `example_2.png`, ...

Puedes usar cualquier prefijo, manteniendo el sufijo numérico:
- `marca_agua_0.png`, `marca_agua_1.png`, ...

## Notas y troubleshooting
- Si el overlay “no deja hacer click” en Linux, casi siempre es porque:
  - No estás en X11 (estás en Wayland), o
  - Tu WM/DE está bloqueando global hotkeys/input shaping.
- En Linux, si no tienes hotkeys:
  - Asegúrate de tener instalado `pynput` (`pip install -r requirements.txt`).
- En Windows, si algo falla al registrar hotkeys globales, prueba a ejecutar la consola como administrador.
