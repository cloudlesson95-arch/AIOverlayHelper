"""Persistent conversation history (text-only).

Stored at ``settings/conversations.json`` next to the user settings.
Keyed by template name, value is the ordered list of turns.

Schema per turn::

    {
      "role": "user" | "assistant",
      "content": "<text>",
      "had_image": true | false
    }

**Image bytes are NOT persisted.** A turn that had a screenshot keeps
``had_image: true`` so the overlay can still show the "with screenshot"
marker after a restart, but the actual pixels are dropped. If the user
continues a thread after restart and the model needs to "see" the
original image, the user should re-attach a screenshot — older turns
in that thread will go to the API as text-only.

Why text-only: keeps the JSON small (no base64 blobs), keeps file I/O
fast (synchronous saves on every change), and matches the user's stated
preference for memory storage. Image-on-disk persistence is a small
follow-up if it's needed later.
"""
from __future__ import annotations
import json
from pathlib import Path

from src.ai_client import Message
from src.config import SETTINGS_DIR
from src.logger import get_logger

log = get_logger(__name__)

CONVERSATIONS_PATH: Path = SETTINGS_DIR / "conversations.json"


def load_conversations() -> dict[str, list[Message]]:
    """Read conversations from disk. Returns ``{}`` if the file is missing
    or unreadable — we never fail the app over a bad history file.
    """
    if not CONVERSATIONS_PATH.exists():
        log.debug("No conversations.json found; starting with empty history.")
        return {}
    try:
        with CONVERSATIONS_PATH.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to read conversations.json (%s); starting empty.", exc)
        return {}

    if not isinstance(raw, dict):
        log.error("conversations.json root is %s, expected dict; starting empty.",
                  type(raw).__name__)
        return {}

    result: dict[str, list[Message]] = {}
    total_turns = 0
    for template_name, turns in raw.items():
        if not isinstance(turns, list):
            continue
        msgs: list[Message] = []
        for t in turns:
            if not isinstance(t, dict):
                continue
            role = t.get("role")
            content = t.get("content", "")
            if role not in ("user", "assistant"):
                continue
            msgs.append(Message(
                role=role,
                content=str(content),
                image=None,  # never persisted
                had_image=bool(t.get("had_image", False)),
            ))
        result[template_name] = msgs
        total_turns += len(msgs)
    log.info("Loaded conversation history: %d template(s), %d turn(s) total.",
             len(result), total_turns)
    return result


def save_conversations(conversations: dict[str, list[Message]]) -> None:
    """Atomically persist conversations to disk.

    Writes to a temp file then renames so a crash mid-write can't leave
    the JSON half-empty.
    """
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    raw: dict[str, list[dict]] = {}
    for template_name, turns in conversations.items():
        raw[template_name] = [
            {
                "role": m.role,
                "content": m.content,
                "had_image": bool(m.had_image),
            }
            for m in turns
        ]

    tmp = CONVERSATIONS_PATH.with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2, ensure_ascii=False)
        tmp.replace(CONVERSATIONS_PATH)
        log.debug("Saved conversations.json (%d template(s)).", len(raw))
    except OSError as exc:
        log.error("Failed to save conversations.json: %s", exc)
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
