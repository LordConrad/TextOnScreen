# TxtOnScrn

**TxtOnScrn** is a specialized desktop application designed for efficient text extraction and processing from the screen. This tool instantly converts visual content into editable text using OCR and allows for subsequent analysis using artificial intelligence.

The vast majority of the work on this project was performed by **Google Copilot**.

## Purpose and Usage
This is a formal and, in the words of the author, a **"quite useful"** solution for users who need to quickly digitize text from images, presentations, or error messages and work with it further. The application runs in the background in the System Tray and is ready for use at any time via a global keyboard shortcut.

## Key Features
*   **Advanced Screen Capture:** Multi-monitor support with an intelligent overlay for area selection (snipping tool).
*   **OCR (Optical Character Recognition):** Integrated Tesseract engine for accurate conversion of images to text (supports Czech and English).
*   **AI Assistant (Public):** Full cloud integration (Google Gemini) for text summarization, error correction, or generating responses directly within the editor.
*   **Text Editor:** Built-in interface for editing extracted text with a preview of the original snapshot.
*   **Security:** API key encryption using Windows DPAPI.
*   **Customization:** Support for dark/light mode (including system settings) and configurable hotkeys.

## Known Limitations and Planned Development
Although the core of the application is fully functional, the following features are currently in the implementation phase:
*   **Local AI Integration:** Options for local LLMs (Ollama, LM Studio, LocalAI) are already prepared in the settings, but the "Local Assistant" currently performs only basic hardware-light operations (e.g., simple formatting) and is not yet connected to the APIs of local servers.
