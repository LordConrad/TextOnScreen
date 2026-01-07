# TxtOnScrn

**TxtOnScrn** je specializovaná desktopová aplikace navržená pro efektivní extrakci a zpracování textu z obrazovky. Nástroj slouží k okamžitému převodu vizuálního obsahu na editovatelný text pomocí OCR a jeho následné analýze pomocí umělé inteligence.

Projekt byl vyvinut v úzké spolupráci s **Google Copilot**.

## Účel a využití
Jedná se o formální a, slovy autora, **"quite useful"** řešení pro uživatele, kteří potřebují rychle digitalizovat text z obrázků, prezentací nebo chybových hlášek a dále s ním pracovat. Aplikace běží na pozadí v systémové liště (System Tray) a je kdykoliv připravena k použití pomocí globální klávesové zkratky.

## Klíčové funkce
*   **Pokročilé snímání obrazovky:** Podpora více monitorů s inteligentním overlayem pro výběr oblasti (snipping tool).
*   **OCR (Optické rozpoznávání znaků):** Integrovaný engine Tesseract pro přesný převod obrázků na text (podpora češtiny a angličtiny).
*   **AI Asistent (Public):** Plná integrace cloudu (Google Gemini) pro sumarizaci textu, opravy chyb nebo generování odpovědí přímo v editoru.
*   **Textový Editor:** Vestavěné rozhraní pro úpravu extrahovaného textu s náhledem původního snímku.
*   **Zabezpečení:** Šifrování API klíčů pomocí Windows DPAPI.
*   **Přizpůsobení:** Podpora tmavého/světlého režimu (včetně systémového nastavení) a konfigurovatelné klávesové zkratky.

## Známá omezení a plánovaný vývoj
Ačkoliv je jádro aplikace plně funkční, následující funkce jsou momentálně ve fázi implementace:
*   **Lokální AI integrace:** V nastavení jsou již připraveny volby pro lokální LLM (Ollama, LM Studio, LocalAI), avšak "Local Assistant" zatím provádí pouze základní hardwarově nenáročné operace (např. jednoduché formátování) a není prozatím napojen na API lokálních serverů.
