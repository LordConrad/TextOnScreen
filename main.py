import sys
import os
import winreg
import tempfile
import traceback
import base64
import ctypes
import json
import io
import subprocess
import urllib.request
import urllib.error
import urllib.parse
import re
import shutil
import importlib
from pathlib import Path
from datetime import datetime


def _get_easyocr_model_dir() -> str:
    """Return a persistent per-user directory for EasyOCR model files."""
    # Windows: use LOCALAPPDATA (survives reboot and temp cleanups)
    base = os.environ.get("LOCALAPPDATA")
    if base:
        model_dir = Path(base) / "TxtOnScrn" / "EasyOCR"
        model_dir.mkdir(parents=True, exist_ok=True)
        return str(model_dir)

    # Other OSes / fallback: use XDG cache if available, otherwise ~/.cache
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        model_dir = Path(xdg_cache) / "TxtOnScrn" / "EasyOCR"
    else:
        model_dir = Path.home() / ".cache" / "TxtOnScrn" / "EasyOCR"
    model_dir.mkdir(parents=True, exist_ok=True)
    return str(model_dir)


def _get_app_data_dir() -> str:
    """Return the persistent per-user app data directory used by TxtOnScrn."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return str(Path(base) / "TxtOnScrn")

    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return str(Path(xdg_cache) / "TxtOnScrn")

    return str(Path.home() / ".cache" / "TxtOnScrn")


def _ocr_worker_main() -> int:
    """CLI entrypoint used by the GUI process to run OCR in a separate process.

    Reads JSON from stdin and writes JSON to stdout.
    Input schema:
      {"image_path": str|null, "png_b64": str|null, "use_greedy": bool}
    Output schema:
      {"ok": true, "text": str} OR {"ok": false, "error": str}
    """
    try:
        raw_in = sys.stdin.buffer.read()
        if not raw_in:
            raise RuntimeError("Missing stdin payload")
        payload = json.loads(raw_in.decode("utf-8", errors="replace"))
        if not isinstance(payload, dict):
            raise RuntimeError("Invalid payload")

        image_path = payload.get("image_path")
        png_b64 = payload.get("png_b64")
        use_greedy = bool(payload.get("use_greedy", False))
        try:
            scale = int(payload.get("scale", 2) or 2)
        except Exception:
            scale = 2
        scale = max(1, min(scale, 4))

        # EasyOCR may print progress bars; avoid encoding crashes on some Windows setups.
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

        np = importlib.import_module("numpy")
        pil_image = importlib.import_module("PIL.Image")
        easyocr = importlib.import_module("easyocr")

        model_dir = _get_easyocr_model_dir()
        reader = easyocr.Reader(
            ["cs", "en"],
            gpu=False,
            model_storage_directory=model_dir,
            verbose=False,
        )

        if png_b64:
            png_bytes = base64.b64decode(png_b64.encode("ascii"), validate=False)
            img = pil_image.open(io.BytesIO(png_bytes))
        else:
            if not image_path:
                raise RuntimeError("Missing image input")
            img = pil_image.open(str(image_path))

        # Mild upscale helps small UI fonts; configurable for speed vs accuracy
        img = img.resize((img.size[0] * scale, img.size[1] * scale))
        img = img.convert("RGB")
        img_arr = np.array(img)

        if use_greedy:
            parts = reader.readtext(img_arr, detail=0, paragraph=True, decoder="greedy")
        else:
            parts = reader.readtext(img_arr, detail=0, paragraph=True)

        out_text = "\n".join([p.strip() for p in parts if isinstance(p, str) and p.strip()]).strip()
        sys.stdout.write(json.dumps({"ok": True, "text": out_text}, ensure_ascii=False))
        return 0
    except ImportError:
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "error": "OCR Libraries Missing: python library 'easyocr' is missing. Please run: pip install easyocr",
                },
                ensure_ascii=False,
            )
        )
        return 2
    except Exception as e:
        sys.stdout.write(
            json.dumps(
                {"ok": False, "error": f"OCR Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}"},
                ensure_ascii=False,
            )
        )
        return 1


def _ocr_server_main() -> int:
    """Long-lived OCR process.

    Reads one JSON object per line from stdin and writes one JSON object per line to stdout.
    Caches EasyOCR Reader in-process so subsequent requests are fast.
    """
    try:
        # Avoid noisy warnings interfering with protocols
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

        np = importlib.import_module("numpy")
        pil_image = importlib.import_module("PIL.Image")
        easyocr = importlib.import_module("easyocr")

        model_dir = _get_easyocr_model_dir()
        reader = easyocr.Reader(
            ["cs", "en"],
            gpu=False,
            model_storage_directory=model_dir,
            verbose=False,
        )

        for line in sys.stdin:
            line = (line or "").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise RuntimeError("Invalid payload")

                image_path = payload.get("image_path")
                png_b64 = payload.get("png_b64")
                use_greedy = bool(payload.get("use_greedy", False))
                try:
                    scale = int(payload.get("scale", 2) or 2)
                except Exception:
                    scale = 2
                scale = max(1, min(scale, 4))

                if png_b64:
                    png_bytes = base64.b64decode(png_b64.encode("ascii"), validate=False)
                    img = pil_image.open(io.BytesIO(png_bytes))
                else:
                    if not image_path:
                        raise RuntimeError("Missing image input")
                    img = pil_image.open(str(image_path))

                # Mild upscale helps small UI fonts; configurable for speed vs accuracy
                img = img.resize((img.size[0] * scale, img.size[1] * scale))
                img = img.convert("RGB")
                img_arr = np.array(img)

                if use_greedy:
                    parts = reader.readtext(img_arr, detail=0, paragraph=True, decoder="greedy")
                else:
                    parts = reader.readtext(img_arr, detail=0, paragraph=True)

                out_text = "\n".join([p.strip() for p in parts if isinstance(p, str) and p.strip()]).strip()
                sys.stdout.write(json.dumps({"ok": True, "text": out_text}, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            except Exception as e:
                sys.stdout.write(
                    json.dumps(
                        {"ok": False, "error": f"OCR Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}"},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                sys.stdout.flush()

        return 0
    except ImportError:
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "error": "OCR Libraries Missing: python library 'easyocr' is missing. Please run: pip install easyocr",
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        sys.stdout.flush()
        return 2
    except Exception:
        # Last-resort: don't print huge tracebacks that might break protocol consumers.
        return 1


# Run subprocess worker modes as early as possible (before Qt imports).
if "--ocr-worker" in sys.argv:
    raise SystemExit(_ocr_worker_main())
if "--ocr-server" in sys.argv:
    raise SystemExit(_ocr_server_main())

# Prevent Qt from applying High DPI scaling
# This ensures we get physical pixel coordinates (1920x1080) for all monitors
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "0"
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
os.environ["QT_SCREEN_SCALE_FACTORS"] = "1"
os.environ["QT_SCALE_FACTOR"] = "1"

# OCR engine is optional and imported lazily (see EditorWindow.run_ocr).

import keyboard

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QColorDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMenuBar,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QSizePolicy,
    QSplitter,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt, QSettings, QSharedMemory, QRect, QPoint, QObject, Signal, QEvent, QUrl, QThread, QBuffer, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPainter, QColor, QPen, QCursor, QPixmap, QPalette, QDesktopServices
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class _OcrWorker(QObject):
    finished = Signal(str)
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self._reader = None

    def run(self, payload) -> None:
        try:
            image_path = None
            png_bytes = None
            use_greedy = False

            if isinstance(payload, dict):
                image_path = payload.get("image_path")
                png_bytes = payload.get("png_bytes")
                use_greedy = bool(payload.get("use_greedy", False))
            else:
                image_path = str(payload)

            # EasyOCR prints a download progress bar on first run.
            # On some Windows setups stdout is cp1250 and can't encode block characters (\u2588).
            try:
                if hasattr(sys.stdout, "reconfigure"):
                    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                if hasattr(sys.stderr, "reconfigure"):
                    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

            np = importlib.import_module("numpy")
            pil_image = importlib.import_module("PIL.Image")
            easyocr = importlib.import_module("easyocr")

            # Lazy-init and cache the reader within this worker thread (heavy)
            if self._reader is None:
                model_dir = _get_easyocr_model_dir()
                self._reader = easyocr.Reader(
                    ["cs", "en"],
                    gpu=False,
                    model_storage_directory=model_dir,
                    verbose=False,
                )

            if png_bytes is not None:
                img = pil_image.open(io.BytesIO(png_bytes))
            else:
                if not image_path:
                    raise RuntimeError("Missing image input")
                img = pil_image.open(image_path)
            # Mild upscale helps small UI fonts
            scale = 2
            img = img.resize((img.size[0] * scale, img.size[1] * scale))
            img = img.convert("RGB")
            img_arr = np.array(img)

            if use_greedy:
                parts = self._reader.readtext(img_arr, detail=0, paragraph=True, decoder="greedy")
            else:
                parts = self._reader.readtext(img_arr, detail=0, paragraph=True)
            out_text = "\n".join([p.strip() for p in parts if isinstance(p, str) and p.strip()]).strip()
            self.finished.emit(out_text)
        except ImportError:
            self.error.emit("OCR Libraries Missing: python library 'easyocr' is missing. Please run: pip install easyocr")
        except Exception as e:
            self.error.emit(f"OCR Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}")


class _OcrSubprocessWorker(QObject):
    finished = Signal(str)
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self._proc = None

    def _ensure_proc(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return

        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--ocr-server"]
        else:
            cmd = [sys.executable, os.path.abspath(__file__), "--ocr-server"]

        # stderr is suppressed to keep stdout strictly as JSON lines.
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

    def shutdown(self) -> None:
        proc = self._proc
        self._proc = None
        if not proc:
            return
        try:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            proc.terminate()
        except Exception:
            pass

    def run(self, payload) -> None:
        try:
            if not isinstance(payload, dict):
                raise RuntimeError("Invalid OCR payload")

            req = {
                "image_path": payload.get("image_path"),
                "png_b64": payload.get("png_b64"),
                "use_greedy": bool(payload.get("use_greedy", False)),
                "scale": payload.get("scale", 2),
            }

            if not req.get("image_path") and not req.get("png_b64"):
                raise RuntimeError("Missing image input")

            self._ensure_proc()
            if not self._proc or self._proc.poll() is not None:
                raise RuntimeError("OCR server process is not running")

            line = json.dumps(req, ensure_ascii=False)
            assert self._proc.stdin is not None
            assert self._proc.stdout is not None
            self._proc.stdin.write(line + "\n")
            self._proc.stdin.flush()

            out_line = self._proc.stdout.readline()
            if not out_line:
                raise RuntimeError("OCR server returned no output")

            resp = json.loads(out_line.strip())
            if isinstance(resp, dict) and resp.get("ok") is True:
                self.finished.emit(str(resp.get("text") or ""))
                return

            if isinstance(resp, dict):
                self.error.emit(str(resp.get("error") or "OCR failed"))
            else:
                self.error.emit("OCR failed: invalid response")
        except Exception as e:
            self.error.emit(f"OCR Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}")


class _EnhanceWorker(QObject):
    finished = Signal(bytes)
    error = Signal(str)

    def run(self, image_path: str) -> None:
        try:
            pil_image = importlib.import_module("PIL.Image")
            pil_ops = importlib.import_module("PIL.ImageOps")
            pil_enhance = importlib.import_module("PIL.ImageEnhance")
            pil_filter = importlib.import_module("PIL.ImageFilter")

            img = pil_image.open(image_path)
            img = img.convert("RGB")

            # Simple "enhance" pipeline (offline): upscale + autocontrast + mild sharpen
            scale = 2
            img = img.resize((img.size[0] * scale, img.size[1] * scale), resample=pil_image.LANCZOS)
            img = pil_ops.autocontrast(img)
            img = pil_enhance.Contrast(img).enhance(1.15)
            img = img.filter(pil_filter.UnsharpMask(radius=2, percent=160, threshold=3))

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            self.finished.emit(buf.getvalue())
        except ImportError:
            self.error.emit("Enhance tool needs Pillow (PIL). Please run: pip install pillow")
        except Exception as e:
            self.error.emit(f"Enhance Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}")


ORG_NAME = "TxtOnScrn"
APP_NAME = "TrayConfig"
REG_APP_NAME = "TxtOnScrn"
SETTINGS_HOTKEY = "hotkey"
DEFAULT_HOTKEY = "Ctrl+Shift+T"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
SETTINGS_THEME_MODE = "appearance_theme_mode"  # system | light | dark
SETTINGS_SNIP_BORDER_COLOR = "appearance_snip_border_color"  # hex, e.g. #0078D7
SETTINGS_SNIP_BORDER_WIDTH = "appearance_snip_border_width"  # int

SETTINGS_AI_OLLAMA_ENABLED = "ai_provider_ollama_enabled"
SETTINGS_AI_LMSTUDIO_ENABLED = "ai_provider_lmstudio_enabled"
SETTINGS_AI_LOCALAI_ENABLED = "ai_provider_localai_enabled"

SETTINGS_ASSISTANT_SELECTED = "assistant_selected"  # e.g. "local" or "public:groq"

SETTINGS_OCR_USE_GREEDY = "ocr_use_greedy"  # faster, slightly less accurate
SETTINGS_OCR_IN_MEMORY = "ocr_in_memory"  # avoid temp file I/O
SETTINGS_OCR_SCALE = "ocr_scale"  # int: 1..4 (speed vs accuracy)
SETTINGS_OCR_SCALE_AUTO = "ocr_scale_auto"  # bool
SETTINGS_OCR_AUTO_RUN = "ocr_auto_run"  # bool: run OCR automatically after capture
SETTINGS_OCR_ASSISTANT_AFTER_CAPTURE = "ocr_assistant_after_capture"  # bool: open Assistant instead of Editor

SETTINGS_AI_PUBLIC_GROQ_ENABLED = "ai_provider_public_groq_enabled"
SETTINGS_AI_PUBLIC_OPENAI_ENABLED = "ai_provider_public_openai_enabled"
SETTINGS_AI_PUBLIC_GEMINI_ENABLED = "ai_provider_public_gemini_enabled"
# (záměrně jednoduché – Public providery se přepínají jako rádio)

SETTINGS_AI_PUBLIC_GROQ_API_KEY = "ai_public_groq_api_key"
SETTINGS_AI_PUBLIC_OPENAI_API_KEY = "ai_public_openai_api_key"
SETTINGS_AI_PUBLIC_GEMINI_API_KEY = "ai_public_gemini_api_key"
SETTINGS_AI_GROQ_ENABLED = "ai_provider_groq_enabled"

DEFAULT_SNIP_BORDER_COLOR = "#aaaaff"
DEFAULT_SNIP_BORDER_WIDTH = 1


def _dpapi_encrypt_to_b64(text: str) -> str:
    """Windows DPAPI encrypt -> base64. Falls back to plain text if not available."""
    if not text:
        return ""
    if os.name != "nt":
        return text
    try:
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        raw = text.encode("utf-8")
        in_blob = DATA_BLOB(len(raw), (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw))
        out_blob = DATA_BLOB()

        if not crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        ):
            return text

        try:
            out_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            return base64.b64encode(out_bytes).decode("ascii")
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except Exception:
        return text


def _dpapi_decrypt_from_b64(value: str) -> str:
    """Windows DPAPI decrypt from base64. If value isn't encrypted, returns it as-is."""
    if not value:
        return ""
    if os.name != "nt":
        return value
    try:
        # If it's not base64, treat as plain
        try:
            enc = base64.b64decode(value.encode("ascii"), validate=True)
        except Exception:
            return value

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        in_blob = DATA_BLOB(len(enc), (ctypes.c_ubyte * len(enc)).from_buffer_copy(enc))
        out_blob = DATA_BLOB()

        if not crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        ):
            return value

        try:
            out_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            return out_bytes.decode("utf-8", errors="replace")
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except Exception:
        return value


