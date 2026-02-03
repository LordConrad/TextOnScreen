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
  - `pip install pillow numpy`
  - OCR engine (pick one):
    - **Windows OCR (fast, recommended on Windows):** `pip install winsdk`
    - **PaddleOCR (ML models):** `pip install paddlepaddle paddleocr`
  - Offline translation (optional, open-source):
    - `pip install argostranslate`
- Run: `python main.py`

## Key Features

- **Advanced Screen Capture:** Multi-monitor support with an intelligent overlay for area selection (snipping tool).
- **OCR (Optical Character Recognition):** Selectable engine:
  - **Windows OCR (WinRT):** very fast, no model downloads.
  - **PaddleOCR:** ML-based recognition; models cached on disk and survive app restarts.
- **Low-RAM Tray Process:** OCR runs in a separate long-lived subprocess (“OCR server”), so OCR engine memory does not stay in the main tray GUI process.
- **Editor & Assistant workflows:**
  - Classic: capture → Editor (image + text) → run OCR → copy/edit.
  - Fast: capture → OCR → AI assistant (skip editor) where OCR text is auto-fed as input context.
- **Assistant Output UX (new):** optional Markdown output mode (rendered in-app), with visible loading/progress while generating.
- **Security:** API key encryption using Windows DPAPI.
- **Customization:** Support for dark/light mode (including system settings) and configurable hotkeys.

## Translation Overlay (offline-capable)

There is an additional hotkey-driven workflow:

- **Settings → Translation**
  - **Source** language (or Auto)
  - **Target** language
  - **Engine**:
    - **Offline (Argos Translate, open-source)** (default)
    - **Online (Public AI provider)** (fallback)

When using Offline (Argos Translate):

- The first use of a new language pair may download a model package once (if enabled in settings).
- Models are stored under `%LOCALAPPDATA%\TxtOnScrn\ArgosTranslate\...`.

## OCR Settings

In Settings → General → OCR:

- **Engine**: Windows OCR (fast) or PaddleOCR (models).
- **Fast mode (greedy decoder)**: faster, sometimes less accurate.
- **Use in-memory image**: avoids temp file for OCR input.
- **Image upscale (1–4)** + **Automatic**: 1 = no upscale (fastest), higher = better for small fonts but slower.
- **Auto-run OCR after capture**
- **After capture: open AI assistant (skip editor)**
- **Clear PaddleOCR model cache**: deletes stored PaddleOCR models (they will be downloaded again on next OCR run).
- **Uninstall / remove all app data**: disables startup, clears settings (registry), deletes app data (LocalAppData) and legacy OCR cache from `%TEMP%`, then exits.

## Build (PyInstaller)

Use [build.bat](build.bat).

- Default is **onefile** (single EXE): `dist\TxtOnScrn.exe`
- **onedir** build (folder): `dist\TxtOnScrn\TxtOnScrn.exe` (copy/run the whole folder, do not move only the EXE)

### Release packaging

You can also create a ready-to-copy `release\` output (plus ZIP):

- `build.bat release` (default onefile + `release\TxtOnScrn.exe` + ZIP)
- `build.bat onedir release` (copies full folder + ZIP)

## Known Limitations and Planned Development

Although the core of the application is functional, please note the current status of AI features:

- **Public AI Assistant:** **Groq works** (OpenAI-compatible endpoint).
- **Public AI Assistant:** **OpenAI works** via API key (it does not use “Sign in with ChatGPT/OpenAI account”; user must generate an API key in OpenAI Platform).
- **Public AI Assistant:** **Gemini works** via Google AI Studio API key.
- **Local Assistant:** Works **offline** for text operations (summaries, bullets, cleanup, stats, sort/dedupe, keywords, outline, simple replace). It is not connected to local model servers (Ollama, LM Studio, etc.) yet, although the options are visible in settings.

## AI Assistant (updated)

The Assistant dialog now supports two output modes:

- **Plain text (default):** output is shown as text.
- **Markdown mode (optional):** output is rendered as Markdown directly in the Assistant output area.

Enable it here:

- **Settings → AI → Assistant → “Answer in Markdown (.md) and open as output”**

When Markdown mode is enabled:

- Output is rendered in-app (Markdown preview).
- Output is also written to a persistent file:
  - `%LOCALAPPDATA%\TxtOnScrn\assistant_output.md`
- The Assistant UI shows a **loading/progress indicator** (“Generating…”) while a Public provider call is running.

### Assistant menu bar & shortcuts

The Assistant window uses an Editor-like menu bar with grouped actions:

- **Input** → Copy OCR / Copy editor text (`Ctrl+Shift+I`)
- **Output** → Copy output (`Ctrl+Shift+C`)
- **Output** → Open `.md` (`Ctrl+Shift+O`) *(enabled only in Markdown mode after the file exists)*
- **Output** → Save output as… (`Ctrl+Shift+S`) *(enabled only in Markdown mode)*
- **Assistant** → Settings… (`Ctrl+,`)
- **Assistant** → Help (`F1`)
- **Assistant** → Run (`Ctrl+Enter`)
- **Assistant** → Close (`Ctrl+W`)
