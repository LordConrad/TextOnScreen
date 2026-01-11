# TextOnScreen

**TxtOnScrn** is a specialized desktop application I designed for efficient text extraction and processing from the screen. This tool instantly converts visual content into editable text using OCR and allows for subsequent analysis using artificial intelligence.

I must admit that the vast majority of the work on this project was performed by **Github Copilot**.

## Purpose and Usage

I created this as a formal and, if I may say so, **"quite useful"** solution for users who need to quickly digitize text from images, presentations, or error messages and work with it further. The application runs in the background in the System Tray and is ready for use at any time via a global keyboard shortcut.

> Note: the app is currently Windows-focused (uses the Windows registry and DPAPI).

## Quick Start (dev)

- Create and activate a venv.
- Install dependencies (at minimum):
  - `pip install PySide6 keyboard`
  - `pip install easyocr pillow numpy` (EasyOCR pulls in Torch; first OCR can take a while)
- Run: `python main.py`

## Key Features

- **Advanced Screen Capture:** Multi-monitor support with an intelligent overlay for area selection (snipping tool).
- **OCR (Optical Character Recognition):** EasyOCR-based text recognition (supports Czech and English). Models are stored persistently on disk (per-user) so they survive app restarts.
- **Low-RAM Tray Process:** OCR runs in a separate long-lived subprocess (“OCR server”), so Torch/EasyOCR memory does not stay in the main tray GUI process.
- **Editor & Assistant workflows:**
  - Classic: capture → Editor (image + text) → run OCR → copy/edit.
  - Fast: capture → OCR → AI assistant (skip editor) where OCR text is auto-fed as input context.
- **Security:** API key encryption using Windows DPAPI.
- **Customization:** Support for dark/light mode (including system settings) and configurable hotkeys.

## OCR Settings

In Settings → General → OCR:

- **Fast mode (greedy decoder)**: faster, sometimes less accurate.
- **Use in-memory image**: avoids temp file for OCR input.
- **Image upscale (1–4)** + **Automatic**: 1 = no upscale (fastest), higher = better for small fonts but slower.
- **Auto-run OCR after capture**
- **After capture: open AI assistant (skip editor)**
- **Clear OCR model cache**: deletes stored EasyOCR models (they will be downloaded again on next OCR run).
- **Uninstall / remove all app data**: disables startup, clears settings (registry), deletes app data (LocalAppData) and legacy OCR cache from `%TEMP%`, then exits.

## Build (PyInstaller)

Use [build.bat](build.bat). Output is an **onedir** build:

- `dist\TxtOnScrn\TxtOnScrn.exe`
- Copy/run the entire `dist\TxtOnScrn` folder (do not move only the EXE).

## Known Limitations and Planned Development

Although the core of the application is functional, please note the current status of AI features:

- **Public AI Assistant:** **Groq works** (OpenAI-compatible endpoint).
- **Local Assistant:** Works **offline** for text operations (summaries, bullets, cleanup, stats, sort/dedupe, keywords, outline, simple replace). It is not connected to local model servers (Ollama, LM Studio, etc.) yet, although the options are visible in settings.
