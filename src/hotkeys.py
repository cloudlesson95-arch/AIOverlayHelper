"""Global hotkey manager built on :mod:`pynput`, plus Qt↔pynput conversion.

pynput runs its own listener thread, so callbacks fire OUTSIDE the Qt main
thread. In ``main.py`` we route them through ``pyqtSignal`` to bounce back
onto the Qt thread before touching any widgets.

Hotkey format note:

* **Canonical storage format** (settings.yaml, in-memory) is Qt's
  ``QKeySequence`` string form — ``"Ctrl+Alt+Space"``, ``"Esc"``,
  ``"Ctrl+Return"`` — because that's what :class:`QKeySequenceEdit`
  produces in the Settings recorder.
* **pynput**'s :class:`GlobalHotKeys` uses ``"<ctrl>+<alt>+<space>"``
  with angle-bracketed modifier/key tokens. :func:`qt_to_pynput` is
  the translation layer; it's applied only at the moment we register
  global hooks (see ``main.App._register_hotkeys``).
* :func:`pynput_to_qt` exists for back-compat — older settings files
  written before this refactor stored pynput format directly, so
  :mod:`src.config` normalizes those on load.
"""
from __future__ import annotations
import re
from typing import Callable, Optional

from pynput import keyboard

from src.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------- #
# Qt ↔ pynput hotkey-string conversion
# ---------------------------------------------------------------------- #

# Qt modifier name (lowercased) → pynput angle-bracket form.
_QT_MOD_TO_PYNPUT = {
    "ctrl": "<ctrl>",
    "control": "<ctrl>",
    "alt": "<alt>",
    "shift": "<shift>",
    "meta": "<cmd>",
    "win": "<cmd>",
    "cmd": "<cmd>",
}

# Qt named non-letter keys (lowercased) → pynput form.
_QT_KEY_TO_PYNPUT = {
    "space": "<space>",
    "return": "<enter>",
    "enter": "<enter>",
    "tab": "<tab>",
    "esc": "<esc>",
    "escape": "<esc>",
    "backspace": "<backspace>",
    "del": "<delete>",
    "delete": "<delete>",
    "ins": "<insert>",
    "insert": "<insert>",
    "home": "<home>",
    "end": "<end>",
    "pgup": "<page_up>",
    "pageup": "<page_up>",
    "pgdown": "<page_down>",
    "pagedown": "<page_down>",
    "pgdn": "<page_down>",
    "up": "<up>",
    "down": "<down>",
    "left": "<left>",
    "right": "<right>",
    # Qt sometimes serializes punctuation as words; map back to chars.
    "comma": ",",
    "period": ".",
    "minus": "-",
    "plus": "+",
    "equal": "=",
    "slash": "/",
    "backslash": "\\",
    "semicolon": ";",
    "apostrophe": "'",
    "bracketleft": "[",
    "bracketright": "]",
}


def qt_to_pynput(qt_combo: str) -> str:
    """Translate a Qt ``QKeySequence`` string to pynput's format.

    Examples::

        Ctrl+Alt+Space   → <ctrl>+<alt>+<space>
        Ctrl+Alt+,       → <ctrl>+<alt>+,
        F1               → <f1>
        Esc              → <esc>

    Strings that already look pynput-shaped (contain ``<``) pass through
    unchanged so callers don't have to track which side they're on.
    """
    if not qt_combo:
        return qt_combo
    if "<" in qt_combo:
        return qt_combo
    out: list[str] = []
    for raw in (p.strip() for p in qt_combo.split("+")):
        if not raw:
            continue
        token = raw.lower()
        if token in _QT_MOD_TO_PYNPUT:
            out.append(_QT_MOD_TO_PYNPUT[token])
        elif token in _QT_KEY_TO_PYNPUT:
            out.append(_QT_KEY_TO_PYNPUT[token])
        elif re.fullmatch(r"f[0-9]{1,2}", token):
            out.append(f"<{token}>")
        elif len(token) == 1:
            out.append(token)
        else:
            out.append(f"<{token}>")
    return "+".join(out)