def get_windows_theme_mode() -> str:
    """Returns 'light' or 'dark' based on Windows settings (AppsUseLightTheme)."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            0,
            winreg.KEY_READ,
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if int(value) == 1 else "dark"
    except Exception:
        return "light"


def apply_theme_mode(mode: str) -> None:
    """Applies 'light'/'dark' palette. Uses Windows mode for 'system'."""
    app = QApplication.instance()
    if app is None:
        return

    chosen = (mode or "system").lower()
    if chosen == "system":
        chosen = get_windows_theme_mode()

    app.setStyle("Fusion")

    palette = QPalette()
    if chosen == "dark":
        palette.setColor(QPalette.Window, QColor(32, 32, 32))
        palette.setColor(QPalette.WindowText, Qt.white)
        palette.setColor(QPalette.Base, QColor(24, 24, 24))
        palette.setColor(QPalette.AlternateBase, QColor(45, 45, 45))
        palette.setColor(QPalette.ToolTipBase, QColor(255, 255, 255))
        palette.setColor(QPalette.ToolTipText, QColor(0, 0, 0))
        palette.setColor(QPalette.Text, Qt.white)
        palette.setColor(QPalette.Button, QColor(45, 45, 45))
        palette.setColor(QPalette.ButtonText, Qt.white)
        palette.setColor(QPalette.BrightText, Qt.red)
        palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
        palette.setColor(QPalette.HighlightedText, Qt.black)
    else:
        # light
        palette.setColor(QPalette.Window, QColor(245, 245, 245))
        palette.setColor(QPalette.WindowText, QColor(0, 0, 0))
        palette.setColor(QPalette.Base, QColor(255, 255, 255))
        palette.setColor(QPalette.AlternateBase, QColor(235, 235, 235))
        palette.setColor(QPalette.ToolTipBase, QColor(255, 255, 220))
        palette.setColor(QPalette.ToolTipText, QColor(0, 0, 0))
        palette.setColor(QPalette.Text, QColor(0, 0, 0))
        palette.setColor(QPalette.Button, QColor(245, 245, 245))
        palette.setColor(QPalette.ButtonText, QColor(0, 0, 0))
        palette.setColor(QPalette.BrightText, Qt.red)
        palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
        palette.setColor(QPalette.HighlightedText, Qt.white)

    app.setPalette(palette)

    # Ensure already-open windows/widgets repaint with the new palette
    for w in app.allWidgets():
        try:
            w.setPalette(app.palette())
            w.update()
        except Exception:
            pass


def resource_path(name: str) -> str:
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str(base_path / name)


def get_startup_command() -> str:
    if getattr(sys, 'frozen', False):
        # Running as compiled executable
        exe_path = Path(sys.executable).resolve()
        return f'"{exe_path}"'
    else:
        # Running as Python script
        script_path = Path(__file__).resolve()
        python_exe = Path(sys.executable).resolve()
        return f'"{python_exe}" "{script_path}"'


def is_startup_enabled() -> bool:
    cmd = get_startup_command()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, REG_APP_NAME)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Startup check: Reg='{value}' vs App='{cmd}'")
            return value.lower() == cmd.lower()
    except FileNotFoundError:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Startup check: Registry key not found or value missing")
        return False
    except OSError as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Startup check error: {e}")
        return False


def set_startup_enabled(enabled: bool) -> None:
    command = get_startup_command()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Setting startup: {enabled} -> {command}")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, REG_APP_NAME, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, REG_APP_NAME)
                except FileNotFoundError:
                    pass
    except OSError as e:
        print(f"Registry error: {e}")


class ConfigTab(QWidget):
    def __init__(self, settings: QSettings, tray_app=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.tray_app = tray_app

        self.startup_checkbox = QCheckBox("Launch at startup")
        self.startup_checkbox.toggled.connect(self.on_startup_toggled)

        self.hotkey_label = QLabel()
        self.change_button = QPushButton("Change")
        self.change_button.clicked.connect(self.on_change_hotkey)

        hotkey_row = QHBoxLayout()
        hotkey_row.addWidget(QLabel("Hotkey:"))
        hotkey_row.addWidget(self.hotkey_label, 1)
        hotkey_row.addWidget(self.change_button)

        ocr_group = QGroupBox("OCR")
        ocr_layout = QVBoxLayout(ocr_group)
        self.ocr_greedy_cb = QCheckBox("Fast mode (greedy decoder)")
        self.ocr_in_memory_cb = QCheckBox("Use in-memory image (avoid temp file)")
        self.ocr_greedy_cb.toggled.connect(lambda checked: self.settings.setValue(SETTINGS_OCR_USE_GREEDY, bool(checked)))
        self.ocr_in_memory_cb.toggled.connect(lambda checked: self.settings.setValue(SETTINGS_OCR_IN_MEMORY, bool(checked)))
        ocr_layout.addWidget(self.ocr_greedy_cb)
        ocr_layout.addWidget(self.ocr_in_memory_cb)

        ocr_scale_row = QHBoxLayout()
        ocr_scale_row.addWidget(QLabel("Image upscale (1 = none):"))
        self.ocr_scale_spin = QSpinBox()
        self.ocr_scale_spin.setRange(1, 4)
        self.ocr_scale_spin.setToolTip("1 = no upscale (fastest). Higher = more accurate on small fonts, but slower. 2 is a good default.")
        self.ocr_scale_spin.valueChanged.connect(lambda v: self.settings.setValue(SETTINGS_OCR_SCALE, int(v)))
        ocr_scale_row.addWidget(self.ocr_scale_spin)
        ocr_scale_row.addWidget(QLabel("(1=none/fast, 2=default, 3-4=slower)"))
        ocr_scale_row.addStretch()
        ocr_layout.addLayout(ocr_scale_row)

        self.ocr_scale_auto_cb = QCheckBox("Automatic (pick scale based on selection size)")
        self.ocr_scale_auto_cb.toggled.connect(self._on_ocr_scale_auto_toggled)
        ocr_layout.addWidget(self.ocr_scale_auto_cb)

        self.ocr_auto_run_cb = QCheckBox("Auto-run OCR after capture")
        self.ocr_auto_run_cb.toggled.connect(lambda checked: self.settings.setValue(SETTINGS_OCR_AUTO_RUN, bool(checked)))
        ocr_layout.addWidget(self.ocr_auto_run_cb)

        self.ocr_assistant_after_capture_cb = QCheckBox("After capture: open AI assistant (skip editor)")
        self.ocr_assistant_after_capture_cb.toggled.connect(
            lambda checked: self.settings.setValue(SETTINGS_OCR_ASSISTANT_AFTER_CAPTURE, bool(checked))
        )
        ocr_layout.addWidget(self.ocr_assistant_after_capture_cb)

        self.ocr_clear_cache_btn = QPushButton("Clear OCR model cache")
        self.ocr_clear_cache_btn.setToolTip("Deletes cached EasyOCR model files from disk. They will be re-downloaded on next OCR use.")
        self.ocr_clear_cache_btn.clicked.connect(self._on_clear_ocr_cache_clicked)
        ocr_layout.addWidget(self.ocr_clear_cache_btn)

        self.uninstall_btn = QPushButton("Uninstall / remove all app data")
        self.uninstall_btn.setToolTip("Disables startup, clears settings (registry), and deletes app data in LocalAppData.")
        self.uninstall_btn.clicked.connect(self._on_uninstall_clicked)

        layout = QVBoxLayout(self)
        layout.addWidget(self.startup_checkbox)
        layout.addLayout(hotkey_row)
        layout.addWidget(ocr_group)
        layout.addWidget(self.uninstall_btn)
        layout.addStretch()

        self.refresh()

    def refresh(self) -> None:
        is_enabled = is_startup_enabled()
        self.startup_checkbox.blockSignals(True)
        self.startup_checkbox.setChecked(is_enabled)
        self.startup_checkbox.blockSignals(False)

        hotkey = self.settings.value(SETTINGS_HOTKEY, DEFAULT_HOTKEY)
        self.hotkey_label.setText(hotkey)

        self.ocr_greedy_cb.blockSignals(True)
        self.ocr_greedy_cb.setChecked(bool(self.settings.value(SETTINGS_OCR_USE_GREEDY, False, type=bool)))
        self.ocr_greedy_cb.blockSignals(False)

        self.ocr_in_memory_cb.blockSignals(True)
        self.ocr_in_memory_cb.setChecked(bool(self.settings.value(SETTINGS_OCR_IN_MEMORY, False, type=bool)))
        self.ocr_in_memory_cb.blockSignals(False)

        try:
            scale = int(self.settings.value(SETTINGS_OCR_SCALE, 2) or 2)
        except Exception:
            scale = 2
        scale = max(1, min(scale, 4))
        self.ocr_scale_spin.blockSignals(True)
        self.ocr_scale_spin.setValue(scale)
        self.ocr_scale_spin.blockSignals(False)

        auto = bool(self.settings.value(SETTINGS_OCR_SCALE_AUTO, False, type=bool))
        self.ocr_scale_auto_cb.blockSignals(True)
        self.ocr_scale_auto_cb.setChecked(auto)
        self.ocr_scale_auto_cb.blockSignals(False)
        self.ocr_scale_spin.setEnabled(not auto)

        self.ocr_auto_run_cb.blockSignals(True)
        self.ocr_auto_run_cb.setChecked(bool(self.settings.value(SETTINGS_OCR_AUTO_RUN, False, type=bool)))
        self.ocr_auto_run_cb.blockSignals(False)

        self.ocr_assistant_after_capture_cb.blockSignals(True)
        self.ocr_assistant_after_capture_cb.setChecked(
            bool(self.settings.value(SETTINGS_OCR_ASSISTANT_AFTER_CAPTURE, False, type=bool))
        )
        self.ocr_assistant_after_capture_cb.blockSignals(False)

    def _on_ocr_scale_auto_toggled(self, checked: bool) -> None:
        self.settings.setValue(SETTINGS_OCR_SCALE_AUTO, bool(checked))
        self.ocr_scale_spin.setEnabled(not bool(checked))

    def _on_clear_ocr_cache_clicked(self) -> None:
        model_dir = _get_easyocr_model_dir()
        answer = QMessageBox.question(
            self,
            "Clear OCR cache",
            "This will delete cached EasyOCR model files from disk.\n"
            "OCR will download them again the next time you run OCR.\n\n"
            f"Folder:\n{model_dir}\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        try:
            if os.path.isdir(model_dir):
                shutil.rmtree(model_dir, ignore_errors=False)
            Path(model_dir).mkdir(parents=True, exist_ok=True)
            QMessageBox.information(self, "Clear OCR cache", "OCR cache cleared.")
        except Exception as e:
            QMessageBox.warning(
                self,
                "Clear OCR cache",
                "Could not clear OCR cache. If OCR is currently running, close OCR windows and try again.\n\n"
                f"Error: {e}",
            )

    def _on_uninstall_clicked(self) -> None:
        app_dir = _get_app_data_dir()
        model_dir = _get_easyocr_model_dir()

        answer = QMessageBox.question(
            self,
            "Uninstall / remove all data",
            "This will:\n"
            "- Disable startup (registry)\n"
            "- Clear all app settings (registry)\n"
            f"- Delete app data folder:\n{app_dir}\n"
            f"  (includes OCR model cache: {model_dir})\n\n"
            "The app will close afterwards. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        # Stop windows / OCR subprocesses first to release file locks.
        try:
            if self.tray_app is not None:
                try:
                    if getattr(self.tray_app, "ocr_assistant_flow", None) is not None:
                        self.tray_app.ocr_assistant_flow.shutdown()
                        self.tray_app.ocr_assistant_flow = None
                except Exception:
                    pass

                try:
                    if getattr(self.tray_app, "editor_window", None) is not None:
                        self.tray_app.editor_window.close()
                        self.tray_app.editor_window = None
                except Exception:
                    pass
        except Exception:
            pass

        errors = []
        try:
            set_startup_enabled(False)
        except Exception as e:
            errors.append(f"Startup: {e}")

        try:
            s = QSettings(ORG_NAME, APP_NAME)
            s.clear()
            s.sync()
        except Exception as e:
            errors.append(f"Settings: {e}")

        try:
            if os.path.isdir(app_dir):
                shutil.rmtree(app_dir)
        except Exception as e:
            errors.append(f"AppData: {e}")

        # Legacy: older versions stored EasyOCR models in %TEMP%\TxtOnScrn_EasyOCR
        try:
            legacy_dir = os.path.join(tempfile.gettempdir(), "TxtOnScrn_EasyOCR")
            if os.path.isdir(legacy_dir):
                shutil.rmtree(legacy_dir)
        except Exception as e:
            errors.append(f"LegacyTemp: {e}")

        if errors:
            QMessageBox.warning(
                self,
                "Uninstall / remove all data",
                "Cleanup finished with errors:\n\n" + "\n".join(errors) + "\n\nThe app will now close.",
            )
        else:
            QMessageBox.information(self, "Uninstall / remove all data", "Cleanup done. The app will now close.")

        # Exit the app after the dialog closes.
        if self.tray_app is not None:
            QTimer.singleShot(0, self.tray_app.quit)
        else:
            QTimer.singleShot(0, QApplication.quit)

    def on_startup_toggled(self, checked: bool) -> None:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Startup toggled: {checked}")
        set_startup_enabled(checked)

    def on_change_hotkey(self) -> None:
        dialog = HotkeyDialog(self.hotkey_label.text(), parent=self)
        if dialog.exec() == QDialog.Accepted:
            new_hotkey = dialog.hotkey_text.strip()
            if new_hotkey:
                self.hotkey_label.setText(new_hotkey)
                self.settings.setValue(SETTINGS_HOTKEY, new_hotkey)
                if self.tray_app:
                    self.tray_app.register_hotkey()


class SettingsDialog(QDialog):
    def __init__(self, settings: QSettings, tray_app=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.tray_app = tray_app

        self.setWindowTitle("Settings")
        self.setWindowIcon(QIcon(resource_path("ico.ico")))
        self.resize(460, 320)

        self.tabs = QTabWidget()
        self.tab_name_to_index = {}

        config_tab = ConfigTab(settings=self.settings, tray_app=self.tray_app)
        self._add_tab(config_tab, "General")

        appearance_tab = QWidget()
        appearance_layout = QVBoxLayout(appearance_tab)

        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("Theme:"))
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("System (Windows)", "system")
        self.theme_combo.addItem("Light", "light")
        self.theme_combo.addItem("Dark", "dark")
        self.theme_combo.currentIndexChanged.connect(self.on_theme_changed)
        theme_row.addWidget(self.theme_combo, 1)

        appearance_layout.addLayout(theme_row)

        # Snipping selection border
        border_row = QHBoxLayout()
        border_row.addWidget(QLabel("Snip border:"))

        self.snip_border_preview = QLabel()
        self.snip_border_preview.setFixedSize(44, 18)
        border_row.addWidget(self.snip_border_preview)

        self.snip_border_color_btn = QPushButton("Color...")
        self.snip_border_color_btn.clicked.connect(self.on_pick_snip_border_color)
        border_row.addWidget(self.snip_border_color_btn)

        border_row.addWidget(QLabel("Width:"))
        self.snip_border_width = QSpinBox()
        self.snip_border_width.setRange(1, 10)
        self.snip_border_width.valueChanged.connect(self.on_snip_border_width_changed)
        border_row.addWidget(self.snip_border_width)

        border_row.addStretch()
        appearance_layout.addLayout(border_row)

        appearance_layout.addStretch()
        self._add_tab(appearance_tab, "Appearance")

        ai_tab = QWidget()
        ai_layout = QVBoxLayout(ai_tab)

        local_group = QGroupBox("Local")
        local_layout = QVBoxLayout(local_group)
        self.ai_ollama_cb = QCheckBox("Ollama (local server)")
        self.ai_lmstudio_cb = QCheckBox("LM Studio (local server)")
        self.ai_localai_cb = QCheckBox("LocalAI (OpenAI-compatible, local)")

        self.ai_ollama_cb.toggled.connect(lambda checked: self.on_local_provider_toggled(self.ai_ollama_cb, SETTINGS_AI_OLLAMA_ENABLED, checked))
        self.ai_lmstudio_cb.toggled.connect(lambda checked: self.on_local_provider_toggled(self.ai_lmstudio_cb, SETTINGS_AI_LMSTUDIO_ENABLED, checked))
        self.ai_localai_cb.toggled.connect(lambda checked: self.on_local_provider_toggled(self.ai_localai_cb, SETTINGS_AI_LOCALAI_ENABLED, checked))

        local_layout.addWidget(self.ai_ollama_cb)
        local_layout.addWidget(self.ai_lmstudio_cb)
        local_layout.addWidget(self.ai_localai_cb)

        public_group = QGroupBox("Public")
        public_layout = QVBoxLayout(public_group)
        self.ai_pub_groq_cb = QCheckBox("Groq")
        self.ai_pub_openai_cb = QCheckBox("OpenAI (ChatGPT)")
        self.ai_pub_gemini_cb = QCheckBox("Google Gemini")

        self.ai_pub_groq_cb.toggled.connect(lambda checked: self.on_public_provider_toggled(self.ai_pub_groq_cb, SETTINGS_AI_PUBLIC_GROQ_ENABLED, checked))
        self.ai_pub_openai_cb.toggled.connect(lambda checked: self.on_public_provider_toggled(self.ai_pub_openai_cb, SETTINGS_AI_PUBLIC_OPENAI_ENABLED, checked))
        self.ai_pub_gemini_cb.toggled.connect(lambda checked: self.on_public_provider_toggled(self.ai_pub_gemini_cb, SETTINGS_AI_PUBLIC_GEMINI_ENABLED, checked))

        public_layout.addWidget(self.ai_pub_groq_cb)
        public_layout.addWidget(self.ai_pub_openai_cb)
        public_layout.addWidget(self.ai_pub_gemini_cb)

        public_key_row = QHBoxLayout()
        self.public_api_key_label = QLabel("API key:")
        public_key_row.addWidget(self.public_api_key_label)
        self.public_api_key_edit = QLineEdit()
        self.public_api_key_edit.setEchoMode(QLineEdit.Password)
        self.public_api_key_edit.setPlaceholderText("Select provider and enter API key…")
        self.public_api_key_edit.textChanged.connect(self.on_public_key_changed)
        public_key_row.addWidget(self.public_api_key_edit, 1)

        self.public_key_show_btn = QPushButton("Show")
        self.public_key_show_btn.clicked.connect(self.on_toggle_public_key_visibility)
        public_key_row.addWidget(self.public_key_show_btn)

        self.public_key_clear_btn = QPushButton("Clear")
        self.public_key_clear_btn.clicked.connect(lambda: self.public_api_key_edit.setText(""))
        public_key_row.addWidget(self.public_key_clear_btn)
        public_layout.addLayout(public_key_row)

        self.public_status_label = QLabel("")
        public_layout.addWidget(self.public_status_label)

        public_buttons = QHBoxLayout()
        public_buttons.addStretch()
        self.ai_get_key_btn = QPushButton("Get API Key")
        self.ai_get_key_btn.clicked.connect(self.on_ai_get_key_clicked)
        public_buttons.addWidget(self.ai_get_key_btn)

        self.ai_test_key_btn = QPushButton("Test key")
        self.ai_test_key_btn.clicked.connect(self.on_ai_test_key_clicked)
        public_buttons.addWidget(self.ai_test_key_btn)

        public_layout.addLayout(public_buttons)

        ai_layout.addWidget(local_group)
        ai_layout.addWidget(public_group)
        ai_layout.addStretch()
        self._add_tab(ai_tab, "AI")

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)

        buttons = QHBoxLayout()
        buttons.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self.refresh_ui()

    def refresh_ui(self) -> None:
        # Sync theme selector from settings
        saved = (self.settings.value(SETTINGS_THEME_MODE, "system") or "system").lower()
        for i in range(self.theme_combo.count()):
            if self.theme_combo.itemData(i) == saved:
                self.theme_combo.blockSignals(True)
                self.theme_combo.setCurrentIndex(i)
                self.theme_combo.blockSignals(False)
                break

        # Sync snip border settings
        border_color = str(self.settings.value(SETTINGS_SNIP_BORDER_COLOR, DEFAULT_SNIP_BORDER_COLOR) or DEFAULT_SNIP_BORDER_COLOR)
        try:
            border_width = int(self.settings.value(SETTINGS_SNIP_BORDER_WIDTH, DEFAULT_SNIP_BORDER_WIDTH) or DEFAULT_SNIP_BORDER_WIDTH)
        except Exception:
            border_width = DEFAULT_SNIP_BORDER_WIDTH

        self._set_snip_border_preview(border_color)
        self.snip_border_width.blockSignals(True)
        self.snip_border_width.setValue(border_width)
        self.snip_border_width.blockSignals(False)

        # Sync AI provider checkboxes
        if hasattr(self, "ai_ollama_cb"):
            self.ai_ollama_cb.blockSignals(True)
            self.ai_ollama_cb.setChecked(bool(self.settings.value(SETTINGS_AI_OLLAMA_ENABLED, False, type=bool)))
            self.ai_ollama_cb.blockSignals(False)
        if hasattr(self, "ai_lmstudio_cb"):
            self.ai_lmstudio_cb.blockSignals(True)
            self.ai_lmstudio_cb.setChecked(bool(self.settings.value(SETTINGS_AI_LMSTUDIO_ENABLED, False, type=bool)))
            self.ai_lmstudio_cb.blockSignals(False)
        if hasattr(self, "ai_localai_cb"):
            self.ai_localai_cb.blockSignals(True)
            self.ai_localai_cb.setChecked(bool(self.settings.value(SETTINGS_AI_LOCALAI_ENABLED, False, type=bool)))
            self.ai_localai_cb.blockSignals(False)

        # Backward-compat: older keys map to Public providers
        if self.settings.value(SETTINGS_AI_PUBLIC_GROQ_ENABLED, None) is None:
            self.settings.setValue(SETTINGS_AI_PUBLIC_GROQ_ENABLED, bool(self.settings.value(SETTINGS_AI_GROQ_ENABLED, False, type=bool)))
        if hasattr(self, "ai_pub_groq_cb"):
            self.ai_pub_groq_cb.blockSignals(True)
            self.ai_pub_groq_cb.setChecked(bool(self.settings.value(SETTINGS_AI_PUBLIC_GROQ_ENABLED, False, type=bool)))
            self.ai_pub_groq_cb.blockSignals(False)
        if hasattr(self, "ai_pub_openai_cb"):
            self.ai_pub_openai_cb.blockSignals(True)
            self.ai_pub_openai_cb.setChecked(bool(self.settings.value(SETTINGS_AI_PUBLIC_OPENAI_ENABLED, False, type=bool)))
            self.ai_pub_openai_cb.blockSignals(False)
        if hasattr(self, "ai_pub_gemini_cb"):
            self.ai_pub_gemini_cb.blockSignals(True)
            self.ai_pub_gemini_cb.setChecked(bool(self.settings.value(SETTINGS_AI_PUBLIC_GEMINI_ENABLED, False, type=bool)))
            self.ai_pub_gemini_cb.blockSignals(False)

        self._refresh_public_key_ui()

    def on_ai_get_key_clicked(self) -> None:
        provider = self._get_selected_public_provider()
        if not provider:
            QMessageBox.information(self, "API key", "Select a provider in the Public section.")
            return

        url = {
            "groq": "https://console.groq.com/keys",
            "openai": "https://platform.openai.com/api-keys",
            "gemini": "https://aistudio.google.com/app/apikey",
        }[provider]
        QDesktopServices.openUrl(QUrl(url))

    def on_ai_test_key_clicked(self) -> None:
        provider = self._get_selected_public_provider()
        if not provider:
            QMessageBox.information(self, "Test key", "Select a provider in the Public section.")
            return

        api_key = (self.public_api_key_edit.text() or "").strip()
        if not api_key:
            QMessageBox.information(self, "Test key", "Enter API key first.")
            return

        self.public_status_label.setText("Testing…")
        self.setCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            ok, msg = self._test_public_key(provider, api_key)
            self.public_status_label.setText(("OK: " if ok else "Error: ") + msg)
        finally:
            self.setCursor(Qt.ArrowCursor)

    def on_toggle_public_key_visibility(self) -> None:
        if self.public_api_key_edit.echoMode() == QLineEdit.Password:
            self.public_api_key_edit.setEchoMode(QLineEdit.Normal)
            self.public_key_show_btn.setText("Hide")
        else:
            self.public_api_key_edit.setEchoMode(QLineEdit.Password)
            self.public_key_show_btn.setText("Show")

    def on_public_key_changed(self, value: str) -> None:
        provider = self._get_selected_public_provider()
        if not provider:
            return

        key_setting = {
            "groq": SETTINGS_AI_PUBLIC_GROQ_API_KEY,
            "openai": SETTINGS_AI_PUBLIC_OPENAI_API_KEY,
            "gemini": SETTINGS_AI_PUBLIC_GEMINI_API_KEY,
        }[provider]
        self.settings.setValue(key_setting, _dpapi_encrypt_to_b64(value))

    def on_local_provider_toggled(self, checkbox: QCheckBox, setting_key: str, checked: bool) -> None:
        if checked:
            for other_cb, other_key in (
                (self.ai_ollama_cb, SETTINGS_AI_OLLAMA_ENABLED),
                (self.ai_lmstudio_cb, SETTINGS_AI_LMSTUDIO_ENABLED),
                (self.ai_localai_cb, SETTINGS_AI_LOCALAI_ENABLED),
            ):
                if other_cb is checkbox:
                    continue
                other_cb.blockSignals(True)
                other_cb.setChecked(False)
                other_cb.blockSignals(False)
                self.settings.setValue(other_key, False)

        self.settings.setValue(setting_key, bool(checked))

    def on_public_provider_toggled(self, checkbox: QCheckBox, setting_key: str, checked: bool) -> None:
        if checked:
            for other_cb, other_key in (
                (self.ai_pub_groq_cb, SETTINGS_AI_PUBLIC_GROQ_ENABLED),
                (self.ai_pub_openai_cb, SETTINGS_AI_PUBLIC_OPENAI_ENABLED),
                (self.ai_pub_gemini_cb, SETTINGS_AI_PUBLIC_GEMINI_ENABLED),
            ):
                if other_cb is checkbox:
                    continue
                other_cb.blockSignals(True)
                other_cb.setChecked(False)
                other_cb.blockSignals(False)
                self.settings.setValue(other_key, False)

        self.settings.setValue(setting_key, bool(checked))
        self._refresh_public_key_ui()

    def _get_selected_public_provider(self):
        if self.ai_pub_groq_cb.isChecked():
            return "groq"
        if self.ai_pub_openai_cb.isChecked():
            return "openai"
        if self.ai_pub_gemini_cb.isChecked():
            return "gemini"
        return None

    def _refresh_public_key_ui(self) -> None:
        provider = self._get_selected_public_provider()
        enabled = provider is not None

        # Provider-aware label + placeholder
        if provider == "groq":
            self.public_api_key_label.setText("Groq API key:")
            setting_key = SETTINGS_AI_PUBLIC_GROQ_API_KEY
        elif provider == "openai":
            self.public_api_key_label.setText("OpenAI API key:")
            setting_key = SETTINGS_AI_PUBLIC_OPENAI_API_KEY
        elif provider == "gemini":
            self.public_api_key_label.setText("Gemini API key:")
            setting_key = SETTINGS_AI_PUBLIC_GEMINI_API_KEY
        else:
            self.public_api_key_label.setText("API key:")
            setting_key = None

        self.public_api_key_edit.setEnabled(enabled)
        self.public_key_show_btn.setEnabled(enabled)
        self.public_key_clear_btn.setEnabled(enabled)
        self.ai_get_key_btn.setEnabled(enabled)
        self.ai_test_key_btn.setEnabled(enabled)

        if not enabled:
            self.public_api_key_edit.blockSignals(True)
            self.public_api_key_edit.setText("")
            self.public_api_key_edit.blockSignals(False)
            self.public_status_label.setText("Select a provider in the Public section.")
            return

        stored = str(self.settings.value(setting_key, "") or "")
        decrypted = _dpapi_decrypt_from_b64(stored)
        self.public_api_key_edit.blockSignals(True)
        self.public_api_key_edit.setText(decrypted)
        self.public_api_key_edit.blockSignals(False)
        self.public_status_label.setText("")

    def _http_json(self, url: str, headers: dict):
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read()
            try:
                return resp.status, json.loads(data.decode("utf-8", errors="replace"))
            except Exception:
                return resp.status, {"raw": data.decode("utf-8", errors="replace")}

    def _test_public_key(self, provider: str, api_key: str):
        try:
            if provider == "groq":
                status, body = self._http_json(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
                )
                if status == 200:
                    return True, "Groq key is valid"
                return False, f"HTTP {status}"

            if provider == "openai":
                status, body = self._http_json(
                    "https://api.openai.com/v1/models",
                    headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
                )
                if status == 200:
                    # Note: This only proves the key is accepted. Chat/completions may still fail
                    # if billing/quota is not enabled for the org/project.
                    return True, "OpenAI key je valid (pozn.: pro generování je potřeba mít aktivní billing/kvótu)"
                return False, f"HTTP {status}"

            if provider == "gemini":
                # Gemini (Google AI Studio) uses API key in query param.
                status, body = self._http_json(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={urllib.parse.quote(api_key)}",
                    headers={"Accept": "application/json"},
                )
                if status == 200:
                    return True, "Gemini API key je valid"
                return False, f"HTTP {status}"

            return False, "Unknown provider"
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}"
        except Exception as e:
            return False, str(e)

    def on_theme_changed(self) -> None:
        mode = self.theme_combo.currentData()
        self.settings.setValue(SETTINGS_THEME_MODE, mode)
        apply_theme_mode(mode)

    def _set_snip_border_preview(self, hex_color: str) -> None:
        color = QColor(hex_color)
        if not color.isValid():
            color = QColor(DEFAULT_SNIP_BORDER_COLOR)
        # simple swatch
        self.snip_border_preview.setStyleSheet(
            f"background-color: {color.name()}; border: 1px solid rgba(0,0,0,0.35);"
        )

    def on_pick_snip_border_color(self) -> None:
        current = str(self.settings.value(SETTINGS_SNIP_BORDER_COLOR, DEFAULT_SNIP_BORDER_COLOR) or DEFAULT_SNIP_BORDER_COLOR)
        initial = QColor(current)
        picked = QColorDialog.getColor(initial, self, "Pick snip border color")
        if not picked.isValid():
            return
        self.settings.setValue(SETTINGS_SNIP_BORDER_COLOR, picked.name())
        self._set_snip_border_preview(picked.name())

    def on_snip_border_width_changed(self, value: int) -> None:
        self.settings.setValue(SETTINGS_SNIP_BORDER_WIDTH, int(value))

    def _add_tab(self, widget: QWidget, name: str) -> None:
        idx = self.tabs.addTab(widget, name)
        self.tab_name_to_index[name.lower()] = idx

    def show_tab(self, name: str) -> None:
        idx = self.tab_name_to_index.get((name or "").lower())
        if idx is not None:
            self.tabs.setCurrentIndex(idx)

        # Refresh General tab on show (startup/hotkey may change outside)
        current = self.tabs.currentWidget()
        if isinstance(current, ConfigTab):
            current.refresh()


class HotkeyDialog(QDialog):
    def __init__(self, current_hotkey: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Change Hotkey")
        self.setWindowIcon(QIcon(resource_path("ico.ico")))
        self.hotkey_text = current_hotkey

        self.label = QLabel("Press new hotkey combination:")
        self.input = QLabel(current_hotkey)
        self.input.setFrameStyle(QLabel.Panel | QLabel.Sunken)

        self.buttons = QWidget()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)

        btn_row = QHBoxLayout(self.buttons)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)

        layout = QVBoxLayout()
        layout.addWidget(self.label)
        layout.addWidget(self.input)
        layout.addWidget(self.buttons)
        self.setLayout(layout)

    def keyPressEvent(self, event):  # noqa: N802
        keys = []
        if event.modifiers() & Qt.ControlModifier:
            keys.append("Ctrl")
        if event.modifiers() & Qt.ShiftModifier:
            keys.append("Shift")
        if event.modifiers() & Qt.AltModifier:
            keys.append("Alt")
        if event.modifiers() & Qt.MetaModifier:
            keys.append("Meta")
        key = event.key()
        if key not in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            key_sequence = QKeySequence(key)
            key_text = key_sequence.toString()
            if key_text:
                keys.append(key_text)
        if keys:
            self.hotkey_text = "+".join(keys)
            self.input.setText(self.hotkey_text)
        event.accept()


class HotkeySignal(QObject):
    hotkey_pressed = Signal()


class AssistantDialog(QDialog):
    def __init__(self, mode: str, get_editor_text, set_editor_text=None, open_settings=None, parent=None):
        super().__init__(parent)
        self.mode = (mode or "local").lower()
        self.get_editor_text = get_editor_text
        self.set_editor_text = set_editor_text
        self.open_settings = open_settings

        self.setWindowTitle("Assistant")
        self.setWindowIcon(QIcon(resource_path("ico.ico")))
        self.resize(640, 420)

        layout = QVBoxLayout(self)

        # Assistant selection + settings shortcut
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Assistant:"))
        self.assistant_combo = QComboBox()
        self.assistant_combo.addItem("Local (offline)", "local")
        self.assistant_combo.addItem("Public (Groq)", "public:groq")
        self.assistant_combo.addItem("Public (OpenAI)", "public:openai")
        self.assistant_combo.addItem("Public (Gemini)", "public:gemini")
        self.assistant_combo.currentIndexChanged.connect(self._on_assistant_changed)
        top_row.addWidget(self.assistant_combo, 1)

        settings_btn = QPushButton("Settings…")
        settings_btn.clicked.connect(self._open_settings_clicked)
        top_row.addWidget(settings_btn)

        help_btn = QPushButton("Help")
        help_btn.clicked.connect(self._help_clicked)
        top_row.addWidget(help_btn)
        layout.addLayout(top_row)

        layout.addWidget(QLabel("Enter task/question (can be unrelated to text)."))

        self.task_edit = QTextEdit()
        self.task_edit.setPlaceholderText("E.g.: Summarize into 5 points. / Fix typos. / Explain what this means... ")
        self.task_edit.installEventFilter(self)
        layout.addWidget(self.task_edit, 1)

        layout.addWidget(QLabel("Output:"))
        self.output_edit = QTextEdit()
        self.output_edit.setReadOnly(True)
        layout.addWidget(self.output_edit, 2)

        self._set_initial_assistant_selection()
        self._on_assistant_changed()

        buttons = QHBoxLayout()
        buttons.addStretch()

        copy_label = "Copy editor text" if callable(self.set_editor_text) else "Copy OCR text"
        self.copy_input_btn = QPushButton(copy_label)
        self.copy_input_btn.clicked.connect(self._copy_input_clicked)
        buttons.addWidget(self.copy_input_btn)

        self.apply_btn = QPushButton("Apply to editor")
        self.apply_btn.setEnabled(callable(self.set_editor_text))
        self.apply_btn.clicked.connect(self._apply_to_editor_clicked)
        buttons.addWidget(self.apply_btn)
        self.run_btn = QPushButton("Run")
        self.run_btn.clicked.connect(self.on_run)
        buttons.addWidget(self.run_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

    def set_run_enabled(self, enabled: bool) -> None:
        try:
            self.run_btn.setEnabled(bool(enabled))
        except Exception:
            pass

    def eventFilter(self, source, event):
        # Submit on Enter, allow newline on Shift+Enter
        if source == self.task_edit and event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ShiftModifier:
                    return False
                self.on_run()
                return True
        return super().eventFilter(source, event)

    def _apply_to_editor_clicked(self) -> None:
        if not callable(self.set_editor_text):
            QMessageBox.information(self, "Apply", "Editor is not available.")
            return

        text = (self.output_edit.toPlainText() or "").strip()
        if not text:
            QMessageBox.information(self, "Apply", "No output to apply.")
            return

        try:
            self.set_editor_text(text)
        except Exception as e:
            QMessageBox.warning(self, "Apply", f"Could not apply to editor: {e}")

    def _copy_input_clicked(self) -> None:
        text = ""
        try:
            text = (self.get_editor_text() or "").strip()
        except Exception:
            text = ""

        if not text:
            QMessageBox.information(self, "Copy", "No input text to copy.")
            return

        try:
            QApplication.clipboard().setText(text)
        except Exception as e:
            QMessageBox.warning(self, "Copy", f"Could not copy to clipboard: {e}")

    def on_run(self):
        task = (self.task_edit.toPlainText() or "").strip()
        editor_text = ""
        try:
            editor_text = (self.get_editor_text() or "")
        except Exception:
            editor_text = ""

        if not task:
            self.output_edit.setPlainText("Please enter a task/question.")
            return

        mode, provider = self._get_selected_assistant()
        if mode == "public":
            self._run_public(task=task, editor_text=editor_text, provider_override=provider)
            return

        # Local Assistant: simple offline text operations (no external APIs)
        text = (editor_text or "").strip()
        if not text:
            self.output_edit.setPlainText(
                "Editor text is empty.\n\n"
                "Tip: First paste text into the editor, then try e.g.: 'Summarize into 5 points'."
            )
            return

        try:
            result = self._run_local(task=task, text=text)
        except Exception as e:
            result = f"Local assistant error: {e}"
        self.output_edit.setPlainText(result)

    def _set_initial_assistant_selection(self) -> None:
        settings = QSettings(ORG_NAME, APP_NAME)

        # 1) Prefer a persisted selection (survives app restarts)
        saved = str(settings.value(SETTINGS_ASSISTANT_SELECTED, "") or "").strip()
        if saved:
            for i in range(self.assistant_combo.count()):
                if str(self.assistant_combo.itemData(i) or "") == saved:
                    self.assistant_combo.setCurrentIndex(i)
                    return

        # 2) Fallback: If the dialog was opened in public mode, default to the selected public provider.
        if self.mode == "public":
            provider = self._get_selected_public_provider(settings) or "groq"
            target = f"public:{provider}"
        else:
            target = "local"

        for i in range(self.assistant_combo.count()):
            if self.assistant_combo.itemData(i) == target:
                self.assistant_combo.setCurrentIndex(i)
                break

    def _get_selected_assistant(self) -> tuple[str, str | None]:
        data = str(self.assistant_combo.currentData() or "local")
        if data.startswith("public:"):
            return "public", data.split(":", 1)[1]
        return "local", None

    def _on_assistant_changed(self) -> None:
        mode, provider = self._get_selected_assistant()
        if mode == "local":
            self.setWindowTitle("Local Assistant")
        else:
            p = (provider or "public").strip().lower()
            if p == "openai":
                title_provider = "OpenAI"
            elif p == "groq":
                title_provider = "Groq"
            elif p == "gemini":
                title_provider = "Gemini"
            else:
                title_provider = (provider or "public").capitalize()
            self.setWindowTitle(f"Public Assistant ({title_provider})")

        # Persist selection for next app start
        try:
            data = str(self.assistant_combo.currentData() or "local")
            QSettings(ORG_NAME, APP_NAME).setValue(SETTINGS_ASSISTANT_SELECTED, data)
        except Exception:
            pass

        # Do not write anything into Output automatically.

    def _help_clicked(self) -> None:
        mode, provider = self._get_selected_assistant()
        if mode == "local":
            self.output_edit.setPlainText(self._local_help_text())
            return
        self.output_edit.setPlainText(self._public_help_text(provider))

    @staticmethod
    def _public_help_text(provider: str | None) -> str:
        p = (provider or "").strip().lower() or "(auto)"
        return (
            "Public Assistant – nápověda:\n"
            f"- Provider: {p}\n"
            "- API klíč nastav v Settings → AI → Public\n"
            "- Podporováno: Groq, OpenAI, Gemini\n"
        )

    def _open_settings_clicked(self) -> None:
        if callable(self.open_settings):
            try:
                self.open_settings()
            except Exception as e:
                QMessageBox.warning(self, "Settings", f"Could not open settings: {e}")
        else:
            QMessageBox.information(self, "Settings", "Open Settings from the main window menu: Settings → Settings…")

    def _run_local(self, task: str, text: str) -> str:
        task_l = (task or "").strip().lower()

        if task_l in {"help", "?", "napoveda", "nápověda", "co umí", "co umi", "commands"}:
            return self._local_help_text()

        # Lightweight stopwords for scoring (CS + EN). Not exhaustive; tuned for robustness.
        stopwords = {
            # Czech
            "a", "aby", "ahoj", "aj", "ak", "ale", "ano", "asi", "aspoň", "az", "až",
            "bez", "bude", "budou", "by", "byl", "byla", "byli", "bylo", "být", "byť",
            "co", "což", "cz", "dnes", "do", "doc", "docs", "e", "email", "fakt", "hmm",
            "i", "jak", "jako", "je", "jeho", "jej", "její", "jejich", "jen", "ještě", "jsi", "jsme", "jsou", "jste",
            "k", "kam", "kde", "kdo", "kdy", "když", "ke", "která", "které", "který", "kteří",
            "kvůli", "li", "má", "mají", "mezi", "mít", "mně", "může", "můžou", "na", "nad", "ne", "nebo",
            "nej", "není", "nic", "nový", "nová", "nové", "noví", "o", "od", "on", "ona", "ono", "oni", "oproti",
            "po", "pod", "podle", "pokud", "proto", "protože", "pro", "před", "přes", "se", "si", "s", "stále",
            "tak", "taky", "takže", "ten", "tento", "tím", "to", "tohle", "tom", "tomu", "tu", "tuto", "tvůj", "tvoje",
            "u", "už", "v", "ve", "w", "www", "vy", "vás", "váš", "vaše", "vše", "však", "že",
            "z", "za", "ze",
            # English
            "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can", "could",
            "did", "do", "does", "done", "for", "from", "had", "has", "have", "he", "her", "here", "him", "his",
            "how", "i", "if", "in", "into", "is", "it", "its", "just", "may", "me", "more", "most", "my",
            "no", "not", "of", "on", "or", "our", "out", "she", "so", "than", "that", "the", "their", "them",
            "then", "there", "these", "they", "this", "to", "too", "up", "us", "was", "we", "were", "what", "when",
            "where", "which", "who", "will", "with", "you", "your",
        }

        # Common helpers
        def _non_empty_lines(src: str) -> list[str]:
            return [ln.rstrip() for ln in src.splitlines() if ln.strip()]

        def _parse_int_from_task(default: int) -> int:
            m = re.search(r"\b(\d{1,3})\b", task_l)
            if not m:
                return default
            try:
                val = int(m.group(1))
                return val if val > 0 else default
            except Exception:
                return default

        def _split_sentences(src: str) -> list[str]:
            s = re.sub(r"\s+", " ", (src or "").strip())
            if not s:
                return []
            # Split on common sentence terminators. Keep it simple and language-agnostic.
            parts = re.split(r"(?<=[\.!\?])\s+", s)
            out = []
            for p in parts:
                p = p.strip()
                if not p:
                    continue
                # Avoid treating short fragments as sentences
                if len(p) < 20 and not re.search(r"[A-Za-zÀ-ž]", p):
                    continue
                out.append(p)
            return out

        def _tokenize_words(src: str) -> list[str]:
            return [w.lower() for w in re.findall(r"[A-Za-zÀ-ž0-9]+", src or "")]

        def _extractive_summary(src: str, max_items: int, as_bullets: bool) -> str:
            sentences = _split_sentences(src)
            if not sentences:
                # Fallback to line-based if we can't split sentences.
                lines2 = _non_empty_lines(src)
                take = min(max_items, len(lines2))
                if take <= 0:
                    return "(Nothing to summarize.)"
                if as_bullets:
                    return "\n".join(f"- {lines2[i][:240]}" for i in range(take))
                return " ".join(lines2[:take])

            words = _tokenize_words(src)
            freq: dict[str, float] = {}
            for w in words:
                if len(w) <= 2:
                    continue
                if w in stopwords:
                    continue
                freq[w] = freq.get(w, 0.0) + 1.0
            if not freq:
                # If everything is stopwords, pick first sentences.
                take = min(max_items, len(sentences))
                picked = sentences[:take]
                return "\n".join(f"- {s}" for s in picked) if as_bullets else " ".join(picked)

            max_f = max(freq.values())
            for k in list(freq.keys()):
                freq[k] = freq[k] / max_f

            scored: list[tuple[float, int, str]] = []
            for idx, sent in enumerate(sentences):
                toks = _tokenize_words(sent)
                if not toks:
                    continue
                score = 0.0
                useful = 0
                for t in toks:
                    if len(t) <= 2 or t in stopwords:
                        continue
                    useful += 1
                    score += freq.get(t, 0.0)
                # Normalize and lightly penalize very long sentences
                if useful > 0:
                    score = score / useful
                score = score / (1.0 + max(0, (len(sent) - 220)) / 220.0)
                # Small boost for sentences that look like key statements
                if re.search(r"\b(důležité|hlavně|shrnutí|závěr|conclusion|key|important)\b", sent, re.IGNORECASE):
                    score *= 1.15
                scored.append((score, idx, sent))

            if not scored:
                take = min(max_items, len(sentences))
                picked = sentences[:take]
            else:
                take = min(max_items, len(scored))
                top = sorted(scored, key=lambda x: x[0], reverse=True)[:take]
                picked = [s for _, _, s in sorted(top, key=lambda x: x[1])]

            if as_bullets:
                return "\n".join(f"- {s}" for s in picked)
            return " ".join(picked)

        def _keywords(src: str, max_items: int) -> list[str]:
            toks = _tokenize_words(src)
            counts: dict[str, int] = {}
            for t in toks:
                if len(t) <= 3:
                    continue
                if t in stopwords:
                    continue
                counts[t] = counts.get(t, 0) + 1
            items = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
            return [w for w, _ in items[:max_items]]

        def _outline_from_headings(src: str, max_items: int) -> list[str]:
            out: list[str] = []
            for ln in (src or "").splitlines():
                s = ln.strip()
                if not s:
                    continue
                if s.startswith("#"):
                    out.append(s.lstrip("#").strip())
                    continue
                # Simple heuristic: all-caps or ends with ':'
                if (len(s) >= 6 and s.upper() == s and re.search(r"[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]", s)) or s.endswith(":"):
                    out.append(s.rstrip(":"))
            # Deduplicate while preserving order
            seen = set()
            uniq = []
            for item in out:
                if item in seen:
                    continue
                seen.add(item)
                uniq.append(item)
            return uniq[:max_items]

        # 1) Better summarization (extractive)
        lines = _non_empty_lines(text)
        if any(k in task_l for k in ["tldr", "tl;dr", "stručně", "strucne", "krátce", "kratce", "short summary"]):
            take = _parse_int_from_task(3)
            return _extractive_summary(text, max_items=take, as_bullets=True)

        if any(k in task_l for k in ["shrň", "shrnout", "summary", "summarize", "souhrn", "zhrň", "zhrnout"]):
            take = _parse_int_from_task(5)
            as_bullets = True
            if any(k in task_l for k in ["věty", "vety", "sentences", "odstavec", "paragraph"]):
                as_bullets = False
            return _extractive_summary(text, max_items=take, as_bullets=as_bullets)

        if any(k in task_l for k in ["odráž", "odraz", "body", "bullet", "bullets"]):
            take = _parse_int_from_task(8)
            return _extractive_summary(text, max_items=take, as_bullets=True)

        # 2) Cleanup / remove empty lines
        if any(k in task_l for k in ["vyčisti", "očisti", "ocisti", "cleanup", "trim"]):
            cleaned = "\n".join(ln.strip() for ln in text.splitlines())
            return cleaned

        if any(k in task_l for k in ["odstraň prázd", "odstran prazd", "remove empty", "drop empty"]):
            return "\n".join(_non_empty_lines(text))

        # 3) Basic stats
        if any(k in task_l for k in ["stat", "statistics", "počty", "pocty", "counts", "info"]):
            all_lines = text.splitlines()
            non_empty = _non_empty_lines(text)
            words = re.findall(r"\S+", text)
            return (
                f"Lines: {len(all_lines)}\n"
                f"Non-empty lines: {len(non_empty)}\n"
                f"Words: {len(words)}\n"
                f"Chars: {len(text)}"
            )

        # 4) Case conversions
        if any(k in task_l for k in ["uppercase", "upper", "velk", "velká", "velka"]):
            return text.upper()
        if any(k in task_l for k in ["lowercase", "lower", "mal", "malá", "mala"]):
            return text.lower()
        if any(k in task_l for k in ["titlecase", "title", "nadpis"]):
            return text.title()

        # 5) Sort / dedupe lines
        if any(k in task_l for k in ["seřaď", "serad", "sort"]):
            reverse = any(k in task_l for k in ["sestup", "desc", "reverse", "obrácen", "obracen"])
            sorted_lines = sorted(_non_empty_lines(text), key=lambda s: s.lower(), reverse=reverse)
            return "\n".join(sorted_lines)

        if any(k in task_l for k in ["unikátní", "unikatni", "dedupe", "unique"]):
            seen = set()
            out = []
            for ln in _non_empty_lines(text):
                key = ln
                if key in seen:
                    continue
                seen.add(key)
                out.append(ln)
            return "\n".join(out)

        # 6) Extract URLs / emails
        if any(k in task_l for k in ["url", "linky", "links", "odkazy"]):
            urls = re.findall(r"https?://[^\s\]\)\>\"']+", text)
            return "\n".join(urls) if urls else "(No URLs found.)"

        if any(k in task_l for k in ["email", "e-mail", "maily", "mail"]):
            emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
            return "\n".join(emails) if emails else "(No emails found.)"

        # 7) JSON pretty print
        if "json" in task_l and any(k in task_l for k in ["pretty", "format", "indent", "zformát", "zformat"]):
            try:
                obj = json.loads(text)
            except Exception as e:
                return f"Invalid JSON: {e}"
            return json.dumps(obj, ensure_ascii=False, indent=2)

        # 8) Find/replace
        # a) sed-like: s/old/new/
        m = re.search(r"\bs/(.*?)/(.*?)/\b", task, flags=re.IGNORECASE)
        if m:
            old, new = m.group(1), m.group(2)
            return text.replace(old, new)

        # b) replace "old" -> "new" or replace old -> new
        m = re.search(r"\breplace\s+(.+?)\s*(?:->|=>|with)\s*(.+)$", task, flags=re.IGNORECASE)
        if not m:
            m = re.search(r"\bnahrad\s+(.+?)\s*(?:->|=>|na)\s*(.+)$", task, flags=re.IGNORECASE)
        if m:
            old = m.group(1).strip().strip('"\'')
            new = m.group(2).strip().strip('"\'')
            if not old:
                return "Replace error: missing 'old' value."
            return text.replace(old, new)

        # 9) Keywords / outline
        if any(k in task_l for k in ["klíčová slova", "klicova slova", "keywords", "tagy", "tags"]):
            n = _parse_int_from_task(12)
            keys = _keywords(text, max_items=n)
            return "\n".join(f"- {k}" for k in keys) if keys else "(No keywords found.)"

        if any(k in task_l for k in ["osnova", "outline", "obsah", "headings", "nadpis"]):
            n = _parse_int_from_task(12)
            items = _outline_from_headings(text, max_items=n)
            return "\n".join(f"- {it}" for it in items) if items else "(No headings found.)"

        return self._local_help_text()

    @staticmethod
    def _local_help_text(task: str | None = None) -> str:
        base = (
            "Local Assistant (offline) – příkazy:\n"
            "- Nápověda: 'help' / 'nápověda'\n"
            "- Shrnutí: 'shrň 5' / 'summarize 5'\n"
            "- TL;DR: 'TL;DR 3' / 'stručně 3'\n"
            "- Odrážky: 'odrážky 8' / 'bullet points 8'\n"
            "- Vyčistit whitespace: 'vyčisti' / 'cleanup'\n"
            "- Odstranit prázdné řádky: 'remove empty lines'\n"
            "- Statistiky: 'stats' / 'počty'\n"
            "- Seřadit řádky: 'seřaď' / 'sort' (+ 'desc')\n"
            "- Unikátní řádky: 'unikátní' / 'dedupe'\n"
            "- Najít URL/email: 'url' / 'email'\n"
            "- Klíčová slova: 'klíčová slova 12' / 'keywords 12'\n"
            "- Osnova z nadpisů: 'osnova' / 'outline'\n"
            "- JSON pretty: 'json pretty'\n"
            "- Nahradit: 'replace A -> B' nebo 's/A/B/'\n"
        )
        return base

    def _run_public(self, task: str, editor_text: str, provider_override: str | None = None) -> None:
        settings = QSettings(ORG_NAME, APP_NAME)

        provider = (provider_override or "").strip().lower() or self._get_selected_public_provider(settings)
        
        if not provider:
            self.output_edit.setPlainText("No public AI provider selected.")
            return

        api_key = ""
        if provider == "groq":
            encrypted = str(settings.value(SETTINGS_AI_PUBLIC_GROQ_API_KEY, "") or "")
            api_key = _dpapi_decrypt_from_b64(encrypted).strip()
        elif provider == "openai":
            encrypted = str(settings.value(SETTINGS_AI_PUBLIC_OPENAI_API_KEY, "") or "")
            api_key = _dpapi_decrypt_from_b64(encrypted).strip()
        elif provider == "gemini":
            encrypted = str(settings.value(SETTINGS_AI_PUBLIC_GEMINI_API_KEY, "") or "")
            api_key = _dpapi_decrypt_from_b64(encrypted).strip()
        else:
            self.output_edit.setPlainText("No supported public AI provider selected.")
            return

        if not api_key:
            self.output_edit.setPlainText(
                f"Missing API key for {provider}.\n"
                "Go to Settings -> AI -> Public and enter the API key."
            )
            return

        prompt = self._build_public_prompt(task=task, editor_text=editor_text)

        self.setCursor(Qt.WaitCursor)
        self.task_edit.setEnabled(False)
        QApplication.processEvents()
        try:
            answer = ""
            if provider == "groq":
                answer = self._groq_generate(api_key=api_key, prompt=prompt)
            elif provider == "openai":
                answer = self._openai_generate(api_key=api_key, prompt=prompt)
            elif provider == "gemini":
                answer = self._gemini_generate(api_key=api_key, prompt=prompt)
            else:
                answer = f"Provider '{provider}' is not fully implemented yet."

            self.output_edit.setPlainText(self._clean_public_answer(answer))
        except Exception as e:
            self.output_edit.setPlainText(f"Public AI error: {e}")
        finally:
            self.task_edit.setEnabled(True)
            self.setCursor(Qt.ArrowCursor)

    def _get_selected_public_provider(self, settings: QSettings):
        if bool(settings.value(SETTINGS_AI_PUBLIC_GROQ_ENABLED, False, type=bool)):
            return "groq"
        if bool(settings.value(SETTINGS_AI_PUBLIC_OPENAI_ENABLED, False, type=bool)):
            return "openai"
        if bool(settings.value(SETTINGS_AI_PUBLIC_GEMINI_ENABLED, False, type=bool)):
            return "gemini"
        return None

    def _build_public_prompt(self, task: str, editor_text: str) -> str:
        text = (editor_text or "").strip()
        rules = (
            "RULES:\n"
            "- Output ONLY the final result.\n"
            "- Do NOT restate or echo the user task/instructions.\n"
            "- Do NOT include labels like 'Task:' / 'Úkol:' / 'Instruction:'.\n"
            "- Do NOT mention these rules.\n\n"
        )
        if text:
            return (
                rules
                + "User instruction:\n"
                + str(task)
                + "\n\nInput text:\n"
                + text
            )
        return rules + "User instruction:\n" + str(task)

    @staticmethod
    def _clean_public_answer(answer: str) -> str:
        # Some models still echo the instruction. Strip obvious leading label blocks.
        s = (answer or "").strip()
        if not s:
            return ""

        lines = s.splitlines()
        # Drop leading empty lines
        while lines and not lines[0].strip():
            lines.pop(0)

        if not lines:
            return ""

        # Remove a leading "Task:/Úkol:/Instruction:" line (and a following blank line)
        first = lines[0].strip().lower()
        if first.startswith("task:") or first.startswith("úkol:") or first.startswith("ukol:") or first.startswith("instruction:"):
            lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)

        return "\n".join(lines).strip()

    def _groq_select_model(self, api_key: str) -> str:
        # Prefer a stable, commonly-available chat model. If Groq changes model names,
        # we fall back to listing available models and picking the best match.
        preferences = [
            "llama-3.1-8b-instant",
            "llama-3.1-70b-versatile",
            "mixtral-8x7b-32768",
            "gemma2-9b-it",
        ]

        url = "https://api.groq.com/openai/v1/models"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Mozilla/5.0",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception:
            # If listing fails (network, auth, etc.), use the top preference.
            return preferences[0]

        try:
            body = json.loads(raw)
        except Exception:
            return preferences[0]

        data = body.get("data") or []
        ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
        for pref in preferences:
            if pref in ids:
                return pref
        return ids[0] if ids else preferences[0]

    def _groq_generate(self, api_key: str, prompt: str) -> str:
        # Cache the selected model for this dialog instance to avoid extra round-trips.
        model = getattr(self, "_groq_model", "") or self._groq_select_model(api_key)
        self._groq_model = model
        url = "https://api.groq.com/openai/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Mozilla/5.0"
            },
        )

        def _do_request() -> str:
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                raw_err = e.read().decode("utf-8", errors="replace")
                raise urllib.error.HTTPError(e.url, e.code, raw_err, e.hdrs, e.fp)

        try:
            raw = _do_request()
        except urllib.error.HTTPError as e:
            raw_err = str(e)
            # If the model is deprecated, pick a new model and retry once.
            try:
                body_err = json.loads(raw_err)
                err = (body_err.get("error") or {}) if isinstance(body_err, dict) else {}
                if err.get("code") == "model_decommissioned":
                    self._groq_model = self._groq_select_model(api_key)
                    payload["model"] = self._groq_model
                    data_retry = json.dumps(payload).encode("utf-8")
                    req.data = data_retry
                    raw = _do_request()
                else:
                    raise RuntimeError(f"HTTP {e.code}: {raw_err}")
            except Exception:
                raise RuntimeError(f"HTTP {e.code}: {raw_err}")

        body = json.loads(raw)
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("No choices in response")

        message = choices[0].get("message") or {}
        content = message.get("content") or ""

        if not content:
            raise RuntimeError("Empty response content")

        return content

    def _openai_select_model(self, api_key: str) -> str:
        preferences = [
            "gpt-4o-mini",
            "gpt-4o",
            "gpt-4.1-mini",
            "gpt-4.1",
        ]

        url = "https://api.openai.com/v1/models"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Mozilla/5.0",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return preferences[0]

        try:
            body = json.loads(raw)
        except Exception:
            return preferences[0]

        data = body.get("data") or []
        ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
        for pref in preferences:
            if pref in ids:
                return pref
        return ids[0] if ids else preferences[0]

    def _openai_generate(self, api_key: str, prompt: str) -> str:
        model = getattr(self, "_openai_model", "") or self._openai_select_model(api_key)
        self._openai_model = model

        url = "https://api.openai.com/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Mozilla/5.0",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode("utf-8", errors="replace")

            # Try to extract structured OpenAI error details
            try:
                body_err = json.loads(raw_err)
            except Exception:
                body_err = None

            code = None
            msg = None
            if isinstance(body_err, dict):
                err = body_err.get("error")
                if isinstance(err, dict):
                    code = err.get("code")
                    msg = err.get("message")

            if e.code == 429 and code == "insufficient_quota":
                raise RuntimeError(
                    "OpenAI odpovědělo 429: insufficient_quota – API key je valid, ale účet/organizace nemá aktivní billing nebo nemá dostupnou kvótu.\n\n"
                    "Co zkontrolovat:\n"
                    "- OpenAI Platform → Billing: zapnout platby / přidat platební metodu\n"
                    "- Jestli klíč patří do správného projektu/organizace\n"
                    "- Nastavené limity (monthly hard limit / budget)\n\n"
                    "Detail od API:\n"
                    + (msg or raw_err)
                )

            if e.code == 429 and code == "rate_limit_exceeded":
                raise RuntimeError(
                    "OpenAI odpovědělo 429: rate_limit_exceeded – příliš mnoho požadavků. Zkus to za chvíli, nebo sniž frekvenci/velikost promptu.\n\n"
                    "Detail od API:\n"
                    + (msg or raw_err)
                )

            raise RuntimeError(f"HTTP {e.code}: {raw_err}")

        body = json.loads(raw)
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("No choices in response")

        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if not content:
            raise RuntimeError("Empty response content")
        return content

    def _gemini_select_model(self, api_key: str) -> str:
        # Prefer a fast, cheap model; fall back if listing fails.
        preferences = [
            "gemini-1.5-flash",
            "gemini-1.5-pro",
        ]

        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={urllib.parse.quote(api_key)}"
        req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return preferences[0]

        try:
            body = json.loads(raw)
        except Exception:
            return preferences[0]

        models = body.get("models") or []
        names = []
        for m in models:
            if not isinstance(m, dict):
                continue
            name = m.get("name")
            if isinstance(name, str) and name.startswith("models/"):
                names.append(name.split("/", 1)[1])

        for pref in preferences:
            if pref in names:
                return pref
        return names[0] if names else preferences[0]

    def _gemini_generate(self, api_key: str, prompt: str) -> str:
        model = getattr(self, "_gemini_model", "") or self._gemini_select_model(api_key)
        self._gemini_model = model

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(api_key)}"
        payload = {
            "contents": [
                {"role": "user", "parts": [{"text": prompt}]}
            ],
            "generationConfig": {
                "temperature": 0.7,
            },
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode("utf-8", errors="replace")
            # Gemini errors are typically JSON with {"error": {"message": ...}}
            try:
                body_err = json.loads(raw_err)
            except Exception:
                body_err = None
            msg = None
            if isinstance(body_err, dict):
                err = body_err.get("error")
                if isinstance(err, dict):
                    msg = err.get("message")
            raise RuntimeError(f"HTTP {e.code}: {msg or raw_err}")

        body = json.loads(raw)
        candidates = body.get("candidates") or []
        if not candidates:
            raise RuntimeError("No candidates in response")

        content = (candidates[0].get("content") or {}) if isinstance(candidates[0], dict) else {}
        parts = content.get("parts") or []
        if not parts:
            raise RuntimeError("Empty response content")

        # parts can contain multiple chunks; concatenate text parts
        out = []
        for p in parts:
            if isinstance(p, dict) and p.get("text"):
                out.append(str(p.get("text")))
        text_out = "".join(out).strip()
        if not text_out:
            raise RuntimeError("Empty response text")
        return text_out


class _OcrOnceController(QObject):
    """Runs OCR for one payload using the existing persistent OCR subprocess worker."""

    ocr_request = Signal(object)
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = _OcrSubprocessWorker()
        self._worker.moveToThread(self._thread)
        self.ocr_request.connect(self._worker.run)
        self._worker.finished.connect(self.finished)
        self._worker.error.connect(self.error)
        self._thread.start()

    def shutdown(self) -> None:
        try:
            self._worker.shutdown()
        except Exception:
            pass
        try:
            if self._thread.isRunning():
                self._thread.quit()
                self._thread.wait(1500)
        except Exception:
            pass


class OcrAssistantFlow(QObject):
    """Capture -> OCR -> open Assistant (skip EditorWindow UI)."""

    def __init__(self, tray_app, pixmap: QPixmap, parent=None):
        super().__init__(parent)
        self.tray_app = tray_app
        self.pixmap = pixmap
        self.settings = QSettings(ORG_NAME, APP_NAME)
        self._source_text = ""
        self._temp_path = None

        self.dialog = AssistantDialog(
            mode="local",
            get_editor_text=lambda: self._source_text,
            set_editor_text=None,
            open_settings=lambda: self.tray_app.show_settings("general"),
            parent=None,
        )
        self.dialog.output_edit.setPlainText("Running OCR…")
        self.dialog.set_run_enabled(False)

        self._ocr = _OcrOnceController()
        self._ocr.finished.connect(self._on_ocr_finished)
        self._ocr.error.connect(self._on_ocr_error)
        self.dialog.destroyed.connect(lambda *_: self.shutdown())

    def show(self) -> None:
        self.dialog.show()
        try:
            self.dialog.raise_()
            self.dialog.activateWindow()
            self.dialog.task_edit.setFocus()
        except Exception:
            pass
        QTimer.singleShot(0, self.start_ocr)

    def shutdown(self) -> None:
        try:
            if getattr(self, "_ocr", None) is not None:
                self._ocr.shutdown()
        except Exception:
            pass
        self._ocr = None
        self._cleanup_temp()

    def _cleanup_temp(self) -> None:
        temp_path = getattr(self, "_temp_path", None)
        self._temp_path = None
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    def _compute_ocr_scale(self) -> int:
        use_auto_scale = bool(self.settings.value(SETTINGS_OCR_SCALE_AUTO, False, type=bool))
        try:
            scale = int(self.settings.value(SETTINGS_OCR_SCALE, 2) or 2)
        except Exception:
            scale = 2

        if use_auto_scale:
            try:
                w = int(self.pixmap.width())
                h = int(self.pixmap.height())
            except Exception:
                w, h = 0, 0
            area = w * h
            if area <= 0:
                scale = 2
            elif area < 700_000:
                scale = 3
            elif area < 2_200_000:
                scale = 2
            else:
                scale = 1

        return max(1, min(int(scale), 4))

    def start_ocr(self) -> None:
        # Always auto-run OCR in this flow.
        try:
            self.dialog.output_edit.setPlainText("Running OCR…")
            self.dialog.set_run_enabled(False)
        except Exception:
            pass

        use_in_memory = bool(self.settings.value(SETTINGS_OCR_IN_MEMORY, False, type=bool))
        use_greedy = bool(self.settings.value(SETTINGS_OCR_USE_GREEDY, False, type=bool))
        scale = self._compute_ocr_scale()

        payload = {
            "use_greedy": use_greedy,
            "image_path": None,
            "png_b64": None,
            "scale": scale,
        }

        self._cleanup_temp()

        if use_in_memory:
            buf = QBuffer()
            buf.open(QBuffer.ReadWrite)
            ok = self.pixmap.save(buf, "PNG")
            if not ok:
                self._on_ocr_error("OCR Error: Could not encode image.")
                return
            payload["png_b64"] = base64.b64encode(bytes(buf.data())).decode("ascii")
            buf.close()
        else:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                self._temp_path = f.name
            self.pixmap.save(self._temp_path)
            if not self._temp_path or not os.path.exists(self._temp_path) or os.path.getsize(self._temp_path) == 0:
                self._on_ocr_error("OCR Error: Could not save image.")
                return
            payload["image_path"] = self._temp_path

        self._ocr.ocr_request.emit(payload)

    def _on_ocr_finished(self, out_text: str) -> None:
        self._source_text = (out_text or "").strip()
        self._cleanup_temp()

        if self._source_text:
            self.dialog.output_edit.setPlainText("OCR ready. Enter task and press Run.")
        else:
            self.dialog.output_edit.setPlainText("OCR finished: no text detected. You can still ask a question.")

        self.dialog.set_run_enabled(True)
        try:
            self.dialog.task_edit.setFocus()
        except Exception:
            pass

    def _on_ocr_error(self, message: str) -> None:
        self._cleanup_temp()
        self._source_text = ""
        self.dialog.output_edit.setPlainText(str(message or "OCR failed"))
        # Still allow using Assistant for unrelated questions.
        self.dialog.set_run_enabled(True)
        try:
            self.dialog.task_edit.setFocus()
        except Exception:
            pass


class EditorWindow(QWidget):
    ocr_request = Signal(object)
    enhance_request = Signal(str)
    ocr_shutdown = Signal()

    def __init__(self, pixmap: QPixmap, tray_app=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Editor")
        self.setWindowIcon(QIcon(resource_path("ico.ico")))
        # Ensure the QWidget is deleted when closed (prevents keeping heavy resources alive)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.resize(800, 600)
        
        self.original_pixmap = pixmap
        self.tray_app = tray_app
        self.settings_dialog = None
        
        layout = QVBoxLayout()
        
        # --- Menu Bar ---
        menu_bar = QMenuBar(self)
        file_menu = menu_bar.addMenu("File")
        
        # Save
        save_action = QAction("Save Image...", self)
        save_action.setShortcut(QKeySequence.Save)
        save_action.triggered.connect(self.save_image)
        file_menu.addAction(save_action)
        
        save_txt_action = QAction("Save as TXT", self)
        save_txt_action.triggered.connect(self.save_text_as_txt)
        file_menu.addAction(save_txt_action)

        # Copy image (no Ctrl+C, that belongs to text)
        copy_action = QAction("Copy image", self)
        copy_action.triggered.connect(self.copy_to_clipboard)
        file_menu.addAction(copy_action)
        
        file_menu.addSeparator()
        
        # Exit
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Tools Menu
        tools_menu = menu_bar.addMenu("Tools")

        # Edit Menu
        edit_menu = menu_bar.addMenu("Edit")

        copy_text_action = QAction("Copy", self)
        copy_text_action.setShortcut(QKeySequence.Copy)
        # Make sure Ctrl+C works even if focus/menus behave oddly on QWidget+QMenuBar.
        copy_text_action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        copy_text_action.triggered.connect(self.copy_text_to_clipboard)
        self.addAction(copy_text_action)
        edit_menu.addAction(copy_text_action)

        undo_action = QAction("Undo", self)
        undo_action.setShortcut(QKeySequence.Undo)  # Ctrl+Z
        undo_action.triggered.connect(lambda: self.text_edit.undo())
        edit_menu.addAction(undo_action)

        redo_action = QAction("Redo", self)
        redo_action.setShortcut(QKeySequence.Redo)  # Ctrl+Y
        redo_action.triggered.connect(lambda: self.text_edit.redo())
        edit_menu.addAction(redo_action)

        clear_action = QAction("Clear", self)
        clear_action.triggered.connect(self.clear_text_confirm)
        edit_menu.addAction(clear_action)

        # AI Tools Menu
        ai_menu = menu_bar.addMenu("AI Tools")

        # Settings Menu
        settings_menu = menu_bar.addMenu("Settings")

        editor_settings_action = QAction("Settings...", self)
        editor_settings_action.triggered.connect(self.open_settings)
        settings_menu.addAction(editor_settings_action)
        
        ocr_action = QAction("Extract Text (OCR)", self)
        ocr_action.triggered.connect(self.run_ocr)
        tools_menu.addAction(ocr_action)

        assistant_action = QAction("Assistant...", self)
        assistant_action.triggered.connect(self.open_assistant)
        ai_menu.addAction(assistant_action)

        enhance_action = QAction("Enhance image...", self)
        enhance_action.triggered.connect(self.enhance_image)
        tools_menu.addAction(enhance_action)
        
        layout.setMenuBar(menu_bar)
        
        # --- Splitter (Image + Text) ---
        self.splitter = QSplitter(Qt.Vertical)
        
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        # Allow shrinking below implicit size
        self.image_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        # self.image_label.setPixmap(pixmap) 
        
        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("Notes...")
        # Explicitly bind undo/redo shortcuts (QTextEdit usually has them, but keep consistent)
        self.text_edit.setUndoRedoEnabled(True)
        
        self.splitter.addWidget(self.image_label)
        self.splitter.addWidget(self.text_edit)
        
        # Restore splitter state/sizes
        self.settings = QSettings(ORG_NAME, APP_NAME)
        saved_state = self.settings.value("editor_splitter_state")
        if saved_state:
            self.splitter.restoreState(saved_state)
        
        layout.addWidget(self.splitter)
        self.setLayout(layout)
        
        # Install event filter to handle image resizing when splitter moves
        self.image_label.installEventFilter(self)

        self._assistant_dialog = None

        # OCR worker thread (runs OCR in a separate process to avoid keeping Torch/EasyOCR RAM in the tray process)
        self._ocr_thread = QThread(self)
        self._ocr_worker = _OcrSubprocessWorker()
        self._ocr_worker.moveToThread(self._ocr_thread)
        self.ocr_request.connect(self._ocr_worker.run)
        self.ocr_shutdown.connect(self._ocr_worker.shutdown)
        self._ocr_worker.finished.connect(self._on_ocr_finished)
        self._ocr_worker.error.connect(self._on_ocr_error)
        self._ocr_thread.start()

        self._ocr_progress = None
        self._ocr_temp_path = None

        # Enhance worker thread
        self._enhance_thread = QThread(self)
        self._enhance_worker = _EnhanceWorker()
        self._enhance_worker.moveToThread(self._enhance_thread)
        self.enhance_request.connect(self._enhance_worker.run)
        self._enhance_worker.finished.connect(self._on_enhance_finished)
        self._enhance_worker.error.connect(self._on_enhance_error)
        self._enhance_thread.start()

        self._enhance_progress = None
        self._enhance_temp_path = None

    def copy_text_to_clipboard(self) -> None:
        # QTextEdit.copy() copies only selection; if user has no selection,
        # copy the whole text to match common expectations.
        try:
            cursor = self.text_edit.textCursor()
            if cursor is not None and cursor.hasSelection():
                self.text_edit.copy()
            else:
                (QApplication.clipboard()).setText(self.text_edit.toPlainText() or "")
        except Exception:
            # Fallback: best-effort
            try:
                (QApplication.clipboard()).setText(self.text_edit.toPlainText() or "")
            except Exception:
                pass

    def open_settings(self):
        if self.tray_app and hasattr(self.tray_app, "show_settings"):
            self.tray_app.show_settings("general")
            return

        if not self.settings_dialog:
            settings = QSettings(ORG_NAME, APP_NAME)
            self.settings_dialog = SettingsDialog(settings=settings, tray_app=None, parent=self)

        self.settings_dialog.show_tab("general")
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def eventFilter(self, source, event):
        if source == self.image_label and event.type() == QEvent.Resize:
            self.update_image_display()
        return super().eventFilter(source, event)

    def update_image_display(self):
        if not self.original_pixmap.isNull() and self.image_label.size().isValid():
            w = self.image_label.width()
            h = self.image_label.height()
            if w > 0 and h > 0:
                scaled = self.original_pixmap.scaled(
                    w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
                self.image_label.setPixmap(scaled)

    def closeEvent(self, event):
        # Save splitter state
        self.settings.setValue("editor_splitter_state", self.splitter.saveState())
        try:
            if hasattr(self, "_ocr_thread") and self._ocr_thread and self._ocr_thread.isRunning():
                try:
                    self.ocr_shutdown.emit()
                except Exception:
                    pass
                self._ocr_thread.quit()
                self._ocr_thread.wait(1500)
        except Exception:
            pass

        try:
            if hasattr(self, "_enhance_thread") and self._enhance_thread and self._enhance_thread.isRunning():
                self._enhance_thread.quit()
                self._enhance_thread.wait(1500)
        except Exception:
            pass

        # Drop TrayApp's reference so this window can be garbage-collected.
        try:
            if self.tray_app is not None and getattr(self.tray_app, "editor_window", None) is self:
                self.tray_app.editor_window = None
        except Exception:
            pass
        super().closeEvent(event)

    def save_image(self):
        file_name, _ = QFileDialog.getSaveFileName(
            self,
            "Save Image",
            os.path.join(os.path.expanduser("~"), "Pictures", "screenshot.png"),
            "Images (*.png *.jpg *.bmp);;All Files (*)"
        )
        if file_name:
            self.original_pixmap.save(file_name)

    def _get_editor_text_for_assistant(self) -> str:
        return self.text_edit.toPlainText()

    def _set_editor_text_for_assistant(self, text: str) -> None:
        self.text_edit.setPlainText(text)

    def open_assistant(self):
        if not self._assistant_dialog:
            # Single dialog that can switch between Local/Public via the dropdown.
            self._assistant_dialog = AssistantDialog(
                mode="local",
                get_editor_text=self._get_editor_text_for_assistant,
                set_editor_text=self._set_editor_text_for_assistant,
                open_settings=self.open_settings,
                parent=self,
            )
        self._assistant_dialog.show()
        self._assistant_dialog.raise_()
        self._assistant_dialog.activateWindow()

    def run_ocr(self):
        if self._ocr_progress is not None:
            return

        self.setCursor(Qt.WaitCursor)
        QApplication.processEvents()

        temp_path = None
        try:
            use_in_memory = bool(self.settings.value(SETTINGS_OCR_IN_MEMORY, False, type=bool))
            use_greedy = bool(self.settings.value(SETTINGS_OCR_USE_GREEDY, False, type=bool))

            use_auto_scale = bool(self.settings.value(SETTINGS_OCR_SCALE_AUTO, False, type=bool))

            try:
                scale = int(self.settings.value(SETTINGS_OCR_SCALE, 2) or 2)
            except Exception:
                scale = 2

            if use_auto_scale:
                # Simple heuristic: larger selections benefit from speed (lower scale),
                # small selections benefit from accuracy (higher scale).
                try:
                    w = int(self.original_pixmap.width())
                    h = int(self.original_pixmap.height())
                except Exception:
                    w, h = 0, 0
                area = w * h
                if area <= 0:
                    scale = 2
                elif area < 700_000:  # e.g. ~1000x700
                    scale = 3
                elif area < 2_200_000:  # e.g. ~1920x1080
                    scale = 2
                else:
                    scale = 1

            scale = max(1, min(scale, 4))

            payload = {
                "use_greedy": use_greedy,
                "image_path": None,
                "png_b64": None,
                "scale": scale,
            }

            if use_in_memory:
                buf = QBuffer()
                buf.open(QBuffer.ReadWrite)
                ok = self.original_pixmap.save(buf, "PNG")
                if not ok:
                    self.text_edit.append("Error: Could not encode image.")
                    return
                payload["png_b64"] = base64.b64encode(bytes(buf.data())).decode("ascii")
                buf.close()
                self._ocr_temp_path = None
            else:
                # Save current image to temp file for OCR
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                    temp_path = f.name

                self.original_pixmap.save(temp_path)
                if os.path.getsize(temp_path) == 0:
                    self.text_edit.append("Error: Saved image file is empty.")
                    return

                self._ocr_temp_path = temp_path
                payload["image_path"] = temp_path

            # Progress window (indeterminate)
            dlg = QProgressDialog("Running OCR…", "", 0, 0, self)
            dlg.setWindowTitle("OCR")
            dlg.setWindowModality(Qt.WindowModal)
            dlg.setCancelButton(None)
            dlg.setMinimumDuration(0)
            dlg.show()
            self._ocr_progress = dlg

            # Kick off OCR in worker thread
            self.ocr_request.emit(payload)
        except Exception as e:
            error_msg = f"OCR Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}"
            self.text_edit.append(error_msg)
            print(error_msg)
            self._cleanup_ocr_ui_and_temp()

    def _cleanup_ocr_ui_and_temp(self) -> None:
        try:
            if self._ocr_progress is not None:
                self._ocr_progress.close()
        except Exception:
            pass
        self._ocr_progress = None
        self.setCursor(Qt.ArrowCursor)

        temp_path = getattr(self, "_ocr_temp_path", None)
        self._ocr_temp_path = None
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    def _on_ocr_finished(self, out_text: str) -> None:
        try:
            if out_text:
                self.text_edit.append(out_text)
            else:
                self.text_edit.append("No text detected.")
        finally:
            self._cleanup_ocr_ui_and_temp()

    def _on_ocr_error(self, message: str) -> None:
        try:
            # Keep current behavior: append detailed error
            self.text_edit.append(message)
            print(message)
        finally:
            self._cleanup_ocr_ui_and_temp()

    def enhance_image(self) -> None:
        if self._enhance_progress is not None:
            return

        self.setCursor(Qt.WaitCursor)
        QApplication.processEvents()

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                temp_path = f.name

            self.original_pixmap.save(temp_path)
            if os.path.getsize(temp_path) == 0:
                QMessageBox.warning(self, "Enhance", "Error: Saved image file is empty.")
                return

            self._enhance_temp_path = temp_path

            dlg = QProgressDialog("Enhancing image…", "", 0, 0, self)
            dlg.setWindowTitle("Enhance")
            dlg.setWindowModality(Qt.WindowModal)
            dlg.setCancelButton(None)
            dlg.setMinimumDuration(0)
            dlg.show()
            self._enhance_progress = dlg

            self.enhance_request.emit(temp_path)
        except Exception as e:
            msg = f"Enhance Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}"
            QMessageBox.warning(self, "Enhance", msg)
            self._cleanup_enhance_ui_and_temp()

    def _cleanup_enhance_ui_and_temp(self) -> None:
        try:
            if self._enhance_progress is not None:
                self._enhance_progress.close()
        except Exception:
            pass
        self._enhance_progress = None
        self.setCursor(Qt.ArrowCursor)

        temp_path = getattr(self, "_enhance_temp_path", None)
        self._enhance_temp_path = None
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    def _on_enhance_finished(self, png_bytes: bytes) -> None:
        try:
            pix = QPixmap()
            ok = pix.loadFromData(png_bytes, "PNG")
            if not ok or pix.isNull():
                QMessageBox.warning(self, "Enhance", "Enhance finished, but image could not be loaded.")
                return

            self.original_pixmap = pix
            self.update_image_display()
        finally:
            self._cleanup_enhance_ui_and_temp()

    def _on_enhance_error(self, message: str) -> None:
        try:
            QMessageBox.warning(self, "Enhance", message)
            print(message)
        finally:
            self._cleanup_enhance_ui_and_temp()

    def copy_to_clipboard(self):
        clipboard = QApplication.clipboard()
        clipboard.setPixmap(self.original_pixmap)

    def clear_text_confirm(self) -> None:
        answer = QMessageBox.question(
            self,
            "Clear",
            "Opravdu chceš vymazat text v editoru?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.text_edit.clear()

    def save_text_as_txt(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save as TXT",
            os.path.join(os.path.expanduser("~"), "Documents", "text.txt"),
            "Text files (*.txt);;All Files (*)",
        )
        if not path:
            return
        try:
            text = self.text_edit.toPlainText() or ""
            with open(path, "w", encoding="utf-8", errors="replace") as f:
                f.write(text)
        except Exception as e:
            QMessageBox.warning(self, "Save as TXT", f"Could not save file: {e}")

        
    def showEvent(self, event):
        # Force initial update
        self.update_image_display()
        super().showEvent(event)


class SnippingOverlay(QWidget):
    capture_taken = Signal(QPixmap)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WA_DeleteOnClose)
        
        # Capture full screen (all monitors)
        screens = QApplication.screens()
        
        # Determine total geometry of all monitors
        total_rect = QRect()
        for screen in screens:
            total_rect = total_rect.united(screen.geometry())
        self.total_rect = total_rect
            
        # Determine largest pixel ratio (scale factor)
        self.max_dpr = max(s.devicePixelRatio() for s in screens) if screens else 1.0
        
        # Store screen captures and their layout info
        self.captures = []
        
        # Sort screens by logical X position to determine physical layout order
        # This helps in reconstructing a continuous physical image
        sorted_screens = sorted(screens, key=lambda s: (s.geometry().y(), s.geometry().x()))
        
        current_phys_x = 0
        max_phys_y = 0

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Monitor detection:")
        for i, screen in enumerate(sorted_screens):
            geo = screen.geometry()
            dpr = screen.devicePixelRatio()
            print(f"  Monitor {i}: Logical geom={geo.getRect()}, DPR={dpr}")
        
        for screen in sorted_screens:
            # Grab raw physical pixels
            pix = screen.grabWindow(0)
            
            # Set DPR for pixmap to match monitor
            # This ensures logical size of pixmap matches logical geometry of window
            pix.setDevicePixelRatio(screen.devicePixelRatio())
            
            # Logical geometry relative to the overlay window
            log_rect = screen.geometry()
            rel_rect = QRect(
                log_rect.x() - total_rect.x(),
                log_rect.y() - total_rect.y(),
                log_rect.width(),
                log_rect.height()
            )
            
            # Estimate physical origin based on flow
            phys_rect = QRect(current_phys_x, 0, pix.width(), pix.height())
            
            self.captures.append({
                'pixmap': pix,
                'log_rect': rel_rect,   # For painting on screen (visual)
                'phys_rect': phys_rect, # For stitching info (backup)
                'screen': screen
            })
            
            # For physical offset shift use physical width (width * DPR if using logical, but pix.width() is raw)
            # pix.width() returns logical width if DPR is set!
            # Must get raw size
            raw_width = int(pix.width() * pix.devicePixelRatio())
            current_phys_x += raw_width
            
            raw_height = int(pix.height() * pix.devicePixelRatio())
            max_phys_y = max(max_phys_y, raw_height)

        # Determine total physical canvas size
        self.phys_total_w = current_phys_x
        self.phys_total_h = max_phys_y
        
        self.setCursor(QCursor(Qt.CrossCursor))
        
        self.start_pos = None
        self.end_pos = None
        self.is_selecting = False
        
    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.start_pos = event.position().toPoint()
            self.is_selecting = True
            self.end_pos = self.start_pos
            self.update()
        event.accept()
    
    def mouseMoveEvent(self, event):  # noqa: N802
        if self.is_selecting:
            self.end_pos = event.position().toPoint()
            self.update()
        event.accept()
    
    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton and self.is_selecting:
            self.is_selecting = False
            self.end_pos = event.position().toPoint()
            
            selection_rect = QRect(self.start_pos, self.end_pos).normalized()
            
            if selection_rect.width() > 10 and selection_rect.height() > 10:
                self.process_capture(selection_rect)
                self.close()
            else:
                self.update()
        event.accept()
        
    def process_capture(self, selection_rect):
        final_fragments = []
        total_output_width = 0
        max_output_height = 0
        
        # Sort captures by visual X order again to ensure left-to-right stitching
        sorted_captures = sorted(self.captures, key=lambda c: c['log_rect'].x())
        
        for cap in sorted_captures:
            # Check intersection in LOGICAL space
            intersect = selection_rect.intersected(cap['log_rect'])
            if not intersect.isEmpty():
                # Map intersection to Source Physical Coords
                rx = intersect.x() - cap['log_rect'].x()
                ry = intersect.y() - cap['log_rect'].y()
                rw = intersect.width()
                rh = intersect.height()
                
                # Scale factors
                # pixmap width is logical if DPR is set.
                pix_log_w = cap['pixmap'].width()
                pix_log_h = cap['pixmap'].height()
                
                sx = pix_log_w / cap['log_rect'].width()
                sy = pix_log_h / cap['log_rect'].height()
                
                # Pro výpočet zdrojového výřezu v raw pixelech musíme zohlednit DPR
                dpr = cap['pixmap'].devicePixelRatio()
                
                # src_x/y v logických souřadnicích pixmapy
                log_src_x = rx * sx
                log_src_y = ry * sy
                log_src_w = rw * sx
                log_src_h = rh * sy
                
                # Převod na fyzické pixely pro copy()
                # copy() bere argumenty v logických souřadnicích, pokud je DPR nastaven?
                # Ne, QPixmap.copy() bere souřadnice v pixelech (záleží na verzi Qt, ale obvykle device dependent pixels)
                # Ale pokud je DPR nastaven, chování se může lišit.
                # Jistota: Copy z raw kopie s DPR 1.0
                
                temp_pix = QPixmap(cap['pixmap'])
                temp_pix.setDevicePixelRatio(1.0)
                
                raw_src_x = int(log_src_x * dpr)
                raw_src_y = int(log_src_y * dpr)
                raw_src_w = int(log_src_w * dpr)
                raw_src_h = int(log_src_h * dpr)
                
                src_x = max(0, raw_src_x)
                src_y = max(0, raw_src_y)
                src_w = min(raw_src_w, temp_pix.width() - src_x)
                src_h = min(raw_src_h, temp_pix.height() - src_y)
                
                if src_w > 0 and src_h > 0:
                    fragment = temp_pix.copy(src_x, src_y, src_w, src_h)
                    final_fragments.append(fragment)
                    total_output_width += src_w
                    max_output_height = max(max_output_height, src_h)
        
        if not final_fragments:
            return

        result = QPixmap(total_output_width, max_output_height)
        result.fill(Qt.transparent)
        result.setDevicePixelRatio(1.0)
        
        painter = QPainter(result)
        current_x = 0
        
        for frag in final_fragments:
             painter.drawPixmap(current_x, 0, frag)
             current_x += frag.width()
             
        painter.end()
        self.capture_taken.emit(result)
        
    def show_fullscreen_custom(self):
        # Instead of classic showFullScreen(), which often takes only primary monitor,
        # manually set geometry across all monitors.
        self.setGeometry(self.total_rect)
        self.show()
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Overlay shown with geometry: {self.geometry()}")
        self.raise_()
        self.activateWindow()
        self.setFocus()
        
    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        
        # Draw each screen capture in its logical position
        for cap in self.captures:
            painter.drawPixmap(cap['log_rect'], cap['pixmap'])
        
        # Poloprůhledný černý overlay
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))
        
        if self.start_pos and self.end_pos:
            # Vybraná oblast (logické souřadnice okna)
            selection_rect = QRect(self.start_pos, self.end_pos).normalized()
            
            # Vykreslit "čistou" část obrázku do výběru
            # We must iterate again and clip painter to selection
            painter.setClipRect(selection_rect)
            for cap in self.captures:
                painter.drawPixmap(cap['log_rect'], cap['pixmap'])
            painter.setClipping(False)
            
            # Nakreslit rámeček kolem výběru
            settings = QSettings(ORG_NAME, APP_NAME)
            border_color = str(settings.value(SETTINGS_SNIP_BORDER_COLOR, DEFAULT_SNIP_BORDER_COLOR) or DEFAULT_SNIP_BORDER_COLOR)
            try:
                border_width = int(settings.value(SETTINGS_SNIP_BORDER_WIDTH, DEFAULT_SNIP_BORDER_WIDTH) or DEFAULT_SNIP_BORDER_WIDTH)
            except Exception:
                border_width = DEFAULT_SNIP_BORDER_WIDTH

            color = QColor(border_color)
            if not color.isValid():
                color = QColor(DEFAULT_SNIP_BORDER_COLOR)
            border_width = max(1, min(int(border_width), 10))

            pen = QPen(color, border_width)
            painter.setPen(pen)
            painter.drawRect(selection_rect)
    
    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()
        event.accept()


class TrayApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        # Single instance check
        self.shared_memory = QSharedMemory("TxtOnScrn_SingleInstance")
        if not self.shared_memory.create(1):
            # Instance already running, notify it to show config
            self.notify_existing_instance()
            sys.exit(0)

        # Setup local server to receive commands
        self.local_server = QLocalServer()
        QLocalServer.removeServer("TxtOnScrn_LocalServer")
        self.local_server.listen("TxtOnScrn_LocalServer")
        self.local_server.newConnection.connect(self.on_new_connection)

        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.current_hotkey = None
        self.editor_window = None
        self.ocr_assistant_flow = None

        # Apply appearance theme early
        apply_theme_mode(self.settings.value(SETTINGS_THEME_MODE, "system"))

        # Signal for communication between keyboard thread and GUI thread
        self.signal_handler = HotkeySignal()
        self.signal_handler.hotkey_pressed.connect(self.on_hotkey_main_thread)

        icon_path = resource_path("ico.ico")
        self.tray_icon = QSystemTrayIcon(QIcon(icon_path))

        self.settings_window = SettingsDialog(settings=self.settings, tray_app=self)

        menu = QMenu()
        config_action = menu.addAction("Config")
        quit_action = menu.addAction("Quit")
        config_action.triggered.connect(lambda: self.show_settings("general"))
        quit_action.triggered.connect(self.quit)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self.on_tray_icon_activated)
        self.tray_icon.show()

        self.register_hotkey()
        
        self.snipping_overlay = None

    def notify_existing_instance(self):
        """Notify existing instance to show config window"""
        socket = QLocalSocket()
        socket.connectToServer("TxtOnScrn_LocalServer")
        if socket.waitForConnected(1000):
            socket.write(b"show_config")
            socket.flush()
            socket.waitForBytesWritten(1000)
            socket.disconnectFromServer()

    def on_new_connection(self):
        """Handle connection from new instance"""
        client = self.local_server.nextPendingConnection()
        if client:
            client.waitForReadyRead(1000)
            data = client.readAll().data()
            if data == b"show_config":
                self.show_settings("general")
            client.disconnectFromServer()

    def register_hotkey(self):
        if self.current_hotkey:
            try:
                keyboard.remove_hotkey(self.current_hotkey)
            except:
                pass
        
        hotkey = self.settings.value(SETTINGS_HOTKEY, DEFAULT_HOTKEY)
        self.current_hotkey = hotkey.lower().replace("ctrl", "control")
        
        try:
            keyboard.add_hotkey(self.current_hotkey, self.on_hotkey_pressed)
        except:
            pass

    def on_hotkey_pressed(self):
        # Keyboard library callback runs in another thread
        # Must emit signal for GUI thread
        self.signal_handler.hotkey_pressed.emit()

    def on_hotkey_main_thread(self):
        # Toggle overlay: if running, close; else open
        
        # Check existing instance
        if self.snipping_overlay:
            try:
                # Zkusíme zjistit, zda je okno viditelné
                # Pokud bylo C++ objektem smazáno, vyhodí toto RuntimeError
                if self.snipping_overlay.isVisible():
                    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Overlay closed")
                    self.snipping_overlay.close()
                    return
            except RuntimeError:
                # Objekt byl smazán, ale reference zůstala. Vyčistíme.
                self.snipping_overlay = None

        # Create and show new overlay for selection
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Overlay enabled")
        self.snipping_overlay = SnippingOverlay()
        # Clear reference after closing
        try:
            self.snipping_overlay.destroyed.connect(self._overlay_destroyed)
            self.snipping_overlay.capture_taken.connect(self.open_editor)
        except Exception:
            pass
        self.snipping_overlay.show_fullscreen_custom()
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Overlay shown")

    def open_editor(self, pixmap: QPixmap):
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Opening editor")

        # Optional: open Assistant directly (skip editor) and feed OCR text as its input context
        try:
            raw_flag = self.settings.value(SETTINGS_OCR_ASSISTANT_AFTER_CAPTURE, False)
            if isinstance(raw_flag, bool):
                flag = raw_flag
            else:
                s = str(raw_flag).strip().lower()
                flag = s in {"1", "true", "yes", "y", "on"}
            print(
                f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] assistant_after_capture={raw_flag!r} ({type(raw_flag).__name__}) -> {flag}"
            )

            if flag:
                if self.ocr_assistant_flow is not None:
                    try:
                        self.ocr_assistant_flow.shutdown()
                    except Exception:
                        pass
                self.ocr_assistant_flow = OcrAssistantFlow(tray_app=self, pixmap=pixmap)
                self.ocr_assistant_flow.show()
                return
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] assistant_after_capture failed: {e}\n{traceback.format_exc()}")

        # If editor already exists, close or update it
        if self.editor_window:
            try:
                self.editor_window.close()
            finally:
                # Ensure we don't keep a stale reference if close does not immediately delete.
                self.editor_window = None

        self.editor_window = EditorWindow(pixmap, tray_app=self)
        self.editor_window.show()

        # Optional: run OCR automatically right after capture
        try:
            if bool(self.settings.value(SETTINGS_OCR_AUTO_RUN, False, type=bool)):
                QTimer.singleShot(0, self.editor_window.run_ocr)
        except Exception:
            pass

    def _overlay_destroyed(self, obj=None):
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Overlay destroyed")
        if self.snipping_overlay is obj:
            self.snipping_overlay = None
        elif self.snipping_overlay and obj is None:
             # Fallback pokud obj není předán, ale víme že se ničí
             pass
    
    def on_tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:  # Levý klik
            self.show_settings("general")

    def show_settings(self, tab_name: str = "general"):
        self.settings_window.refresh_ui()
        self.settings_window.show_tab(tab_name)
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    def focus_editor(self) -> bool:
        if not self.editor_window:
            return False
        try:
            self.editor_window.show()
            self.editor_window.raise_()
            self.editor_window.activateWindow()
            return True
        except Exception:
            return False

    def quit(self):
        if self.ocr_assistant_flow is not None:
            try:
                self.ocr_assistant_flow.shutdown()
            except Exception:
                pass
            self.ocr_assistant_flow = None
        if self.editor_window:
            try:
                self.editor_window.close()
            except Exception:
                pass
            self.editor_window = None
        if self.current_hotkey:
            try:
                keyboard.remove_hotkey(self.current_hotkey)
            except:
                pass
        if hasattr(self, 'local_server'):
            self.local_server.close()
        if hasattr(self, 'shared_memory'):
            self.shared_memory.detach()
        self.tray_icon.hide()
        QApplication.quit()

    def run(self):
        sys.exit(self.app.exec())


def main():
    app = TrayApp()
    return app.run()


if __name__ == "__main__":
    main()
