"""Settings loader and saver.

Single source of truth: ``settings/settings.yaml`` at the project root.
API keys are NEVER stored here — they come from environment variables.
"""
from pathlib import Path
import yaml

from src.hotkeys import pynput_to_qt
from src.logger import get_logger

log = get_logger(__name__)

# project_root/src/config.py  →  project_root/settings/settings.yaml
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_DIR = PROJECT_ROOT / "settings"
SETTINGS_PATH = SETTINGS_DIR / "settings.yaml"

DEFAULT_SETTINGS = {
    "provider": "openai",
    "model": "gpt-4o-mini",
    # Canonical hotkey format here is Qt's QKeySequence string form
    # ("Ctrl+Alt+Space"), because that's what the Settings recorder
    # (QKeySequenceEdit) produces. main.py converts these to pynput's
    # format only when registering the *global* hooks; the *local*
    # overlay shortcuts (send_prompt, next_template, prev_template) are
    # pure Qt and use the strings verbatim.
    "hotkeys": {
        # Global. Pressing summon_overlay while the overlay is already
        # visible hides it — there's no separate hide_window hotkey.
        "summon_overlay": "Ctrl+Alt+Space",
        "open_settings": "Ctrl+Alt+,",
        # Local overlay shortcuts (only fire while overlay has focus).
        # Stored in canonical Qt PortableText ("Return") but shown to
        # users as "Enter" in Settings, matching the keycap label.
        "send_prompt": "Ctrl+Return",
        "next_template": "Ctrl+Alt+Right",
        "prev_template": "Ctrl+Alt+Left",
    },
    "logging": {
        "enabled": True,
        "level": "INFO",
    },
    # When True, the overlay opens as a normal OS window (title bar, close/
    # maximize/minimize buttons, resizable, taskbar entry). When False (the
    # default), it's a frameless translucent always-on-top popup — the
    # original overlay look. Toggle in Settings → Additional.
    "framed_window": False,
    # Initial overlay window size. Editable in Settings → Additional.
    # Width/height are applied on next show() and on any settings Save/Apply.
    "window_width": 540,
    "window_height": 460,
    # Chat-rendering style. All values are consumed by overlay._build_markdown_css.
    # font_family empty means "use the QTextEdit default" (system UI font).
    # *_align controls which side each role's bubble hugs in the response area.
    "chat_style": {
        "font_family": "",
        "font_size": 13,
        "turn_spacing": 18,         # vertical gap (px) between turn bubbles
        "user_align": "left",       # "left" | "right"
        "assistant_align": "right",
        "show_labels": True,        # show the "You" / "AI" header on each bubble
        # Per-role background tinting. Mode = "never" / "headers" / "all".
        # Color is a hex string; the renderer applies it at a soft alpha
        # (~0.20 for full bubble, ~0.30 for header strip) so any picked
        # hue stays readable. Defaults: no tint, with sensible accent
        # hues ready for the user to opt in via Settings.
        "user_bg_mode": "never",
        "user_bg_color": "#4a7dff",
        "assistant_bg_mode": "never",
        "assistant_bg_color": "#888888",
    },
    "memory": {
        # Global on/off for the long-term memory feature (ChromaDB-backed
        # RAG). Even when True, only templates with use_memory: true
        # actually save & retrieve.
        "enabled": False,
        # Embedding backend: "gemini" | "openai" | "local"
        "backend": "gemini",
        # Model name for the chosen backend. Defaults below match each
        # backend; if you change `backend` and not `model`, the store
        # picks a sensible default in code.
        "model": "gemini-embedding-001",
        # How many past entries to retrieve per Send.
        "top_k": 3,
        # Local backend only. When True, sets HF_HUB_OFFLINE=1 before
        # sentence-transformers loads, skipping the HuggingFace Hub
        # HEAD request that otherwise checks for a newer model revision
        # on every load. Eliminates the "unauthenticated request" warning
        # and lets the app work fully offline once the model is cached.
        # Change requires app restart (huggingface_hub caches the value
        # at import time).
        "local_offline_mode": False,
    },
    "templates": [
        {"name": "General", "text": "{prompt}", "include_screenshot": False},
    ],
}


def load_settings() -> dict:
    """Load settings from disk, creating the file with defaults if missing."""
    log.debug("Loading settings from %s", SETTINGS_PATH)
    if not SETTINGS_PATH.exists():
        log.info("No settings file found — writing defaults to %s", SETTINGS_PATH)
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        save_settings(DEFAULT_SETTINGS)
        return {k: v for k, v in DEFAULT_SETTINGS.items()}

    with SETTINGS_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    # Backfill any missing top-level keys with defaults
    for key, default in DEFAULT_SETTINGS.items():
        data.setdefault(key, default)

    # Migrate hotkeys: older settings.yaml files stored pynput-format
    # strings ("<ctrl>+<alt>+<space>"). Convert them to Qt format so the
    # Settings recorder, save path, and every consumer see one format.
    # Also backfill any expected keys that are missing.
    hk = dict(data.get("hotkeys") or {})
    for key, default_qt in DEFAULT_SETTINGS["hotkeys"].items():
        raw = hk.get(key)
        if not raw:
            hk[key] = default_qt
        else:
            hk[key] = pynput_to_qt(raw)
    # Drop retired hotkey keys so they can't keep haunting the in-memory
    # dict after we stopped reading them. Kept as a tiny migration list
    # — when a key is removed from DEFAULT_SETTINGS["hotkeys"], add it
    # here so old settings.yaml files don't leave behind orphan entries.
    for retired in ("hide_window",):
        hk.pop(retired, None)
    data["hotkeys"] = hk

    log.debug("Settings loaded: provider=%s model=%s templates=%d",
              data.get("provider"), data.get("model"), len(data.get("templates", [])))
    return data


def save_settings(settings: dict) -> None:
    """Persist settings to disk in human-readable YAML."""
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    log.debug("Saving settings to %s", SETTINGS_PATH)
    with SETTINGS_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(settings, f, sort_keys=False, allow_unicode=True)
    log.info("Settings saved.")