# Reverse maps for migration from pre-refactor pynput-format settings.
_PYNPUT_MOD_TO_QT = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Meta"}
_PYNPUT_KEY_TO_QT = {
    "space": "Space", "enter": "Return", "tab": "Tab", "esc": "Esc",
    "backspace": "Backspace", "delete": "Del", "insert": "Ins",
    "home": "Home", "end": "End", "page_up": "PgUp", "page_down": "PgDown",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
}


def prettify_combo(combo: str) -> str:
    """Convert a Qt-PortableText hotkey string to its user-facing form.

    QKeySequenceEdit serializes the main Enter key as ``Return`` (its
    canonical Qt key name), but every keyboard label says **Enter** — so
    we substitute at the display boundary. Storage stays canonical, the
    QShortcut/pynput layers parse the stored value cleanly, and only the
    UI labels swap in the prettier name.

    Currently this is the only mapping; add more here if other Qt
    names ever feel surprising to users (rare).
    """
    if not combo:
        return ""
    return "+".join(
        "Enter" if p.strip() == "Return" else p
        for p in combo.split("+")
    )


def pynput_to_qt(combo: str) -> str:
    """Translate pynput-format hotkey back to Qt ``QKeySequence`` form.

    Used at settings-load time to migrate the old on-disk format. Strings
    that contain no ``<`` brackets pass through unchanged (already Qt).
    """
    if not combo or "<" not in combo:
        return combo
    out: list[str] = []
    for raw in (p.strip() for p in combo.split("+")):
        m = re.fullmatch(r"<(.+)>", raw)
        if m:
            inner = m.group(1).lower()
            if inner in _PYNPUT_MOD_TO_QT:
                out.append(_PYNPUT_MOD_TO_QT[inner])
            elif inner in _PYNPUT_KEY_TO_QT:
                out.append(_PYNPUT_KEY_TO_QT[inner])
            elif re.fullmatch(r"f[0-9]{1,2}", inner):
                out.append(inner.upper())
            else:
                out.append(inner.capitalize())
        elif len(raw) == 1:
            out.append(raw.upper() if raw.isalpha() else raw)
        else:
            out.append(raw)
    return "+".join(out)


class HotkeyManager:
    """Wraps ``pynput.keyboard.GlobalHotKeys`` with safe restart semantics."""

    def __init__(self) -> None:
        self._listener: Optional[keyboard.GlobalHotKeys] = None

    def register(self, bindings: dict[str, Callable[[], None]]) -> None:
        """Register hotkey combos, replacing any existing bindings.

        ``bindings`` maps a pynput-style combo string like
        ``"<ctrl>+<alt>+<space>"`` to a zero-arg callable.
        """
        log.debug("Registering hotkeys: %s", list(bindings.keys()))
        self.stop()
        self._listener = keyboard.GlobalHotKeys(bindings)
        self._listener.start()
        log.info("Hotkeys active: %s", ", ".join(bindings.keys()))

    def stop(self) -> None:
        """Stop and *join* the previous listener.

        ``Listener.stop()`` on its own only flags the listener to exit —
        the listener's OS-level keyboard hook may still be alive (and
        firing its callbacks) until the listener thread actually drains
        and unhooks. Without the join, calling ``register(...)`` in
        quick succession can leave the previous global hotkeys active
        alongside the new ones, which is what manifested as
        "Settings → Apply, but old hotkeys still react."
        """
        listener = self._listener
        if listener is None:
            return
        log.debug("Stopping previous hotkey listener.")
        self._listener = None
        try:
            listener.stop()
        except Exception:  # noqa: BLE001 — never let teardown abort the swap
            log.exception("Hotkey listener stop raised")
        # Listener subclasses threading.Thread. Joining with a small
        # bound makes sure the OS hook is released before we replace it.
        try:
            listener.join(timeout=2.0)
        except Exception:  # noqa: BLE001
            log.exception("Hotkey listener join raised")
