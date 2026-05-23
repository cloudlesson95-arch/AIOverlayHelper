"""System tray icon: persistent affordance while the app runs in the background.

Menu actions are wired to callables passed in by ``App``. The icon is a
Qt standard icon — swap in a custom ``.ico`` later if desired.
"""
from __future__ import annotations
from typing import Callable

from PyQt6.QtWidgets import (
    QApplication,
    QMenu,
    QStyle,
    QSystemTrayIcon,
)

from src.logger import get_logger

log = get_logger(__name__)


class TrayIcon(QSystemTrayIcon):
    """System tray icon with Open / Settings / Quit menu.

    Left-click **toggles** the overlay (show if hidden, hide if visible).
    The menu item still shows the overlay unconditionally — that's the
    discoverable "I just want it open" path.
    """

    def __init__(
        self,
        on_show_overlay: Callable[[], None],
        on_show_settings: Callable[[], None],
        on_quit: Callable[[], None],
        on_toggle_overlay: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        # The icon's left-click activator toggles by default; falls back
        # to plain "show" if the caller didn't supply a toggle callback.
        self._on_toggle_overlay = on_toggle_overlay or on_show_overlay

        app = QApplication.instance()
        if app is not None:
            self.setIcon(app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        self.setToolTip("AI Overlay Helper")

        menu = QMenu()
        menu.addAction("Show overlay", on_show_overlay)
        menu.addAction("Settings…", on_show_settings)
        menu.addSeparator()
        menu.addAction("Quit", on_quit)
        self.setContextMenu(menu)

        self.activated.connect(self._on_activated)

    def _on_activated(self, reason) -> None:
        """Left-click (``Trigger``) toggles the overlay; right-click shows
        the menu via Qt's built-in handling.

        ``reason`` is intentionally un-annotated: PyQt6 on Windows
        sometimes hands us a value its strict enum-converter chokes on
        (see ``_install_pyqt6_tray_workaround`` in main.py). The
        try/except here is a second line of defense in case the value
        *does* reach us but doesn't compare cleanly with the enum
        member — we'd rather silently ignore than crash.
        """
        try:
            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                log.debug("[tray] Left-click → toggle overlay")
                self._on_toggle_overlay()
        except Exception:  # noqa: BLE001 — defensive against PyQt enum quirks
            log.debug("[tray] Ignored unexpected activation value: %r", reason)
