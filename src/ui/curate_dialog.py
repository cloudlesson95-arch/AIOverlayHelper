"""Curate dialog — review / edit / delete turns in a conversation thread.

Opens from the overlay's **Curate thread…** button. Shows the current
template's full conversation as a checkable list. Actions:

* **Delete selected** — drops the checked turns from the working copy.
* **Edit selected** (exactly one checked) — opens a multi-line editor
  for that turn's text content.
* **Summarize → Memory…** — sends the checked turns to the AI for
  distillation, lets the user edit the result, then saves it as a
  ``kind: summary`` entry in the ChromaDB-backed long-term memory store
  for this template. Requires long-term memory to be enabled in
  Settings *and* chromadb to be installed (the dialog probes the store
  at open time and grays the button out with a reason tooltip when
  unavailable).
* **Save changes** — commits the working copy back into the overlay's
  per-template history. The overlay handles persistence to disk.

Image bytes are not editable here — only text. The ``had_image`` marker
is preserved through edits, so a reloaded "with screenshot" turn keeps
its marker after editing.
"""
from __future__ import annotations
from typing import Optional, TYPE_CHECKING

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.ai_client import Message
from src.logger import get_logger
from src.memory import MemoryUnavailable
from src.worker import run_in_background

if TYPE_CHECKING:
    from src.ai_client import AIClient
    from src.memory import MemoryStore

log = get_logger(__name__)

# Truncation length for preview text (in chars).
_PREVIEW_LEN = 300


# ---------------------------------------------------------------------- #
# Per-turn row widget
# ---------------------------------------------------------------------- #

class _TurnRow(QFrame):
    """Visual row representing one turn: checkbox + role label + content preview."""

    def __init__(self, msg: Message) -> None:
        super().__init__()
        self.msg = msg
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Raised)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(4)

        # Header: checkbox + role + char-count
        header = QHBoxLayout()
        self.checkbox = QCheckBox()
        header.addWidget(self.checkbox)

        role_text = "You" if msg.role == "user" else "AI"
        if msg.role == "user" and msg.had_image:
            role_text += " · with screenshot"
        role_label = QLabel(
            f"<b>{role_text}</b>  "
            f"<span style='opacity:0.5;'>({len(msg.content)} chars)</span>"
        )
        role_label.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(role_label, 1)
        layout.addLayout(header)

        # Content preview
        preview_text = msg.content or "(empty)"
        if len(preview_text) > _PREVIEW_LEN:
            preview_text = preview_text[:_PREVIEW_LEN].rstrip() + "…"
        preview = QLabel(preview_text)
        preview.setWordWrap(True)
        preview.setContentsMargins(24, 0, 0, 0)
        preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(preview)

    def is_checked(self) -> bool:
        return self.checkbox.isChecked()

    def set_checked(self, on: bool) -> None:
        self.checkbox.setChecked(on)


# ---------------------------------------------------------------------- #
# The dialog
# ---------------------------------------------------------------------- #

