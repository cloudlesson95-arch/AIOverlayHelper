"""Fullscreen translucent overlay for drag-to-select region capture.

Workflow:

1. Caller creates a :class:`RegionSelector` and connects to ``selected``
   and ``cancelled``.
2. ``show_selector()`` covers the primary screen with a dim overlay.
3. User drags a rectangle; release emits ``selected(QRect)`` with
   screen-coordinate geometry. Esc or a zero-size drag emits ``cancelled``.

DPI: the emitted rect is in **physical pixels** so the consumer can hand
it straight to ``mss`` / native screenshot APIs. Qt's own mouse coords
are in logical (DPI-scaled) pixels, so we multiply by
:meth:`devicePixelRatioF` before emitting. At 100% scaling this is a
no-op; at 125% it converts a 400×300 logical drag into a 500×375
physical region, which is what the user actually selected on screen.

Multi-monitor: covers the primary screen only for now. Extending to the
virtual desktop is on the future-enhancements list in Plan.md.
"""
from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QKeyEvent, QMouseEvent, QPainter
from PyQt6.QtWidgets import QLabel, QRubberBand, QWidget

from src.logger import get_logger

log = get_logger(__name__)


class RegionSelector(QWidget):
    """One-shot region picker. Self-deletes after emitting a signal."""

    selected = pyqtSignal(QRect)   # global screen coordinates
    cancelled = pyqtSignal()

    # Small delay after hiding ourselves so Qt actually repaints the screen
    # before the caller's screenshot grab — otherwise mss would capture our
    # dim overlay on top of the user's content.
    _HIDE_GRACE_MS = 80

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._origin = QPoint()
        self._dragging = False
        self._rubber = QRubberBand(QRubberBand.Shape.Rectangle, self)

        # Floating hint label centered at top
        self._hint = QLabel(
            "Drag to select region   ·   Esc to cancel",
            self,
        )
        self._hint.setStyleSheet(
            "QLabel { "
            "color: white; "
            "background-color: rgba(20,20,24,210); "
            "padding: 6px 14px; "
            "border-radius: 6px; "
            "font-size: 13px; "
            "}"
        )
        self._hint.adjustSize()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def show_selector(self) -> None:
        screen = QGuiApplication.primaryScreen().geometry()
        self.setGeometry(screen)
        # Re-center hint after geometry is known
        self._hint.move((screen.width() - self._hint.width()) // 2, 24)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        log.debug("RegionSelector shown over %dx%d", screen.width(), screen.height())

    # ------------------------------------------------------------------ #
    # Painting (dim the screen so the selection is visible)
    # ------------------------------------------------------------------ #

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 90))

    # ------------------------------------------------------------------ #
    # Mouse / keyboard
    # ------------------------------------------------------------------ #

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.pos()
            self._rubber.setGeometry(QRect(self._origin, QSize()))
            self._rubber.show()
            self._dragging = True

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging:
            self._rubber.setGeometry(QRect(self._origin, event.pos()).normalized())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if not self._dragging:
            return
        self._dragging = False
        rect = self._rubber.geometry().normalized()
        self._rubber.hide()
        self.hide()

        if rect.width() < 4 or rect.height() < 4:
            log.debug("Region too small (%dx%d) — cancelling.",
                      rect.width(), rect.height())
            QTimer.singleShot(0, self._emit_cancelled)
            return

        # Qt mouse coords are logical pixels; mss / native capture APIs
        # use physical pixels. Convert via the screen's device pixel ratio
        # so a drag at 125% scaling captures the actual region drawn.
        top_left = self.mapToGlobal(rect.topLeft())
        dpr = self.devicePixelRatioF()
        global_rect = QRect(
            round(top_left.x() * dpr),
            round(top_left.y() * dpr),
            round(rect.width() * dpr),
            round(rect.height() * dpr),
        )
        log.debug("Region selected (DPR=%.2f): %dx%d at (%d,%d) physical",
                  dpr, global_rect.width(), global_rect.height(),
                  global_rect.x(), global_rect.y())
        QTimer.singleShot(self._HIDE_GRACE_MS,
                          lambda r=global_rect: self._emit_selected(r))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            log.debug("RegionSelector cancelled via Esc.")
            self.hide()
            QTimer.singleShot(0, self._emit_cancelled)

    # ------------------------------------------------------------------ #
    # Emit helpers — close self after the signal so the caller sees the
    # final geometry before we're gone.
    # ------------------------------------------------------------------ #

    def _emit_selected(self, rect: QRect) -> None:
        self.selected.emit(rect)
        self.close()

    def _emit_cancelled(self) -> None:
        self.cancelled.emit()
        self.close()
