import sys
import os
import winreg
import tempfile
import traceback
import base64
import ctypes
import json
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

# Prevent Qt from applying High DPI scaling
# This ensures we get physical pixel coordinates (1920x1080) for all monitors
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "0"
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
os.environ["QT_SCREEN_SCALE_FACTORS"] = "1"
os.environ["QT_SCALE_FACTOR"] = "1"

# Try importing Tesseract (Pytesseract)
try:
    import pytesseract
    from PIL import Image
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

 

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
from PySide6.QtCore import Qt, QSettings, QSharedMemory, QRect, QPoint, QObject, Signal, QEvent, QUrl
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPainter, QColor, QPen, QCursor, QPixmap, QPalette, QDesktopServices
from PySide6.QtNetwork import QLocalServer, QLocalSocket


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

SETTINGS_AI_PUBLIC_GEMINI_ENABLED = "ai_provider_public_gemini_enabled"
SETTINGS_AI_PUBLIC_HUGGINGFACE_ENABLED = "ai_provider_public_huggingface_enabled"
SETTINGS_AI_PUBLIC_GROQ_ENABLED = "ai_provider_public_groq_enabled"
# (záměrně jen 3 položky v Public)

SETTINGS_AI_PUBLIC_GEMINI_API_KEY = "ai_public_gemini_api_key"
SETTINGS_AI_PUBLIC_HUGGINGFACE_API_KEY = "ai_public_huggingface_api_key"
SETTINGS_AI_PUBLIC_GROQ_API_KEY = "ai_public_groq_api_key"

# Backward-compat (older versions)
SETTINGS_AI_GEMINI_API_KEY = "ai_gemini_api_key"

# Backward-compat keys (older versions)
SETTINGS_AI_GEMINI_ENABLED = "ai_provider_gemini_enabled"
SETTINGS_AI_HUGGINGFACE_ENABLED = "ai_provider_huggingface_enabled"
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
    """Vrátí 'light' nebo 'dark' podle nastavení Windows (AppsUseLightTheme)."""
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
    """Aplikuje paletu pro 'light'/'dark'. Pro 'system' použije Windows režim."""
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


def setup_tesseract():
    """Konfiguruje cestu k Tesseract OCR binárce."""
    if not HAS_TESSERACT:
        return False
        
    # 1. Zkusíme najít Tesseract přibalený v aplikaci (pro EXE verzi)
    # Předpokládáme, že složka "Tesseract-OCR" je přibalena v rootu
    internal_tess = resource_path(os.path.join("Tesseract-OCR", "tesseract.exe"))
    if os.path.exists(internal_tess):
        pytesseract.pytesseract.tesseract_cmd = internal_tess
        return True
        
    # 2. Pokud není přibalený (běžíme ve VS Code), zkusíme vedle skriptu
    local_tess = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Tesseract-OCR", "tesseract.exe")
    if os.path.exists(local_tess):
        pytesseract.pytesseract.tesseract_cmd = local_tess
        return True

    # 3. Fallback: Zkusíme systémové cesty
    possible_paths = [
        r'C:\Program Files\Tesseract-OCR\tesseract.exe',
        r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe'
    ]
    for path in possible_paths:
        if os.path.exists(path):
            pytesseract.pytesseract.tesseract_cmd = path
            return True
            
    return False