class CurateDialog(QDialog):
    """Modal for managing the current template's conversation thread."""

    # Emitted after a successful Summarize → Memory save. The overlay
    # listens so it can refresh its usage strip (the summarization call
    # contributes tokens just like a normal Send).
    summary_saved = pyqtSignal()

    def __init__(self, template_name: str, turns: list[Message],
                 ai_client: Optional["AIClient"] = None,
                 memory: Optional["MemoryStore"] = None,
                 parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Curate thread — {template_name}")
        self.setMinimumSize(620, 520)
        self.setModal(True)

        self._template_name = template_name
        self._ai_client = ai_client
        self._memory = memory
        # In-flight summarization worker refs (kept alive while running).
        # Only the AI-summarization phase is awaited here — the memory
        # save is detached from this dialog's lifecycle (daemon thread,
        # see _begin_summary_save) so curate can close immediately on
        # OK even when the first local-model load takes ~1s.
        self._summ_thread = None
        self._summ_worker = None

        # Working copy — only committed back to caller via get_turns() on save.
        self._turns: list[Message] = list(turns)
        self._rows: list[_TurnRow] = []
        self._empty_label: Optional[QLabel] = None

        self._build_ui()
        self._rebuild_rows()
        self._refresh_summarize_button()
        log.debug("CurateDialog opened (template=%s, turns=%d, memory=%s)",
                  template_name, len(self._turns),
                  memory is not None and memory.enabled)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        header = QLabel(
            "Check the turns you want to act on, then choose Delete or Edit. "
            "Click Save changes to commit, or Cancel to discard."
        )
        header.setWordWrap(True)
        root.addWidget(header)

        # Scrollable area for rows
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self._row_container = QWidget()
        self._row_layout = QVBoxLayout(self._row_container)
        self._row_layout.setContentsMargins(0, 0, 0, 0)
        self._row_layout.setSpacing(8)
        self._row_layout.addStretch()  # rows insert before this stretch
        self.scroll.setWidget(self._row_container)
        root.addWidget(self.scroll, 1)

        # Selection helpers
        sel = QHBoxLayout()
        sel_all = QPushButton("Select all")
        sel_all.clicked.connect(self._select_all)
        sel.addWidget(sel_all)
        sel_none = QPushButton("Deselect all")
        sel_none.clicked.connect(self._deselect_all)
        sel.addWidget(sel_none)
        sel.addStretch()
        root.addLayout(sel)

        # Action buttons
        actions = QHBoxLayout()
        self._delete_btn = QPushButton("Delete selected")
        self._delete_btn.clicked.connect(self._delete_selected)
        actions.addWidget(self._delete_btn)
        self._edit_btn = QPushButton("Edit selected")
        self._edit_btn.setToolTip("Open a multi-line editor for the single checked turn.")
        self._edit_btn.clicked.connect(self._edit_selected)
        actions.addWidget(self._edit_btn)
        self._summarize_btn = QPushButton("Summarize → Memory…")
        self._summarize_btn.clicked.connect(self._summarize_to_memory)
        actions.addWidget(self._summarize_btn)
        actions.addStretch()
        root.addLayout(actions)

        # Save / Cancel
        bottom = QHBoxLayout()
        bottom.addStretch()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.reject)
        self._save_btn = QPushButton("Save changes")
        self._save_btn.setDefault(True)
        self._save_btn.clicked.connect(self._on_save_clicked)
        bottom.addWidget(self._cancel_btn)
        bottom.addWidget(self._save_btn)
        root.addLayout(bottom)

    # ------------------------------------------------------------------ #
    # Rebuild rows from self._turns
    # ------------------------------------------------------------------ #

    def _rebuild_rows(self) -> None:
        for row in self._rows:
            self._row_layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()
        if self._empty_label is not None:
            self._row_layout.removeWidget(self._empty_label)
            self._empty_label.deleteLater()
            self._empty_label = None

        if self._turns:
            for msg in self._turns:
                row = _TurnRow(msg)
                self._rows.append(row)
                # Insert before the trailing stretch so rows stack at top.
                self._row_layout.insertWidget(self._row_layout.count() - 1, row)
        else:
            empty = QLabel("This thread is empty.")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet("opacity: 0.5; padding: 24px;")
            self._row_layout.insertWidget(self._row_layout.count() - 1, empty)
            self._empty_label = empty

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    def _select_all(self) -> None:
        for row in self._rows:
            row.set_checked(True)

    def _deselect_all(self) -> None:
        for row in self._rows:
            row.set_checked(False)

    def _delete_selected(self) -> None:
        keep = [m for m, row in zip(self._turns, self._rows) if not row.is_checked()]
        removed = len(self._turns) - len(keep)
        if removed == 0:
            QMessageBox.information(
                self, "Nothing selected",
                "Check the turns you want to delete first.",
            )
            return
        self._turns = keep
        log.info("[curate] Marked %d turn(s) for deletion (%d remain)",
                 removed, len(keep))
        self._rebuild_rows()

    def _edit_selected(self) -> None:
        checked_idx = [i for i, row in enumerate(self._rows) if row.is_checked()]
        if len(checked_idx) != 1:
            QMessageBox.information(
                self, "Pick exactly one",
                "Check exactly one turn to edit (you can't edit multiple at once).",
            )
            return
        i = checked_idx[0]
        msg = self._turns[i]
        title = f"Edit turn — {'You' if msg.role == 'user' else 'AI'}"
        new_text, ok = QInputDialog.getMultiLineText(
            self, title, "Content:", msg.content,
        )
        if not ok:
            return
        # Preserve image / had_image; replace only content.
        self._turns[i] = Message(
            role=msg.role,
            content=new_text,
            image=msg.image,
            had_image=msg.had_image,
        )
        log.info("[curate] Edited turn %d (new len=%d chars)", i, len(new_text))
        self._rebuild_rows()

    # ------------------------------------------------------------------ #
    # Summarize → Memory
    # ------------------------------------------------------------------ #

    def _refresh_summarize_button(self) -> None:
        """Enable Summarize only when the AI + a usable memory store are both
        available. Surfaces the actual blocker (disabled, missing chromadb,
        missing API key) in the tooltip — no more silent burning of tokens
        followed by a quiet 'skip' in the logs.
        """
        if self._ai_client is None:
            reason: Optional[str] = "AI client unavailable."
        elif self._memory is None:
            reason = "Long-term memory store is not initialized."
        else:
            reason = self._memory.probe()
        can = reason is None
        self._summarize_btn.setEnabled(can)
        if can:
            self._summarize_btn.setToolTip(
                "Ask the AI to distill the checked turns into a long-term "
                "memory entry (saved into ChromaDB) tagged for this "
                "template. You can edit the result before saving."
            )
        else:
            self._summarize_btn.setToolTip(
                f"Summarize is unavailable: {reason}"
            )

    def _summarize_to_memory(self) -> None:
        if self._summ_thread is not None:
            return  # already in flight
        if self._ai_client is None or self._memory is None:
            return

        checked = [m for m, row in zip(self._turns, self._rows)
                   if row.is_checked()]
        if not checked:
            QMessageBox.information(
                self, "Nothing selected",
                "Check at least one turn to summarize.",
            )
            return

        # Build transcript + summarization prompt.
        transcript = "\n\n".join(
            f"{'User' if m.role == 'user' else 'Assistant'}: {m.content}"
            for m in checked
        )
        prompt = (
            "Summarize the following exchange(s) into a concise lesson "
            "(2-3 sentences) that captures the key insight or pattern. "
            "Write it as a standalone takeaway that could help answer "
            "similar future questions. Use plain language. Don't include "
            "meta-references like 'the user asked'.\n\n"
            f"{transcript}"
        )
        log.info("[curate] Summarizing %d turn(s) for template '%s'",
                 len(checked), self._template_name)

        ai = self._ai_client

        def call() -> str:
            return ai.send(prompt, image=None)

        self._set_busy(True)
        self._summ_thread, self._summ_worker = run_in_background(
            self, call,
            on_finished=self._on_summary_finished,
            on_failed=self._on_summary_failed,
        )

    def _on_summary_finished(self, summary) -> None:
        self._set_busy(False)
        # Always tell the overlay that an API call completed so the
        # usage strip reflects the summarization tokens.
        self.summary_saved.emit()
        if not isinstance(summary, str) or not summary.strip():
            QMessageBox.warning(
                self, "Empty summary",
                "The AI returned an empty response. Try again, or "
                "select different turns.",
            )
            return

        review = _SummaryReviewDialog(summary.strip(), parent=self)
        if review.exec() != QDialog.DialogCode.Accepted:
            # User cancelled the review — close the curate dialog too
            # (per user request: OK and Cancel both exit the curate flow).
            # accept() so any prior turn edits the user made do propagate
            # back to the overlay's conversation.
            log.debug("[curate] Summary review cancelled — closing curate.")
            self.accept()
            return
        final = review.text().strip()
        if not final:
            # Empty post-edit summary — nothing to save; still close.
            log.debug("[curate] Empty edited summary — closing curate.")
            self.accept()
            return

        # Dispatch the actual chromadb write on a worker thread. On first
        # local-model use this triggers the lazy sentence-transformers
        # materialization (~80MB load) which on the Qt main thread would
        # freeze the dialog for ~1s right at the moment of closing.
        self._begin_summary_save(final)

    def _begin_summary_save(self, final: str) -> None:
        """Fire-and-forget memory save — close the curate dialog *now*
        and let the embed/write happen on a background daemon thread.

        Uses a plain :class:`threading.Thread` rather than a QThread
        because we want the save to outlive the dialog without us
        having to manage QObject parent/lifetime juggling while
        ``accept()`` destroys ``self``. The daemon thread holds its own
        refs to ``memory`` / ``template_name`` / ``final`` via the
        closure, so the work runs to completion (or failure) regardless.

        Errors don't bubble up to a popup — by the time they'd surface
        the curate dialog is gone. Instead the failure is recorded on
        :attr:`MemoryStore.last_error`, which the overlay's usage
        strip indicator (``⚠ memory error``) reads on its next refresh
        (next Send, Settings open, etc.). The user explicitly asked
        for the dialog to close first, accepting that error surfacing
        is slightly delayed.
        """
        import threading
        assert self._memory is not None  # button-gate guarantees this

        memory = self._memory
        template_name = self._template_name

        def _save() -> None:
            try:
                memory.add_document(
                    template_name, final, metadata={"kind": "summary"},
                )
                log.info("[curate] Background-saved summary "
                         "(%d chars) for template '%s'.",
                         len(final), template_name)
            except MemoryUnavailable as exc:
                # _ensure_loaded failed (e.g. chromadb missing post-install,
                # API key cleared, etc.). add_document itself sets last_error
                # only on actual collection.add() failures, so we record here.
                log.warning("[curate] Summary save skipped: %s", exc)
                memory._record_error("save", exc)  # noqa: SLF001
            except Exception:  # noqa: BLE001
                log.exception("[curate] Summary save raised unexpectedly")
                # add_document's own except clause already recorded last_error
                # before re-raising, so we don't double-record here.

        threading.Thread(
            target=_save, daemon=True, name="memory-summary-save",
        ).start()
        log.info("[curate] Dispatched summary save (%d chars); closing curate.",
                 len(final))
        self.accept()  # close curate dialog now, propagate any turn edits

    def _on_summary_failed(self, message: str) -> None:
        self._set_busy(False)
        log.error("[curate] Summarization failed: %s", message)
        QMessageBox.warning(self, "Summarization failed", message)

    def _set_busy(self, busy: bool) -> None:
        """Toggle the in-flight summarization state — disables all actions
        and changes the Summarize button label while waiting on the AI."""
        if busy:
            self._summarize_btn.setText("Summarizing…")
        else:
            self._summarize_btn.setText("Summarize → Memory…")
            self._summ_thread = None
            self._summ_worker = None
        for btn in (self._summarize_btn, self._delete_btn, self._edit_btn,
                    self._save_btn, self._cancel_btn):
            btn.setEnabled(not busy)
        # Restore the summarize button's gated-state on exit from busy.
        if not busy:
            self._refresh_summarize_button()

    # ------------------------------------------------------------------ #
    # Block dialog close while a summarization is in flight (the worker
    # holds a `self` reference and would touch destroyed widgets if it
    # finished after close).
    # ------------------------------------------------------------------ #

    def reject(self) -> None:
        # Only the AI summarization phase blocks close — its QThread
        # worker emits back into this dialog's slots, so destroying mid-
        # call would touch dead widgets. The memory save is a detached
        # daemon thread (see _begin_summary_save) and is safe to outlive
        # the dialog.
        if self._summ_thread is not None:
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._summ_thread is not None:
            event.ignore()
            return
        super().closeEvent(event)

    # ------------------------------------------------------------------ #
    # Save
    # ------------------------------------------------------------------ #

    def _on_save_clicked(self) -> None:
        # Soft safety: most providers reject conversations that don't
        # start with a user turn. Warn but let the user proceed.
        if self._turns and self._turns[0].role == "assistant":
            reply = QMessageBox.warning(
                self, "Thread starts with assistant",
                "This thread now starts with an assistant turn. Most AI "
                "providers reject conversations that don't start with a "
                "user message — your next Send may fail.\n\nSave anyway?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Save:
                return
        log.info("[curate] Saving changes (%d turns)", len(self._turns))
        self.accept()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def get_turns(self) -> list[Message]:
        """Return the current working copy of turns."""
        return list(self._turns)


# ---------------------------------------------------------------------- #
# Summary-review modal (replacement for QInputDialog.getMultiLineText)
# ---------------------------------------------------------------------- #

class _SummaryReviewDialog(QDialog):
    """Edit-and-confirm modal for the AI's drafted memory summary.

    Replaces ``QInputDialog.getMultiLineText`` for two reasons:

    * Default size is too small to comfortably read/edit a 200-300 char
      paragraph — this defaults to 720×520.
    * Adds a **Full screen** toggle for users who want even more room
      (handy if the AI ran long, or for screen-recording sessions).

    Result is read via :meth:`text` after the dialog returns
    ``DialogCode.Accepted``.
    """

    def __init__(self, initial: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review summary")
        self.setWindowFlags(Qt.WindowType.Window)  # max button + resize
        self.resize(720, 520)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        intro = QLabel(
            "Edit if needed, then click OK to save into long-term "
            "memory (ChromaDB). Cancel discards the summary — the AI "
            "call has already been billed."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._edit = QTextEdit()
        self._edit.setPlainText(initial)
        self._edit.setAcceptRichText(False)
        layout.addWidget(self._edit, 1)

        bottom = QHBoxLayout()
        self._fullscreen_btn = QPushButton("Full screen")
        self._fullscreen_btn.setCheckable(True)
        self._fullscreen_btn.setToolTip(
            "Toggle a maximized window for more editing room."
        )
        self._fullscreen_btn.toggled.connect(self._toggle_fullscreen)
        bottom.addWidget(self._fullscreen_btn)
        bottom.addStretch()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        bottom.addWidget(buttons)
        layout.addLayout(bottom)

        # Esc cancels even when focus is in the text edit. The default
        # button-box behavior already accepts on Enter from buttons, but
        # Enter inside the QTextEdit inserts a newline — that's correct
        # for a multi-line editor, so we don't bind a shortcut for OK.
        QShortcut(QKeySequence("Esc"), self, activated=self.reject)

    def _toggle_fullscreen(self, on: bool) -> None:
        if on:
            self.showMaximized()
            self._fullscreen_btn.setText("Exit full screen")
        else:
            self.showNormal()
            self._fullscreen_btn.setText("Full screen")

    def text(self) -> str:
        return self._edit.toPlainText()
