"""Background workers for AI calls.

Two flavours:

* :class:`AICallWorker` + :func:`run_ai_call` — streams chunks for an
  ``AIClient.stream()`` call. Three signals: ``chunk(str)``, ``finished()``,
  ``failed(str)``.
* :class:`BackgroundWorker` + :func:`run_in_background` — generic one-shot:
  runs an arbitrary zero-arg callable on a QThread. Two signals:
  ``finished(object)`` carrying the return value, ``failed(str)``. Used
  for structured-extraction calls and anything else where the result is
  a single Python object rather than a stream.
"""
from __future__ import annotations
from typing import Any, Callable

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from src.ai_client import AIClient, Message
from src.logger import get_logger

log = get_logger(__name__)


class AICallWorker(QObject):
    """Streams an AI call. Lives on a QThread; signals back to main thread."""

    chunk = pyqtSignal(str)
    finished = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, ai_client: AIClient, messages: list[Message]) -> None:
        super().__init__()
        self.ai_client = ai_client
        self.messages = messages

    def run(self) -> None:
        log.debug("Worker.run on thread=%s", QThread.currentThread())
        chunks = 0
        try:
            for piece in self.ai_client.stream(self.messages):
                self.chunk.emit(piece)
                chunks += 1
            log.debug("Stream complete (%d chunks).", chunks)
            self.finished.emit()
        except Exception as exc:  # noqa: BLE001 — surface all errors to UI
            log.exception("AI stream failed inside worker (after %d chunks)", chunks)
            self.failed.emit(str(exc))


def run_ai_call(
    parent: QObject,
    ai_client: AIClient,
    messages: list[Message],
    on_chunk,
    on_finished,
    on_failed,
) -> tuple[QThread, AICallWorker]:
    """Spawn a QThread + AICallWorker for a streaming conversation.

    Returns the (thread, worker) pair so the caller can hold a reference
    until the thread finishes — without that, Python may GC them mid-run.
    Both auto-clean via ``deleteLater`` once the thread quits.
    """
    thread = QThread(parent)
    worker = AICallWorker(ai_client, messages)
    worker.moveToThread(thread)

    thread.started.connect(worker.run)
    worker.chunk.connect(on_chunk)
    worker.finished.connect(on_finished)
    worker.failed.connect(on_failed)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)

    thread.start()
    log.debug("AI stream dispatched to background thread.")
    return thread, worker


# ---------------------------------------------------------------------- #
# Generic one-shot worker
# ---------------------------------------------------------------------- #

class BackgroundWorker(QObject):
    """Runs an arbitrary zero-arg callable on a QThread.

    Used for things like structured extraction where the result is a
    single object rather than a stream of chunks.
    """

    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 — surface all errors to caller
            log.exception("BackgroundWorker callable raised")
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


def run_in_background(
    parent: QObject,
    fn: Callable[[], Any],
    on_finished: Callable[[Any], None],
    on_failed: Callable[[str], None],
) -> tuple[QThread, BackgroundWorker]:
    """Spawn a QThread + BackgroundWorker, wire signals, start it."""
    thread = QThread(parent)
    worker = BackgroundWorker(fn)
    worker.moveToThread(thread)

    thread.started.connect(worker.run)
    worker.finished.connect(on_finished)
    worker.failed.connect(on_failed)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)

    thread.start()
    log.debug("Background task dispatched to QThread.")
    return thread, worker