# Inicializace tesseractu při startu
setup_tesseract()


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
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Nastavuji startup: {enabled} -> {command}")
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
        print(f"Chyba registru: {e}")


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

        layout = QVBoxLayout(self)
        layout.addWidget(self.startup_checkbox)
        layout.addLayout(hotkey_row)
        layout.addStretch()

        self.refresh()

    def refresh(self) -> None:
        is_enabled = is_startup_enabled()
        self.startup_checkbox.blockSignals(True)
        self.startup_checkbox.setChecked(is_enabled)
        self.startup_checkbox.blockSignals(False)

        hotkey = self.settings.value(SETTINGS_HOTKEY, DEFAULT_HOTKEY)
        self.hotkey_label.setText(hotkey)

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
        self.ai_ollama_cb = QCheckBox("Ollama (lokální server)")
        self.ai_lmstudio_cb = QCheckBox("LM Studio (lokální server)")
        self.ai_localai_cb = QCheckBox("LocalAI (OpenAI-compatible, lokálně)")

        self.ai_ollama_cb.toggled.connect(lambda checked: self.on_local_provider_toggled(self.ai_ollama_cb, SETTINGS_AI_OLLAMA_ENABLED, checked))
        self.ai_lmstudio_cb.toggled.connect(lambda checked: self.on_local_provider_toggled(self.ai_lmstudio_cb, SETTINGS_AI_LMSTUDIO_ENABLED, checked))
        self.ai_localai_cb.toggled.connect(lambda checked: self.on_local_provider_toggled(self.ai_localai_cb, SETTINGS_AI_LOCALAI_ENABLED, checked))

        local_layout.addWidget(self.ai_ollama_cb)
        local_layout.addWidget(self.ai_lmstudio_cb)
        local_layout.addWidget(self.ai_localai_cb)

        public_group = QGroupBox("Public")
        public_layout = QVBoxLayout(public_group)
        self.ai_pub_gemini_cb = QCheckBox("Google Gemini")
        self.ai_pub_hf_cb = QCheckBox("Hugging Face")
        self.ai_pub_groq_cb = QCheckBox("Groq")

        self.ai_pub_gemini_cb.toggled.connect(lambda checked: self.on_public_provider_toggled(self.ai_pub_gemini_cb, SETTINGS_AI_PUBLIC_GEMINI_ENABLED, checked))
        self.ai_pub_hf_cb.toggled.connect(lambda checked: self.on_public_provider_toggled(self.ai_pub_hf_cb, SETTINGS_AI_PUBLIC_HUGGINGFACE_ENABLED, checked))
        self.ai_pub_groq_cb.toggled.connect(lambda checked: self.on_public_provider_toggled(self.ai_pub_groq_cb, SETTINGS_AI_PUBLIC_GROQ_ENABLED, checked))

        public_layout.addWidget(self.ai_pub_gemini_cb)
        public_layout.addWidget(self.ai_pub_hf_cb)
        public_layout.addWidget(self.ai_pub_groq_cb)

        public_key_row = QHBoxLayout()
        self.public_api_key_label = QLabel("API key:")
        public_key_row.addWidget(self.public_api_key_label)
        self.public_api_key_edit = QLineEdit()
        self.public_api_key_edit.setEchoMode(QLineEdit.Password)
        self.public_api_key_edit.setPlaceholderText("Vyber providera a vlož API key…")
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
        self.ai_login_btn = QPushButton("Přihlásit účet")
        self.ai_login_btn.clicked.connect(self.on_ai_login_clicked)
        public_buttons.addWidget(self.ai_login_btn)

        self.ai_get_key_btn = QPushButton("Získat API klíč")
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
        if self.settings.value(SETTINGS_AI_PUBLIC_GEMINI_ENABLED, None) is None:
            self.settings.setValue(SETTINGS_AI_PUBLIC_GEMINI_ENABLED, bool(self.settings.value(SETTINGS_AI_GEMINI_ENABLED, False, type=bool)))
        if self.settings.value(SETTINGS_AI_PUBLIC_HUGGINGFACE_ENABLED, None) is None:
            self.settings.setValue(SETTINGS_AI_PUBLIC_HUGGINGFACE_ENABLED, bool(self.settings.value(SETTINGS_AI_HUGGINGFACE_ENABLED, False, type=bool)))
        if self.settings.value(SETTINGS_AI_PUBLIC_GROQ_ENABLED, None) is None:
            self.settings.setValue(SETTINGS_AI_PUBLIC_GROQ_ENABLED, bool(self.settings.value(SETTINGS_AI_GROQ_ENABLED, False, type=bool)))

        if hasattr(self, "ai_pub_gemini_cb"):
            self.ai_pub_gemini_cb.blockSignals(True)
            self.ai_pub_gemini_cb.setChecked(bool(self.settings.value(SETTINGS_AI_PUBLIC_GEMINI_ENABLED, False, type=bool)))
            self.ai_pub_gemini_cb.blockSignals(False)
        if hasattr(self, "ai_pub_hf_cb"):
            self.ai_pub_hf_cb.blockSignals(True)
            self.ai_pub_hf_cb.setChecked(bool(self.settings.value(SETTINGS_AI_PUBLIC_HUGGINGFACE_ENABLED, False, type=bool)))
            self.ai_pub_hf_cb.blockSignals(False)
        if hasattr(self, "ai_pub_groq_cb"):
            self.ai_pub_groq_cb.blockSignals(True)
            self.ai_pub_groq_cb.setChecked(bool(self.settings.value(SETTINGS_AI_PUBLIC_GROQ_ENABLED, False, type=bool)))
            self.ai_pub_groq_cb.blockSignals(False)

        # Migrate old Gemini key (plain) -> provider-specific key storage
        legacy_gemini = str(self.settings.value(SETTINGS_AI_GEMINI_API_KEY, "") or "")
        if legacy_gemini and not str(self.settings.value(SETTINGS_AI_PUBLIC_GEMINI_API_KEY, "") or ""):
            self.settings.setValue(SETTINGS_AI_PUBLIC_GEMINI_API_KEY, _dpapi_encrypt_to_b64(legacy_gemini))

        self._refresh_public_key_ui()

    def on_ai_login_clicked(self) -> None:
        provider = self._get_selected_public_provider()
        if not provider:
            QMessageBox.information(self, "Přihlášení", "Vyber providera v sekci Public.")
            return

        url = {
            "gemini": "https://accounts.google.com/",
            "huggingface": "https://huggingface.co/login",
            "groq": "https://console.groq.com/",
        }[provider]
        QDesktopServices.openUrl(QUrl(url))

    def on_ai_get_key_clicked(self) -> None:
        provider = self._get_selected_public_provider()
        if not provider:
            QMessageBox.information(self, "API key", "Vyber providera v sekci Public.")
            return

        url = {
            "gemini": "https://aistudio.google.com/app/apikey",
            "huggingface": "https://huggingface.co/settings/tokens",
            "groq": "https://console.groq.com/keys",
        }[provider]
        QDesktopServices.openUrl(QUrl(url))

    def on_ai_test_key_clicked(self) -> None:
        provider = self._get_selected_public_provider()
        if not provider:
            QMessageBox.information(self, "Test key", "Vyber providera v sekci Public.")
            return

        api_key = (self.public_api_key_edit.text() or "").strip()
        if not api_key:
            QMessageBox.information(self, "Test key", "Nejdřív vlož API key.")
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
            "gemini": SETTINGS_AI_PUBLIC_GEMINI_API_KEY,
            "huggingface": SETTINGS_AI_PUBLIC_HUGGINGFACE_API_KEY,
            "groq": SETTINGS_AI_PUBLIC_GROQ_API_KEY,
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
                (self.ai_pub_gemini_cb, SETTINGS_AI_PUBLIC_GEMINI_ENABLED),
                (self.ai_pub_hf_cb, SETTINGS_AI_PUBLIC_HUGGINGFACE_ENABLED),
                (self.ai_pub_groq_cb, SETTINGS_AI_PUBLIC_GROQ_ENABLED),
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
        if self.ai_pub_gemini_cb.isChecked():
            return "gemini"
        if self.ai_pub_hf_cb.isChecked():
            return "huggingface"
        if self.ai_pub_groq_cb.isChecked():
            return "groq"
        return None

    def _refresh_public_key_ui(self) -> None:
        provider = self._get_selected_public_provider()
        enabled = provider is not None

        # Provider-aware label + placeholder
        if provider == "gemini":
            self.public_api_key_label.setText("Gemini API key:")
            setting_key = SETTINGS_AI_PUBLIC_GEMINI_API_KEY
        elif provider == "huggingface":
            self.public_api_key_label.setText("Hugging Face token:")
            setting_key = SETTINGS_AI_PUBLIC_HUGGINGFACE_API_KEY
        elif provider == "groq":
            self.public_api_key_label.setText("Groq API key:")
            setting_key = SETTINGS_AI_PUBLIC_GROQ_API_KEY
        else:
            self.public_api_key_label.setText("API key:")
            setting_key = None

        self.public_api_key_edit.setEnabled(enabled)
        self.public_key_show_btn.setEnabled(enabled)
        self.public_key_clear_btn.setEnabled(enabled)
        self.ai_login_btn.setEnabled(enabled)
        self.ai_get_key_btn.setEnabled(enabled)
        self.ai_test_key_btn.setEnabled(enabled)

        if not enabled:
            self.public_api_key_edit.blockSignals(True)
            self.public_api_key_edit.setText("")
            self.public_api_key_edit.blockSignals(False)
            self.public_status_label.setText("Vyber providera v sekci Public.")
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
            if provider == "gemini":
                status, body = self._http_json(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
                    headers={"Accept": "application/json"},
                )
                if status == 200 and isinstance(body, dict) and body.get("models"):
                    return True, "Gemini key is valid"
                return False, f"HTTP {status}"

            if provider == "huggingface":
                status, body = self._http_json(
                    "https://huggingface.co/api/whoami-v2",
                    headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
                )
                if status == 200 and isinstance(body, dict) and body.get("name"):
                    return True, f"Logged in as {body.get('name')}"
                return False, f"HTTP {status}"

            if provider == "groq":
                status, body = self._http_json(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
                )
                if status == 200:
                    return True, "Groq key is valid"
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
    def __init__(self, mode: str, get_editor_text, parent=None):
        super().__init__(parent)
        self.mode = (mode or "local").lower()
        self.get_editor_text = get_editor_text

        title = "Local Assistant" if self.mode == "local" else "Public Assistant"
        self.setWindowTitle(title)
        self.setWindowIcon(QIcon(resource_path("ico.ico")))
        self.resize(640, 420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Zadej úkol/otázku (může být i mimo text)."))

        self.task_edit = QTextEdit()
        self.task_edit.setPlaceholderText("Např.: Shrň text do 5 bodů. / Oprav překlepy. / Vysvětli, co znamená... ")
        layout.addWidget(self.task_edit, 1)

        layout.addWidget(QLabel("Výstup:"))
        self.output_edit = QTextEdit()
        self.output_edit.setReadOnly(True)
        layout.addWidget(self.output_edit, 2)

        buttons = QHBoxLayout()
        buttons.addStretch()
        run_btn = QPushButton("Run")
        run_btn.clicked.connect(self.on_run)
        buttons.addWidget(run_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

    def on_run(self):
        task = (self.task_edit.toPlainText() or "").strip()
        editor_text = ""
        try:
            editor_text = (self.get_editor_text() or "")
        except Exception:
            editor_text = ""

        if not task:
            self.output_edit.setPlainText("Zadej prosím úkol/otázku.")
            return

        if self.mode == "public":
            self._run_public(task=task, editor_text=editor_text)
            return

        # Local Assistant: jednoduché offline operace s textem (bez externích API)
        text = (editor_text or "").strip()
        if not text:
            self.output_edit.setPlainText(
                "Editor text je prázdný.\n\n"
                f"Úkol: {task}\n"
                "Tip: Nejprve vlož text do editoru, pak zkus třeba: 'Shrň do 5 bodů'."
            )
            return

        task_l = task.lower()
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
        if "shrň" in task_l or "shrnout" in task_l or "summary" in task_l or "summarize" in task_l:
            take = min(5, len(lines))
            summary = "\n".join(f"- {lines[i][:200]}" for i in range(take))
            self.output_edit.setPlainText(summary if summary else "(Není co shrnout.)")
            return

        if "odráž" in task_l or "body" in task_l or "bullet" in task_l:
            take = min(10, len(lines))
            bullets = "\n".join(f"- {lines[i][:200]}" for i in range(take))
            self.output_edit.setPlainText(bullets if bullets else "(Není co vypsat.)")
            return

        if "vyčisti" in task_l or "očisti" in task_l or "cleanup" in task_l:
            cleaned = "\n".join(ln.strip() for ln in text.splitlines())
            self.output_edit.setPlainText(cleaned)
            return

        self.output_edit.setPlainText(
            "Local Assistant umí zatím jen jednoduché offline operace.\n"
            "Zkus třeba: 'Shrň', 'Udělej odrážky', 'Vyčisti'.\n\n"
            f"Úkol: {task}"
        )

    def _run_public(self, task: str, editor_text: str) -> None:
        settings = QSettings(ORG_NAME, APP_NAME)

        provider = self._get_selected_public_provider(settings)
        if provider != "gemini":
            self.output_edit.setPlainText(
                "Public Assistant je teď napojený pouze na Gemini.\n"
                "Vyber v Settings → AI → Public: Google Gemini."
            )
            return

        encrypted = str(settings.value(SETTINGS_AI_PUBLIC_GEMINI_API_KEY, "") or "")
        api_key = _dpapi_decrypt_from_b64(encrypted).strip()
        if not api_key:
            self.output_edit.setPlainText(
                "Chybí Gemini API key.\n"
                "Jdi do Settings → AI → Public, vyber Google Gemini a vlož API key."
            )
            return

        prompt = self._build_public_prompt(task=task, editor_text=editor_text)

        self.setCursor(Qt.WaitCursor)
        self.task_edit.setEnabled(False)
        QApplication.processEvents()
        try:
            answer = self._gemini_generate(api_key=api_key, prompt=prompt)
            self.output_edit.setPlainText(answer)
        except Exception as e:
            self.output_edit.setPlainText(f"Public AI error: {e}")
        finally:
            self.task_edit.setEnabled(True)
            self.setCursor(Qt.ArrowCursor)

    def _get_selected_public_provider(self, settings: QSettings):
        if bool(settings.value(SETTINGS_AI_PUBLIC_GEMINI_ENABLED, False, type=bool)):
            return "gemini"
        if bool(settings.value(SETTINGS_AI_PUBLIC_HUGGINGFACE_ENABLED, False, type=bool)):
            return "huggingface"
        if bool(settings.value(SETTINGS_AI_PUBLIC_GROQ_ENABLED, False, type=bool)):
            return "groq"
        return None

    def _build_public_prompt(self, task: str, editor_text: str) -> str:
        text = (editor_text or "").strip()
        if text:
            return (
                "Jsi asistent. Uživatel ti dává úkol a také text z editoru. "
                "Když úkol souvisí s textem, pracuj s ním.\n\n"
                f"Úkol: {task}\n\n"
                "Text z editoru:\n"
                f"{text}"
            )
        return f"Úkol: {task}"

    def _gemini_generate(self, api_key: str, prompt: str) -> str:
        # Model choice: prefer a fast generally-available model name.
        model = "gemini-1.5-flash"
        url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={api_key}"
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ]
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {raw}")

        body = json.loads(raw)
        candidates = body.get("candidates") or []
        if not candidates:
            raise RuntimeError("No candidates in response")

        content = (candidates[0].get("content") or {})
        parts = content.get("parts") or []
        text_parts = [p.get("text", "") for p in parts if isinstance(p, dict)]
        answer = "".join(text_parts).strip()
        if not answer:
            raise RuntimeError("Empty response")
        return answer


class EditorWindow(QWidget):
    def __init__(self, pixmap: QPixmap, tray_app=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Editor")
        self.setWindowIcon(QIcon(resource_path("ico.ico")))
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
        
        # Copy
        copy_action = QAction("Copy", self)
        copy_action.setShortcut(QKeySequence.Copy)
        copy_action.triggered.connect(self.copy_to_clipboard)
        file_menu.addAction(copy_action)
        
        file_menu.addSeparator()
        
        # Exit
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Tools Menu
        tools_menu = menu_bar.addMenu("Tools")

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

        local_assistant_action = QAction("Local Assistant...", self)
        local_assistant_action.triggered.connect(self.open_local_assistant)
        ai_menu.addAction(local_assistant_action)

        public_assistant_action = QAction("Public Assistant...", self)
        public_assistant_action.triggered.connect(self.open_public_assistant)
        ai_menu.addAction(public_assistant_action)
        
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

        self._local_assistant_dialog = None
        self._public_assistant_dialog = None

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

    def open_local_assistant(self):
        if not self._local_assistant_dialog:
            self._local_assistant_dialog = AssistantDialog(
                mode="local",
                get_editor_text=self._get_editor_text_for_assistant,
                parent=self,
            )
        self._local_assistant_dialog.show()
        self._local_assistant_dialog.raise_()
        self._local_assistant_dialog.activateWindow()

    def open_public_assistant(self):
        if not self._public_assistant_dialog:
            self._public_assistant_dialog = AssistantDialog(
                mode="public",
                get_editor_text=self._get_editor_text_for_assistant,
                parent=self,
            )
        self._public_assistant_dialog.show()
        self._public_assistant_dialog.raise_()
        self._public_assistant_dialog.activateWindow()

    def run_ocr(self):
        # Re-check imports locally to handle runtime installation
        global HAS_TESSERACT
        
        try:
            from PIL import Image, ImageEnhance, ImageFilter
            import pytesseract
            
            # If we are here, imports work. Update global state if needed.
            if not HAS_TESSERACT:
                HAS_TESSERACT = True
                setup_tesseract()
        except ImportError:
            HAS_TESSERACT = False

        if not HAS_TESSERACT:
            QMessageBox.warning(
                self, 
                "OCR Libraries Missing", 
                "Python library 'pytesseract' or 'pillow' is missing.\nPlease run: pip install pytesseract pillow"
            )
            return

        # Ověření, zda máme funkční cestu k EXE (pokud selže version check, zkusíme znovu setup)
        try:
            pytesseract.get_tesseract_version()
        except:
            if not setup_tesseract():
                 QMessageBox.warning(
                    self,
                    "Tesseract Engine Missing",
                    "Could not find Tesseract-OCR executable.\nEnsure the 'Tesseract-OCR' folder is in the application directory."
                )
                 return

        self.setCursor(Qt.WaitCursor)
        QApplication.processEvents()

        temp_path = None
        try:
            # Save current image to temp file for OCR
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
                temp_path = f.name
            
            self.original_pixmap.save(temp_path)
            
            # Check if file was saved correctly
            if os.path.getsize(temp_path) == 0:
                self.text_edit.append("Error: Saved image file is empty.")
                return

            # --- TESSERACT OCR logic ---
            # Pre-processing pro lepší přesnost
            img = Image.open(temp_path)
            
            # 1. Zvětšení obrázku (Upscaling)
            # Tesseract očekává cca 300 DPI, ale obrazovka má obvykle 96 DPI.
            # Zvětšení 3x výrazně zpřesní rozpoznávání malých písmen a diakritiky.
            new_size = tuple(3 * x for x in img.size)
            # Použijeme kvalitní resampling (LANCZOS)
            resample_method = Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS
            img = img.resize(new_size, resample_method)
            
            # 2. Převod na odstíny šedi (Grayscale)
            img = img.convert('L')
            
            # 3. Zvýšení kontrastu a ostrosti
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance(2.0) # Zvýšíme kontrast 2x
            
            enhancer = ImageEnhance.Sharpness(img)
            img = enhancer.enhance(2.0) # Doostříme hrany
            
            # 4. Volitelně: Binarizace (černá/bílá) - často pomůže zbavit se šumu pozadí
            # img = img.point(lambda x: 0 if x < 140 else 255, '1')

            # lang='ces+eng' (čeština + angličtina)
            # --psm 6 (Assume a single uniform block of text) - ideální pro screenshoty
            custom_config = r'--psm 6'
            
            text = pytesseract.image_to_string(img, lang='ces+eng', config=custom_config)
            
            if text and text.strip():
                self.text_edit.append(text.strip())
            else:
                self.text_edit.append("No text detected.")

        except Exception as e:
            # Print full error for debugging
            error_msg = f"OCR Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}"
            self.text_edit.append(error_msg)
            print(error_msg) # Also print to console
            
        finally:
            self.setCursor(Qt.ArrowCursor)
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except:
                    pass

    def copy_to_clipboard(self):
        clipboard = QApplication.clipboard()
        clipboard.setPixmap(self.original_pixmap)

        
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
        
        # Zachytit celou obrazovku (všechny monitory)
        screens = QApplication.screens()
        
        # Zjistit celkovou geometrii všech monitorů
        total_rect = QRect()
        for screen in screens:
            total_rect = total_rect.united(screen.geometry())
        self.total_rect = total_rect
            
        # Zjistit největší pixel ratio (scale factor)
        self.max_dpr = max(s.devicePixelRatio() for s in screens) if screens else 1.0
        
        # Store screen captures and their layout info
        self.captures = []
        
        # Sort screens by logical X position to determine physical layout order
        # This helps in reconstructing a continuous physical image
        sorted_screens = sorted(screens, key=lambda s: (s.geometry().y(), s.geometry().x()))
        
        current_phys_x = 0
        max_phys_y = 0

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Detekce monitorů:")
        for i, screen in enumerate(sorted_screens):
            geo = screen.geometry()
            dpr = screen.devicePixelRatio()
            print(f"  Monitor {i}: Logická geom={geo.getRect()}, DPR={dpr}")
        
        for screen in sorted_screens:
            # Grab raw physical pixels
            pix = screen.grabWindow(0)
            
            # Nastavíme DPR pro pixmapu, aby odpovídalo monitoru
            # Tím zajistíme, že logická velikost pixmapy odpovídá logické geometrii okna
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
            
            # Pro posun fyzického offsetu používáme fyzickou šířku (width * DPR if using logical, but pix.width() is raw)
            # pix.width() vrací logickou šířku pokud je nastaveno DPR!
            # Musíme získat raw velikost
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
        # Místo klasického showFullScreen(), který často bere jen primární monitor,
        # manuálně nastavíme geometrii přes všechny monitory.
        self.setGeometry(self.total_rect)
        self.show()
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Overlay zobrazen s geometrií: {self.geometry()}")
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

        # Apply appearance theme early
        apply_theme_mode(self.settings.value(SETTINGS_THEME_MODE, "system"))

        # Signál pro komunikaci mezi threadem klávesnice a GUI threadem
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
        # Callback z keyboard knihovny běží v jiném vlákně
        # Musíme emitovat signál pro GUI vlákno
        self.signal_handler.hotkey_pressed.emit()

    def on_hotkey_main_thread(self):
        # Toggle overlay: pokud běží, zavřít; jinak otevřít
        
        # Kontrola stávající instance
        if self.snipping_overlay:
            try:
                # Zkusíme zjistit, zda je okno viditelné
                # Pokud bylo C++ objektem smazáno, vyhodí toto RuntimeError
                if self.snipping_overlay.isVisible():
                    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Overlay zavřen")
                    self.snipping_overlay.close()
                    return
            except RuntimeError:
                # Objekt byl smazán, ale reference zůstala. Vyčistíme.
                self.snipping_overlay = None

        # Vytvořit a zobrazit nový overlay pro výběr oblasti
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Overlay zapnut")
        self.snipping_overlay = SnippingOverlay()
        # Vyčistit referenci po zavření
        try:
            self.snipping_overlay.destroyed.connect(self._overlay_destroyed)
            self.snipping_overlay.capture_taken.connect(self.open_editor)
        except Exception:
            pass
        self.snipping_overlay.show_fullscreen_custom()
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Overlay zobrazen")

    def open_editor(self, pixmap: QPixmap):
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Otevírám editor")
        # Pokud už editor existuje, zavřeme ho nebo aktualizujeme
        if self.editor_window:
            self.editor_window.close()
            
        self.editor_window = EditorWindow(pixmap, tray_app=self)
        self.editor_window.show()

    def _overlay_destroyed(self, obj=None):
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Overlay zničen")
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
