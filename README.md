# TextOnScreen

**TxtOnScrn** is a specialized desktop application I designed for efficient text extraction and processing from the screen. This tool instantly converts visual content into editable text using OCR and allows for subsequent analysis using artificial intelligence.

I must admit that the vast majority of the work on this project was performed by **Github Copilot**.

## Purpose and Usage
I created this as a formal and, if I may say so, **"quite useful"** solution for users who need to quickly digitize text from images, presentations, or error messages and work with it further. The application runs in the background in the System Tray and is ready for use at any time via a global keyboard shortcut.

## Key Features
*   **Advanced Screen Capture:** Multi-monitor support with an intelligent overlay for area selection (snipping tool).
*   **OCR (Optical Character Recognition):** EasyOCR-based text recognition (supports Czech and English). Note: on first use EasyOCR downloads model files.
*   **Text Editor:** Built-in interface for editing extracted text with a preview of the original snapshot.
*   **Security:** API key encryption using Windows DPAPI.
*   **Customization:** Support for dark/light mode (including system settings) and configurable hotkeys.

## Known Limitations and Planned Development
Although the core of the application is functional, please note the current status of AI features:
*   **Public AI Assistant:** **Groq works** (OpenAI-compatible endpoint).
*   **Local Assistant:** Works **offline** for text operations (summaries, bullets, cleanup, stats, sort/dedupe, keywords, outline, simple replace). It is not connected to local model servers (Ollama, LM Studio, etc.) yet, although the options are visible in settings.
