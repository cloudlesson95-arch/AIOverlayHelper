"""Modal dialog for reviewing and saving an AI-proposed template.

Triggered by the overlay's **Analyze image…** action once
:func:`variable_resolver.propose_template_from_image` returns a
:class:`TemplateProposal`. The user can edit any field before saving,
then ``Save as new template`` emits ``saved(dict)`` with a ready-to-write
``settings.yaml`` template entry (values become variable defaults).
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.logger import get_logger
from src.variable_resolver import TemplateProposal

log = get_logger(__name__)


# ---------------------------------------------------------------------- #
# One-row editor for a proposed variable
# ---------------------------------------------------------------------- #

class _ProposedVarCard(QFrame):
    """Editable card mirroring the shape of a settings VariableCard, plus a value field."""

    def __init__(self, name: str, value: str, prefix: str, suffix: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Raised)
        grid = QGridLayout(self)
        grid.setContentsMargins(10, 8, 10, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("variable name (snake_case)")
        font = self.name_edit.font()
        font.setBold(True)
        self.name_edit.setFont(font)
        grid.addWidget(QLabel("Name:"), 0, 0)
        grid.addWidget(self.name_edit, 0, 1)

        self.value_edit = QLineEdit(value)
        self.value_edit.setPlaceholderText("extracted value (becomes default)")
        grid.addWidget(QLabel("Value:"), 1, 0)
        grid.addWidget(self.value_edit, 1, 1)

        self.prefix_edit = QLineEdit(prefix)
        self.prefix_edit.setPlaceholderText("text inserted BEFORE the value")
        grid.addWidget(QLabel("Prefix:"), 2, 0)
        grid.addWidget(self.prefix_edit, 2, 1)

        self.suffix_edit = QLineEdit(suffix)
        self.suffix_edit.setPlaceholderText("text inserted AFTER the value")
        grid.addWidget(QLabel("Suffix:"), 3, 0)
        grid.addWidget(self.suffix_edit, 3, 1)

        grid.setColumnStretch(1, 1)

    def to_dict(self) -> dict:
        # value → default, default_on=true so user sees it in the overlay
        return {
            "name": self.name_edit.text().strip(),
            "prefix": self.prefix_edit.text(),
            "suffix": self.suffix_edit.text(),
            "default": self.value_edit.text(),
            "default_on": True,
        }


# ---------------------------------------------------------------------- #
# The dialog
# ---------------------------------------------------------------------- #

class ProposalDialog(QDialog):
    """Review-and-save modal for an AI-proposed template."""

    saved = pyqtSignal(dict)  # template dict ready for settings.yaml

    def __init__(self, proposal: TemplateProposal, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review proposed template")
        self.setMinimumSize(620, 540)
        self.setModal(True)

        self._var_cards: list[_ProposedVarCard] = []
        self._build_ui(proposal)
        log.debug("ProposalDialog opened (name=%r, vars=%d)",
                  proposal.name, len(proposal.variables))

    def _build_ui(self, proposal: TemplateProposal) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        header = QLabel("The AI proposed this template from your screenshot. "
                        "Edit anything, then save.")
        header.setWordWrap(True)
        root.addWidget(header)

        # Name
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.name_edit = QLineEdit(proposal.name)
        self.name_edit.setPlaceholderText("template name")
        name_row.addWidget(self.name_edit, 1)
        root.addLayout(name_row)

        # Text
        root.addWidget(QLabel("Template text:"))
        self.text_edit = QTextEdit()
        self.text_edit.setPlainText(proposal.text)
        self.text_edit.setPlaceholderText(
            "Prompt text with {placeholder} markers — one per variable."
        )
        self.text_edit.setFixedHeight(120)
        root.addWidget(self.text_edit)

        # include_screenshot
        self.shot_cb = QCheckBox("Remind to take screenshot when using this template")
        self.shot_cb.setChecked(proposal.include_screenshot)
        root.addWidget(self.shot_cb)

        # Variables
        root.addWidget(QLabel("Variables:"))
        self.var_scroll = QScrollArea()
        self.var_scroll.setWidgetResizable(True)
        var_host = QWidget()
        self._var_layout = QVBoxLayout(var_host)
        self._var_layout.setContentsMargins(0, 0, 0, 0)
        self._var_layout.setSpacing(8)
        for v in proposal.variables:
            card = _ProposedVarCard(v.name, v.value, v.prefix, v.suffix)
            self._var_cards.append(card)
            self._var_layout.addWidget(card)
        self._var_layout.addStretch()
        self.var_scroll.setWidget(var_host)
        root.addWidget(self.var_scroll, 1)

        # Buttons
        bottom = QHBoxLayout()
        bottom.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Save as new template")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save_clicked)
        bottom.addWidget(cancel_btn)
        bottom.addWidget(save_btn)
        root.addLayout(bottom)

    # ------------------------------------------------------------------ #
    # Save
    # ------------------------------------------------------------------ #

    def _on_save_clicked(self) -> None:
        name = self.name_edit.text().strip()
        text = self.text_edit.toPlainText()
        if not name:
            QMessageBox.warning(self, "Missing name", "Template name cannot be empty.")
            return
        if not text:
            QMessageBox.warning(self, "Missing text", "Template text cannot be empty.")
            return

        variables = []
        seen: set[str] = set()
        for card in self._var_cards:
            d = card.to_dict()
            if not d["name"]:
                QMessageBox.warning(self, "Invalid variable",
                                    "Every variable needs a name.")
                return
            if d["name"] in seen:
                QMessageBox.warning(self, "Duplicate variable",
                                    f"Variable {{{d['name']}}} is listed twice.")
                return
            seen.add(d["name"])
            variables.append(d)

        template_dict = {
            "name": name,
            "text": text,
            "include_screenshot": self.shot_cb.isChecked(),
            "variables": variables,
        }
        log.info("[proposal] Saving template '%s' (%d vars)", name, len(variables))
        self.saved.emit(template_dict)
        self.accept()
