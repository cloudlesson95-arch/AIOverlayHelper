"""Entry point: boots Qt, registers global hotkeys, wires everything up."""
from __future__ import annotations
import sys
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from dotenv import load_dotenv

from src.ai_client import AIClient
from src.config import load_settings, save_settings
from src.conversations import load_conversations
from src.hotkeys import HotkeyManager, qt_to_pynput
from src.logger import configure_logging, get_logger
from src.memory import MemoryStore
from src.templates import load_templates
from src.ui.overlay import OverlayWindow
from src.ui.settings_window import SettingsWindow
from src.ui.tray import TrayIcon

load_dotenv()  # loads .env from the current working directory

log = get_logger(__name__)


class App(QObject):
    """Glue layer: owns the windows, hotkeys, and current settings.

    Hotkey callbacks fire on pynput's listener thread, so we route them
    through Qt signals before touching any widgets.
    """

    summon_overlay_signal = pyqtSignal()
    open_settings_signal = pyqtSignal()
    # Emitted (with template name) by per-template global hotkeys, routed
    # from pynput's thread back onto the Qt main thread before touching
    # the overlay.
    select_template_signal = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        log.info("[startup] Loading settings…")
        self.settings: dict = load_settings()
        configure_logging(self.settings.get("logging"))
        log.info("[startup] App starting (provider=%s model=%s)",
                 self.settings["provider"], self.settings["model"])

        self.ai_client = AIClient(self.settings["provider"], self.settings["model"])
        self.templates = load_templates(self.settings)
        self.memory = MemoryStore(self.settings.get("memory"))

        self.overlay = OverlayWindow(
            self.ai_client,
            self.templates,
            initial_conversations=load_conversations(),
            memory=self.memory,
            hotkeys=self.settings.get("hotkeys"),
            framed_window=bool(self.settings.get("framed_window", False)),
            window_size=(
                int(self.settings.get("window_width") or 540),
                int(self.settings.get("window_height") or 460),
            ),
            chat_style=self.settings.get("chat_style"),
        )
        self.overlay.template_saved_signal.connect(self._on_template_saved)
        self.settings_window: Optional[SettingsWindow] = None

        self.hotkeys = HotkeyManager()
        # Summon hotkey doubles as Hide — pressing it when the overlay
        # is already visible hides it. There used to be a separate
        # hide_window hotkey for this; merged so the same keystroke
        # gives a clean show/hide toggle from anywhere.
        self.summon_overlay_signal.connect(self.toggle_overlay)
        self.open_settings_signal.connect(self.show_settings)
        self.select_template_signal.connect(self.select_template_by_name)
        self._register_hotkeys()

        self.tray = TrayIcon(
            on_show_overlay=self.show_overlay,
            on_show_settings=self.show_settings,
            on_quit=self.quit_app,
            on_toggle_overlay=self.toggle_overlay,
        )
        self.tray.show()
        log.info("[startup] App ready (tray icon active).")

    # ------------------------------------------------------------------ #
    # Hotkeys
    # ------------------------------------------------------------------ #

    def _register_hotkeys(self) -> None:
        """Wire the *global* hotkeys: summon overlay, open settings, plus
        any per-template global hotkeys configured in Settings → Templates.

        Settings stores Qt format; pynput needs its own format, so we
        translate at the boundary. The local overlay shortcuts
        (send_prompt, next_template, prev_template) are handled by
        the overlay itself and don't pass through here — they only
        fire while the overlay window has focus.
        """
        hk = self.settings.get("hotkeys", {})
        bindings: dict[str, object] = {}
        try:
            bindings[qt_to_pynput(hk.get("summon_overlay") or "Ctrl+Alt+Space")] = (
                self.summon_overlay_signal.emit
            )
            bindings[qt_to_pynput(hk.get("open_settings") or "Ctrl+Alt+,")] = (
                self.open_settings_signal.emit
            )
            # Per-template hotkeys. Empty / missing strings are skipped.
            # Later templates with the same combo silently override earlier
            # ones at registration time — Settings' duplicate-detection
            # dialog is supposed to prevent that from happening.
            for t in self.settings.get("templates", []):
                combo = str(t.get("hotkey") or "").strip()
                name = str(t.get("name") or "").strip()
                if not combo or not name:
                    continue
                key = qt_to_pynput(combo)
                if not key:
                    continue
                bindings[key] = (
                    lambda n=name: self.select_template_signal.emit(n)
                )
            self.hotkeys.register(bindings)
        except Exception as exc:  # noqa: BLE001 — surface bad combos
            log.error("[hotkey] Registration failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Window summoners
    # ------------------------------------------------------------------ #

    def quit_app(self) -> None:
        log.info("[tray] Quit requested — shutting down.")
        self.hotkeys.stop()
        self.tray.hide()
        QApplication.quit()

    def show_overlay(self) -> None:
        log.info("[hotkey] Summon overlay")
        self.overlay.show_overlay()

    def toggle_overlay(self) -> None:
        """Show if hidden, hide if visible. Wired to both tray left-click
        and the global Summon-overlay hotkey, so the same affordance
        cleanly dismisses the overlay without a separate Hide hotkey."""
        if self.overlay.isVisible():
            log.info("[hotkey] Hide overlay (toggle)")
            self.overlay.hide()
        else:
            log.info("[hotkey] Show overlay (toggle)")
            self.overlay.show_overlay()

    def select_template_by_name(self, name: str) -> None:
        """Per-template hotkey landed: select that template, summon the overlay."""
        log.info("[hotkey] Select template: %s", name)
        self.overlay.select_template_by_name(name)

    def show_settings(self) -> None:
        log.info("[hotkey] Open settings")
        # Hide the overlay (if it's currently visible) so Settings is the
        # only window in focus. The overlay can be re-summoned at any
        # time via hotkey or tray left-click — closing Settings does NOT
        # auto-bring it back; that's deliberate, otherwise toggling
        # would feel surprising.
        if self.overlay.isVisible():
            log.debug("[settings] Hiding overlay while Settings is open.")
            self.overlay.hide()
        if self.settings_window is None or not self.settings_window.isVisible():
            self.settings_window = SettingsWindow(self.settings, memory=self.memory)
            self.settings_window.settings_saved.connect(self._on_settings_saved)
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    # ------------------------------------------------------------------ #
    # Live reload after a save in Settings
    # ------------------------------------------------------------------ #

    def _on_settings_saved(self, new_settings: dict) -> None:
        log.info("[settings] Reloading runtime…")
        self.settings = new_settings
        configure_logging(new_settings.get("logging"))
        self.ai_client = AIClient(new_settings["provider"], new_settings["model"])
        self.templates = load_templates(new_settings)
        self.memory = MemoryStore(new_settings.get("memory"))
        self.overlay.update_runtime(
            self.ai_client, self.templates, memory=self.memory,
            hotkeys=new_settings.get("hotkeys"),
            framed_window=bool(new_settings.get("framed_window", False)),
            window_size=(
                int(new_settings.get("window_width") or 540),
                int(new_settings.get("window_height") or 460),
            ),
            chat_style=new_settings.get("chat_style"),
        )
        self._register_hotkeys()
        log.info("[settings] Reloaded (provider=%s model=%s).",
                 new_settings["provider"], new_settings["model"])

    def _on_template_saved(self, template_dict: dict) -> None:
        """Persist a template proposed by Analyze image…, then refresh the overlay."""
        name = template_dict.get("name", "(unnamed)")
        log.info("[template] Appending new template '%s' from Analyze flow", name)
        self.settings.setdefault("templates", []).append(template_dict)
        try:
            save_settings(self.settings)
        except Exception:  # noqa: BLE001
            log.exception("Failed to persist new template")
            return
        self.templates = load_templates(self.settings)
        self.overlay.update_runtime(
            self.ai_client, self.templates,
            select_name=name, memory=self.memory,
        )


def _install_pyqt6_tray_workaround() -> None:
    """Silently drop a known PyQt6-on-Windows spurious tray error.

    ``QSystemTrayIcon.activated`` occasionally fires with a C++
    ``ActivationReason`` value PyQt6 can't unmarshal to the Python enum
    — most often under a debugger (VSCode's Run/Debug attaches one) or
    on transient focus events that Windows reports with values outside
    the documented enum range. The conversion fails *before* our slot
    runs, so a local ``try`` in the handler can't catch it; the
    resulting TypeError escapes through Qt's dispatcher and, in recent
    PyQt6 builds, terminates the process.

    We only act on ``Trigger`` (left-click) anyway, so dropping events
    we couldn't decode is functionally identical to ignoring them.
    The hook filters by the exception's text so unrelated TypeErrors
    still bubble up normally.
    """
    _orig_excepthook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        if exc_type is TypeError and "QSystemTrayIcon::ActivationReason" in str(exc_value):
            log.debug("[tray] Suppressed spurious PyQt6 ActivationReason conversion error.")
            return
        _orig_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook

    # PyQt6 routes some slot-dispatch errors through sys.unraisablehook
    # instead of excepthook depending on version/connection type.
    _orig_unraisable = sys.unraisablehook

    def _un_hook(unraisable):
        msg = str(unraisable.exc_value) if unraisable.exc_value else ""
        if "QSystemTrayIcon::ActivationReason" in msg:
            log.debug("[tray] Suppressed spurious PyQt6 ActivationReason (unraisable).")
            return
        _orig_unraisable(unraisable)

    sys.unraisablehook = _un_hook


def main() -> None:
    # Bootstrap logging with defaults so early messages are visible;
    # App.__init__ will reconfigure from settings.yaml as soon as it loads.
    configure_logging(None)
    log.info("[main] AI Overlay Helper starting…")
    _install_pyqt6_tray_workaround()
    qt_app = QApplication(sys.argv)
    # Keep running in the background after the user closes both windows.
    qt_app.setQuitOnLastWindowClosed(False)

    app = App()  # noqa: F841 — kept alive by Qt event loop via signal connections

    hk = app.settings.get("hotkeys", {})
    log.info(
        "[main] Running. Hotkeys: %s (overlay toggle), %s (settings), %s (send).",
        hk.get("summon_overlay"), hk.get("open_settings"),
        hk.get("send_prompt"),
    )
    sys.exit(qt_app.exec())


if __name__ == "__main__":
    main()
