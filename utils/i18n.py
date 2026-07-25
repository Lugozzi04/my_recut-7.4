from __future__ import annotations


SUPPORTED_LANGUAGES = ("en", "it")

_STRINGS = {
    "en": {
        "add_video": "Add Video",
        "main": "Main",
        "export": "Export",
        "export_mp4": "Export MP4",
        "export_edl": "Export EDL",
        "process_ai": "Process AI",
        "reprocess_ai": "Reprocess AI",
        "show_logs": "Show logs",
        "copy_logs": "Copy logs",
        "open_logs": "Open logs folder",
        "quick_start": "Quick start...",
        "diagnostics": "Create diagnostics bundle...",
        "about": "About Auto Cutter...",
        "third_party": "Third-party notices...",
    },
    "it": {
        "add_video": "Aggiungi video",
        "main": "Montaggio",
        "export": "Esporta",
        "export_mp4": "Esporta MP4",
        "export_edl": "Esporta EDL",
        "process_ai": "Analizza con AI",
        "reprocess_ai": "Rianalizza con AI",
        "show_logs": "Mostra log",
        "copy_logs": "Copia log",
        "open_logs": "Apri cartella log",
        "quick_start": "Guida rapida...",
        "diagnostics": "Crea pacchetto diagnostico...",
        "about": "Informazioni su Auto Cutter...",
        "third_party": "Licenze di terze parti...",
    },
}


def normalize_language(value: str | None) -> str:
    language = str(value or "").strip().lower().replace("_", "-").split("-", 1)[0]
    return language if language in SUPPORTED_LANGUAGES else "en"


def text(key: str, language: str = "en") -> str:
    locale = normalize_language(language)
    return _STRINGS.get(locale, _STRINGS["en"]).get(key, _STRINGS["en"].get(key, key))
