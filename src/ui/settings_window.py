"""Settings editor window — separate hotkey opens this.

Lets the user edit provider, model, hotkeys, logging, and templates.
Emits a ``settings_saved`` signal so the App can live-reload everything
without restarting.

Template editor highlights:

* Per-template ``Remind to take screenshot`` flag (shows a confirmation
  in the overlay if the user tries to send without one).
* Per-variable cards with ``prefix`` / ``suffix`` / ``default`` /
  ``default_on``. Cards auto-sync with the template text on a 600ms
  debounce: typing ``{newvar}`` adds a card; deleting ``{var}`` removes
  it. ``+ Add variable`` is a faster path that inserts ``{name}`` at the
  cursor and creates a card in one step.
"""
from __future__ import annotations
import html
import re
from typing import TYPE_CHECKING, Callable, Optional

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.config import save_settings
from src.hotkeys import prettify_combo as _prettify_combo
from src.logger import get_logger
from src.templates import VARIABLE_RE, Template, Variable

if TYPE_CHECKING:
    from src.memory import MemoryStore

log = get_logger(__name__)


# Curated model lists per provider, shown in the Model tab's dropdown.
# The combo is editable, so users with newer/older/fine-tuned/local models
# can still type a custom string — these are just one-click defaults.
# Keep in rough "cheapest → priciest" order so the cheap default is first.
_PROVIDER_MODELS: dict[str, list[str]] = {
    "openai": [
        "gpt-4o-mini",
        "gpt-4.1-mini",
        "gpt-4o",
        "gpt-4.1",
    ],
    "anthropic": [
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
        "claude-opus-4-7",
    ],
    "gemini": [
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-2.5-pro",
    ],
    "ollama": [
        # Common local picks; users must `ollama pull <name>` themselves.
        "llama3.2",
        "llama3.1",
        "qwen2.5",
        "phi3",
    ],
}

# Same idea but for embedding models in the Memory tab. Local names are
# sentence-transformers model IDs and auto-download on first use.
_EMBED_MODELS: dict[str, list[str]] = {
    "gemini": [
        "gemini-embedding-001",
    ],
    "openai": [
        "text-embedding-3-small",
        "text-embedding-3-large",
        "text-embedding-ada-002",
    ],
    "local": [
        "all-MiniLM-L6-v2",
        "all-mpnet-base-v2",
        "all-MiniLM-L12-v2",
    ],
}


# ---------------------------------------------------------------------- #
# Per-variable card
# ---------------------------------------------------------------------- #

class VariableCard(QFrame):
    """Editable card for one template variable."""

    remove_clicked = pyqtSignal(str)  # variable name
    # Fires when any of prefix/suffix/default/default_on changes; the
    # Settings window listens so it can refresh the live template preview.
    changed = pyqtSignal()

    def __init__(self, data: dict):
        super().__init__()
        self.var_name: str = data["name"]
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Raised)
        self._build_ui(data)
        # Wire change emitters after _build_ui so the line edits exist.
        for edit in (self.prefix_edit, self.suffix_edit, self.default_edit):
            edit.textChanged.connect(self.changed.emit)
        self.default_on_cb.toggled.connect(self.changed.emit)

    def _build_ui(self, d: dict) -> None:
        grid = QGridLayout(self)
        grid.setContentsMargins(10, 8, 10, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        title = QLabel(f"{{{self.var_name}}}")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        grid.addWidget(title, 0, 0, 1, 2)

        remove_btn = QPushButton("✕")
        remove_btn.setFixedSize(22, 22)
        remove_btn.setToolTip(f"Remove {{{self.var_name}}} from this template")
        remove_btn.clicked.connect(lambda: self.remove_clicked.emit(self.var_name))
        grid.addWidget(remove_btn, 0, 2)

        self.prefix_edit = QLineEdit(d.get("prefix", ""))
        self.prefix_edit.setPlaceholderText("text inserted BEFORE the value")
        grid.addWidget(QLabel("Prefix:"), 1, 0)
        grid.addWidget(self.prefix_edit, 1, 1, 1, 2)

        self.suffix_edit = QLineEdit(d.get("suffix", ""))
        self.suffix_edit.setPlaceholderText("text inserted AFTER the value")
        grid.addWidget(QLabel("Suffix:"), 2, 0)
        grid.addWidget(self.suffix_edit, 2, 1, 1, 2)

        self.default_edit = QLineEdit(d.get("default", ""))
        self.default_edit.setPlaceholderText("pre-filled value in the overlay")
        grid.addWidget(QLabel("Default:"), 3, 0)
        grid.addWidget(self.default_edit, 3, 1, 1, 2)

        self.default_on_cb = QCheckBox("Default ON in overlay")
        self.default_on_cb.setChecked(bool(d.get("default_on", True)))
        self.default_on_cb.setToolTip(
            "If checked, the variable starts toggled-on when the overlay opens.\n"
            "If unchecked, the user has to explicitly enable it before sending."
        )
        grid.addWidget(self.default_on_cb, 4, 0, 1, 3)

        grid.setColumnStretch(1, 1)

    def to_dict(self) -> dict:
        return {
            "name": self.var_name,
            "prefix": self.prefix_edit.text(),
            "suffix": self.suffix_edit.text(),
            "default": self.default_edit.text(),
            "default_on": self.default_on_cb.isChecked(),
        }


# ---------------------------------------------------------------------- #
# Hotkey row
# ---------------------------------------------------------------------- #
# _prettify_combo lives in src.hotkeys (imported above) so the overlay's
# Send button can use the same display-only translation without a UI
# layer pulling on Settings.

class HotkeyRow(QWidget):
    """One hotkey field with **Edit** / **Default** controls.

    Normal state: a read-only line edit shows the current Qt-format combo.
    Click **Edit** → the field is swapped for a focused ``QKeySequenceEdit``
    that captures the next combo; Edit relabels itself "Done" and Default
    relabels "Cancel" so the active action is unambiguous. Pressing Done
    commits the captured sequence (or keeps the old one if nothing was
    pressed). Cancel discards the capture.

    **Default** restores the per-row default combo passed at construction
    time. The widget owns no policy — the parent reads :meth:`value` on
    Save and the captured string flows back into ``settings["hotkeys"]``.

    ``on_commit`` (optional): callback the parent passes in to vet a
    just-captured combo *before* it lands in the display field. Returns
    True to accept, False to discard. Used by SettingsWindow to detect
    hotkey conflicts and ask the user whether to overwrite or cancel.

    ``default_button_text`` / ``default_button_tooltip``: customize the
    secondary action button. For template hotkey rows (which have no
    built-in default), we relabel "Default" → "Clear" so the affordance
    matches what the button actually does.
    """

    def __init__(
        self,
        default_combo: str,
        on_commit: Optional[Callable[[str], bool]] = None,
        default_button_text: str = "Default",
        default_button_tooltip: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._default = default_combo
        self._capture: Optional[QKeySequenceEdit] = None
        self._on_commit = on_commit
        self._default_button_text = default_button_text
        # Canonical Qt-PortableText value ("Ctrl+Return"). _display
        # shows the prettified form via _prettify_combo so users see
        # "Ctrl+Enter" but pynput / QKeySequence keep getting the
        # exact name Qt produced.
        self._value: str = ""
        # Snapshot of the canonical value at the moment capture begins,
        # so Cancel can deterministically restore it.
        self._pre_capture_value: str = ""

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)

        self._display = QLineEdit()
        self._display.setReadOnly(True)
        self._display.setPlaceholderText("(unbound)")
        self._layout.addWidget(self._display, 1)

        self._edit_btn = QPushButton("Edit")
        self._edit_btn.setFixedWidth(64)
        self._edit_btn.clicked.connect(self._begin_capture)
        self._layout.addWidget(self._edit_btn)

        self._default_btn = QPushButton(default_button_text)
        self._default_btn.setFixedWidth(72)
        if default_button_tooltip is None:
            default_button_tooltip = (
                f"Restore default: {_prettify_combo(default_combo)}"
                if default_combo else "Clear this hotkey"
            )
        self._default_btn.setToolTip(default_button_tooltip)
        self._default_btn.clicked.connect(self._restore_default)
        self._layout.addWidget(self._default_btn)

    # -- public --------------------------------------------------------- #

    def set_value(self, combo: str) -> None:
        self._value = (combo or "").strip()
        self._display.setText(_prettify_combo(self._value))

    def value(self) -> str:
        # If capture is open, commit it implicitly so an un-clicked "Done"
        # isn't silently lost on Save.
        if self._capture is not None:
            self._commit_capture()
        return self._value.strip()

    # -- capture flow --------------------------------------------------- #

    def _begin_capture(self) -> None:
        if self._capture is not None:
            return
        # Stash the current canonical value so Cancel can restore it
        # explicitly. Defensive — _value shouldn't be mutated during
        # capture, but snapshotting eliminates any risk of an unrelated
        # mutation leaking through as a silent overwrite on Cancel.
        self._pre_capture_value = self._value
        self._capture = QKeySequenceEdit()
        # Limit to a single combo, otherwise QKeySequenceEdit happily
        # records multi-stroke sequences like "Ctrl+K, Ctrl+S" which
        # neither pynput nor our QShortcut wiring would honor.
        self._capture.setMaximumSequenceLength(1)
        # Replace the display field in-place so the row width stays stable.
        self._layout.replaceWidget(self._display, self._capture)
        self._display.hide()
        self._capture.show()
        self._capture.setFocus()

        self._edit_btn.setText("Done")
        self._edit_btn.clicked.disconnect()
        self._edit_btn.clicked.connect(self._commit_capture)
        self._default_btn.setText("Cancel")
        self._default_btn.clicked.disconnect()
        self._default_btn.clicked.connect(self._cancel_capture)

    def _commit_capture(self) -> None:
        if self._capture is None:
            return
        captured = self._capture.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        # IMPORTANT: tear down BEFORE invoking on_commit. The callback
        # (SettingsWindow._resolve_hotkey_conflict) walks every
        # HotkeyRow.value() to scan for duplicates, and value()
        # auto-commits any open capture — i.e. it re-enters this
        # method on the same row. With _capture still set, the
        # inner call's teardown nulls _capture, then the outer
        # teardown blows up on `assert self._capture is not None`.
        # Resetting first leaves the inner value() to return the
        # display text harmlessly. Setting the post-conflict text
        # is the only thing left for either branch.
        self._teardown_capture()
        if captured and self._on_commit is not None:
            try:
                accepted = bool(self._on_commit(captured))
            except Exception:  # noqa: BLE001 — never let a callback bug eat the capture
                log.exception("HotkeyRow on_commit raised; treating as accept")
                accepted = True
            if not accepted:
                # User cancelled the conflict — restore canonical snapshot.
                self._value = self._pre_capture_value
                self._display.setText(_prettify_combo(self._value))
                return
        if captured:
            self._value = captured
            self._display.setText(_prettify_combo(captured))
        # else: empty capture (user clicked Done without pressing anything)
        # — keep the previous value untouched.

    def _cancel_capture(self) -> None:
        if self._capture is None:
            return
        self._teardown_capture()
        # Explicit restore from the canonical snapshot.
        self._value = self._pre_capture_value
        self._display.setText(_prettify_combo(self._value))

    def _teardown_capture(self) -> None:
        assert self._capture is not None
        self._layout.replaceWidget(self._capture, self._display)
        self._capture.deleteLater()
        self._capture = None
        self._display.show()

        self._edit_btn.setText("Edit")
        self._edit_btn.clicked.disconnect()
        self._edit_btn.clicked.connect(self._begin_capture)
        self._default_btn.setText(self._default_button_text)
        self._default_btn.clicked.disconnect()
        self._default_btn.clicked.connect(self._restore_default)

    def _restore_default(self) -> None:
        self._value = self._default
        self._display.setText(_prettify_combo(self._default))


# ---------------------------------------------------------------------- #
# Color picker button
# ---------------------------------------------------------------------- #

class ColorButton(QPushButton):
    """A compact swatch-button that opens :class:`QColorDialog` on click.

    Stores its value as a ``#RRGGBB`` hex string. The button paints
    itself in the current color and shows the hex value as its label
    (auto-contrasted black/white text). Used by the Chat styles tab
    for the per-role background-color pickers — settings.yaml stores
    just the hex; the renderer ([overlay._build_markdown_css]) is
    responsible for fading it to a soft alpha.
    """

    color_changed = pyqtSignal(str)

    def __init__(self, initial: str = "#888888") -> None:
        super().__init__()
        self.setFixedWidth(110)
        self.setFixedHeight(28)
        self._color: str = "#888888"
        self.set_color(initial)
        self.clicked.connect(self._pick)

    def color(self) -> str:
        return self._color

    def set_color(self, hex_color: str) -> None:
        c = (hex_color or "").strip()
        if not c.startswith("#"):
            c = "#" + c if c else "#888888"
        if len(c) not in (4, 7):
            c = "#888888"
        self._color = c
        text_color = self._contrasting_text(c)
        self.setStyleSheet(
            "QPushButton {"
            f" background-color: {c};"
            f" color: {text_color};"
            " border: 1px solid rgba(255,255,255,60);"
            " border-radius: 4px;"
            "}"
            "QPushButton:hover { border: 1px solid rgba(255,255,255,140); }"
        )
        self.setText(c)
        self.color_changed.emit(c)

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(
            QColor(self._color), self, "Pick color",
            QColorDialog.ColorDialogOption.DontUseNativeDialog,
        )
        if chosen.isValid():
            self.set_color(chosen.name())

    @staticmethod
    def _contrasting_text(hex_color: str) -> str:
        """Pick a black or white text color whichever reads on the swatch."""
        h = hex_color.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            return "#000"
        try:
            r = int(h[0:2], 16)
            g = int(h[2:4], 16)
            b = int(h[4:6], 16)
        except ValueError:
            return "#000"
        # Rec. 709 luminance — same formula the renderer uses for tint alpha.
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        return "#000" if lum > 140 else "#fff"


# ---------------------------------------------------------------------- #
# Settings window
# ---------------------------------------------------------------------- #

class SettingsWindow(QWidget):
    """Editor for ``settings.yaml``."""

    settings_saved = pyqtSignal(dict)

    # ms to wait after the user stops typing before syncing variable
    # cards with the template text.
    _SYNC_DEBOUNCE_MS = 600

    def __init__(self, settings: dict, memory: Optional["MemoryStore"] = None):
        super().__init__()
        # Deep-ish copy so cancel doesn't mutate the live settings.
        self.settings: dict = {
            "provider": settings.get("provider", "openai"),
            "model": settings.get("model", ""),
            "hotkeys": dict(settings.get("hotkeys", {})),
            "logging": dict(settings.get("logging", {})),
            "memory": dict(settings.get("memory", {})),
            "framed_window": bool(settings.get("framed_window", False)),
            "window_width": int(settings.get("window_width") or 540),
            "window_height": int(settings.get("window_height") or 460),
            "chat_style": dict(settings.get("chat_style") or {}),
            "templates": [
                # Templates are nested dicts; deep-copy variables too.
                {**t, "variables": [dict(v) for v in t.get("variables", [])]
                                  if t.get("variables") is not None else None}
                for t in settings.get("templates", [])
            ],
        }
        # Live MemoryStore (current process). Used for the "N entries" / Clear
        # row — we only inspect/mutate it; replacement happens on Save via the
        # App, which builds a fresh store from the saved config.
        self._memory = memory

        self.var_cards: dict[str, VariableCard] = {}
        self._loading_template = False  # suppress sync while populating cards

        self.setWindowTitle("AI Overlay — Settings")
        self.resize(840, 760)

        self._build_ui()
        self._load_into_ui()
        log.debug("SettingsWindow opened.")

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        """Tabbed layout: Model / Hotkeys / Memory / Additional / Templates.

        Each tab is its own QWidget built by a `_build_*_tab` helper.
        Widget attribute names (``self.provider_combo``, ``self.t_text``,
        etc.) are preserved across the refactor so the load / save / sync
        helpers don't need to change.
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)

        tabs = QTabWidget()
        tabs.addTab(self._build_model_tab(), "Model")
        tabs.addTab(self._build_hotkeys_tab(), "Hotkeys")
        tabs.addTab(self._build_memory_tab(), "Memory")
        tabs.addTab(self._build_chat_styles_tab(), "Chat styles")
        tabs.addTab(self._build_additional_tab(), "Additional")
        tabs.addTab(self._build_templates_tab(), "Templates")
        root.addWidget(tabs, 1)

        # Save / Apply / Cancel sit below the tab strip. Buttons go on the
        # left (stretch is at the right) per the requested layout. Apply
        # persists without closing — useful when iterating on hotkeys /
        # templates / memory settings without re-opening the window each
        # time. Save = Apply + close.
        bottom = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.setToolTip("Save changes and close the window.")
        apply_btn = QPushButton("Apply")
        apply_btn.setToolTip("Save changes but keep this window open.")
        cancel_btn = QPushButton("Cancel")
        save_btn.clicked.connect(self.save)
        apply_btn.clicked.connect(self.apply)
        cancel_btn.clicked.connect(self.close)
        bottom.addWidget(save_btn)
        bottom.addWidget(apply_btn)
        bottom.addWidget(cancel_btn)
        bottom.addStretch()
        root.addLayout(bottom)

    # ------------------------------------------------------------------ #
    # Per-tab builders
    # ------------------------------------------------------------------ #

    def _build_model_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 16, 12, 12)
        form = QFormLayout()

        self.provider_combo = QComboBox()
        self.provider_combo.addItems(["openai", "anthropic", "gemini", "ollama"])

        # Editable so users can type a custom model (newer releases, fine-tuned
        # models, locally-pulled Ollama tags) that isn't in the curated list.
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_combo.lineEdit().setPlaceholderText(
            "Pick a suggested model, or type your own"
        )
        self.model_combo.setToolTip(
            "Suggestions are curated per provider; cost tracking uses these "
            "names. You can still type any custom string (e.g. a fine-tuned "
            "model or a local Ollama tag); it just won't have a known $ rate."
        )

        form.addRow("Provider:", self.provider_combo)
        form.addRow("Model:", self.model_combo)
        layout.addLayout(form)
        layout.addStretch()

        # Repopulate the model list whenever the provider changes. Don't
        # clobber a custom model the user typed — only reset to the new
        # provider's first suggestion when the current text doesn't match
        # any saved entry.
        self.provider_combo.currentTextChanged.connect(
            self._on_provider_changed
        )
        return page

    def _on_provider_changed(self, provider: str) -> None:
        """Repopulate the model dropdown for the newly-selected provider."""
        current = self.model_combo.currentText().strip()
        models = _PROVIDER_MODELS.get(provider, [])
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(models)
        # If the previously-shown model isn't a suggestion for this provider,
        # default to the first (cheapest) one. If the user had typed a custom
        # string we still want to swap — providers don't share model names.
        if current and current in models:
            self.model_combo.setCurrentText(current)
        elif models:
            self.model_combo.setCurrentIndex(0)
        else:
            self.model_combo.setEditText("")
        self.model_combo.blockSignals(False)

    # Tab order + per-key defaults for the Hotkeys tab. The default strings
    # MUST stay in sync with ``config.DEFAULT_SETTINGS["hotkeys"]`` — that's
    # where new installs pick their seed values, and what each row's
    # "Default" button restores to. (Single source of truth would be nicer
    # but pulling config into UI code adds an import cycle risk.)
    _HOTKEY_ROWS = (
        ("summon_overlay", "Summon / hide overlay:", "Ctrl+Alt+Space"),
        ("open_settings",  "Open settings:",         "Ctrl+Alt+,"),
        ("send_prompt",    "Send prompt:",           "Ctrl+Return"),
        ("next_template",  "Next template:",         "Ctrl+Alt+Right"),
        ("prev_template",  "Previous template:",     "Ctrl+Alt+Left"),
    )

    # Pretty labels used in the conflict dialog so the user sees
    # "Hotkeys → Summon overlay" instead of the raw key name.
    _HOTKEY_LABELS = {
        "summon_overlay": "Hotkeys → Summon / hide overlay",
        "open_settings":  "Hotkeys → Open settings",
        "send_prompt":    "Hotkeys → Send prompt",
        "next_template":  "Hotkeys → Next template",
        "prev_template":  "Hotkeys → Previous template",
    }

    def _build_hotkeys_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 16, 12, 12)

        hint = QLabel(
            "Click <b>Edit</b> on a row, then press the desired key "
            "combination. Click <b>Done</b> to commit (or <b>Cancel</b> "
            "to discard). <b>Default</b> restores the built-in binding."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(hint)

        form = QFormLayout()
        # Keyed by hotkey-name so _load_into_ui / save() can address rows
        # generically rather than hard-coding two attribute names.
        self.hotkey_rows: dict[str, HotkeyRow] = {}
        for key, label, default in self._HOTKEY_ROWS:
            # Bind a per-row source key into the conflict resolver so it
            # knows which row's "Done" the captured combo came from
            # (needed to exclude that row from the conflict scan).
            row = HotkeyRow(
                default,
                on_commit=lambda combo, k=key: self._resolve_hotkey_conflict(
                    f"hotkey:{k}", combo,
                ),
            )
            self.hotkey_rows[key] = row
            form.addRow(label, row)
        layout.addLayout(form)
        layout.addStretch()
        return page

    def _build_memory_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 16, 12, 12)

        # Keep a direct reference to the form layout — we can't rely on
        # widget.parent().layout() because at addRow() time the layout
        # isn't yet attached to its parent QWidget; parent() returns None.
        self._mem_form = QFormLayout()
        form = self._mem_form
        self.mem_enabled = QCheckBox("Enable long-term memory")
        self.mem_enabled.setToolTip(
            "Master switch for the optional ChromaDB-backed long-term memory.\n"
            "Even when ON, only templates with 'Use long-term memory' "
            "checked actually save & retrieve. Plain per-template "
            "conversation history (the thread you see) works independently "
            "and is always on."
        )
        # Probe chromadb the moment the user ticks the box so we fail loud
        # at config time, not later when Summarize silently no-ops.
        self.mem_enabled.toggled.connect(self._on_mem_enabled_toggled)
        form.addRow("Memory:", self.mem_enabled)

        self.mem_backend = QComboBox()
        self.mem_backend.addItems(["gemini", "openai", "local"])
        self.mem_backend.setToolTip(
            "gemini / openai → cloud embeddings (uses your existing API key, "
            "tiny per-call cost).\n"
            "local → sentence-transformers, free + offline (~80MB model "
            "auto-downloads on first use)."
        )
        # When backend changes, repopulate the model dropdown AND reset
        # the selection to the new backend's default — different from
        # the Provider→Model behavior, where we preserved custom text.
        # The user said this is the expected UX.
        self.mem_backend.currentTextChanged.connect(self._on_embed_backend_changed)
        form.addRow("Embedding backend:", self.mem_backend)

        # Local-only: skip HuggingFace Hub revision-check on each model
        # load. Shown only when backend == "local"; visibility is toggled
        # by _on_embed_backend_changed. Stays in the form (just hidden)
        # so its row position is stable when the user flips backends.
        self.mem_local_offline_cb = QCheckBox(
            "Offline mode (don't check HuggingFace Hub for updates)"
        )
        self.mem_local_offline_cb.setToolTip(
            "When ON, sets HF_HUB_OFFLINE=1 before sentence-transformers "
            "loads. Skips the network HEAD request that otherwise checks "
            "for a newer model revision on every load — eliminates the "
            "'unauthenticated request' warning and lets the app work "
            "fully offline once the model is cached on disk.\n\n"
            "Requires app restart to change (huggingface_hub caches the "
            "value at import time)."
        )
        form.addRow("Local model:", self.mem_local_offline_cb)
        # Initial visibility matches whatever backend is currently selected.
        # _load_into_ui will refresh this after settings are read.
        self._set_local_only_visible(self.mem_backend.currentText() == "local")

        # Editable combo (like the AI Model tab) — suggestions one click
        # away, custom strings still possible for new/local models.
        self.mem_model = QComboBox()
        self.mem_model.setEditable(True)
        self.mem_model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.mem_model.lineEdit().setPlaceholderText(
            "Pick a suggested model, or type your own"
        )
        self.mem_model.setToolTip(
            "Suggestions are curated per backend. For local you can paste "
            "any sentence-transformers model ID — it'll auto-download on "
            "first use. Use 'Test embedding' below to verify a model "
            "works before saving."
        )
        form.addRow("Embedding model:", self.mem_model)

        self.mem_top_k = QSpinBox()
        self.mem_top_k.setRange(1, 10)
        self.mem_top_k.setToolTip(
            "How many past exchanges to retrieve and inject per Send."
        )
        form.addRow("Top-K:", self.mem_top_k)

        # Test embedding row. Hits the configured backend + model with
        # a tiny test string; for local backends this also triggers the
        # one-time model download so the first real Send isn't a silent
        # 60-second hang.
        mem_test_row = QHBoxLayout()
        self.mem_test_btn = QPushButton("Test embedding")
        self.mem_test_btn.setToolTip(
            "Run a real round-trip embed against the current backend + "
            "model. Surfaces deprecated models, bad API keys, network "
            "issues. For 'local', this also kicks off the one-time "
            "~80MB model download."
        )
        self.mem_test_btn.clicked.connect(self._test_embedding)
        mem_test_row.addWidget(self.mem_test_btn)
        mem_test_row.addStretch()
        form.addRow("", mem_test_row)

        mem_status_row = QHBoxLayout()
        self.mem_status_label = QLabel("…")
        mem_status_row.addWidget(self.mem_status_label, 1)
        self.mem_clear_btn = QPushButton("Clear all memory")
        self.mem_clear_btn.setToolTip(
            "Delete all stored memory entries (across all templates) "
            "immediately.\n\n"
            "Use this when you want to wipe memory WITHOUT changing the "
            "embedding backend or model. If you change backend/model "
            "above and click Save / Apply, the wipe is offered "
            "automatically — no need to click this first."
        )
        self.mem_clear_btn.clicked.connect(self._clear_memory)
        mem_status_row.addWidget(self.mem_clear_btn)
        form.addRow("Stored:", mem_status_row)

        layout.addLayout(form)
        layout.addStretch()
        return page

    def _on_embed_backend_changed(self, backend: str) -> None:
        """Repopulate embed model dropdown + reset to that backend's default,
        and show/hide local-only controls.

        Different from ``_on_provider_changed`` in the Model tab: there we
        keep the user's typed text across provider switches if possible.
        Here the user explicitly asked for the model to **change** with
        the backend (each backend uses entirely different model names
        and dimensions, so reusing the previous string is almost never
        what you want — and chromadb's per-collection dim lock makes a
        mismatch a hard error anyway).
        """
        models = _EMBED_MODELS.get(backend, [])
        self.mem_model.blockSignals(True)
        self.mem_model.clear()
        self.mem_model.addItems(models)
        if models:
            self.mem_model.setCurrentIndex(0)
        else:
            self.mem_model.setEditText("")
        self.mem_model.blockSignals(False)
        self._set_local_only_visible(backend == "local")

    def _set_local_only_visible(self, visible: bool) -> None:
        """Toggle the visibility of controls that only apply to the local
        backend (currently just the HF offline-mode checkbox).

        Uses ``QFormLayout.setRowVisible`` against the stored form layout
        reference so the QLabel and the widget hide together — manually
        hiding just the checkbox would leave an orphan 'Local model:'
        label sitting in the form.
        """
        if not hasattr(self, "_mem_form") or not hasattr(self, "mem_local_offline_cb"):
            return  # called before build finished
        self._mem_form.setRowVisible(self.mem_local_offline_cb, visible)

    def _build_additional_tab(self) -> QWidget:
        """Catch-all for cross-cutting prefs that don't fit elsewhere."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 16, 12, 12)
        form = QFormLayout()

        self.log_enabled = QCheckBox("Enable debug logging")
        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        form.addRow("Logging:", self.log_enabled)
        form.addRow("Log level:", self.log_level)

        self.framed_window_cb = QCheckBox(
            "Show window chrome (title bar, close/maximize, resizable)"
        )
        self.framed_window_cb.setToolTip(
            "When OFF (default): the overlay is a frameless translucent "
            "always-on-top popup, like a hotkey-summoned palette.\n"
            "When ON: it becomes a normal OS window with a title bar, "
            "close/maximize/minimize buttons, and is resizable from any "
            "edge. Use this if you want to keep the overlay open while "
            "working in another app.\n"
            "Takes effect immediately on Save — no restart needed."
        )
        form.addRow("Overlay window:", self.framed_window_cb)

        # Overlay window size — applied on next Save / Apply via
        # OverlayWindow.update_runtime → self.resize(w, h).
        size_row = QHBoxLayout()
        self.window_width = QSpinBox()
        self.window_width.setRange(320, 3000)
        self.window_width.setSuffix(" px")
        self.window_height = QSpinBox()
        self.window_height.setRange(240, 3000)
        self.window_height.setSuffix(" px")
        size_row.addWidget(QLabel("W:"))
        size_row.addWidget(self.window_width)
        size_row.addWidget(QLabel("H:"))
        size_row.addWidget(self.window_height)
        size_row.addStretch()
        form.addRow("Window size:", size_row)

        layout.addLayout(form)
        layout.addStretch()
        return page

    # ------------------------------------------------------------------ #
    # Chat styles tab
    # ------------------------------------------------------------------ #

    # Curated font families shown in the dropdown; combo is editable so
    # users can paste any installed font name. "(default)" is rendered as
    # an empty string so QTextDocument falls back to its system font.
    _CHAT_FONT_SUGGESTIONS = (
        "(default)",
        "Segoe UI",
        "Arial",
        "Calibri",
        "Verdana",
        "Tahoma",
        "Georgia",
        "Times New Roman",
        "Consolas",
        "Courier New",
    )

    def _build_chat_styles_tab(self) -> QWidget:
        """Per-chat font/spacing/alignment controls. All values feed into
        overlay._build_markdown_css on the next Apply/Save.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 16, 12, 12)
        form = QFormLayout()

        self.chat_font_family = QComboBox()
        self.chat_font_family.setEditable(True)
        self.chat_font_family.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.chat_font_family.addItems(list(self._CHAT_FONT_SUGGESTIONS))
        self.chat_font_family.setToolTip(
            "Font for the chat area. Pick a suggestion or type any installed "
            "font name. '(default)' uses the system UI font."
        )
        form.addRow("Font family:", self.chat_font_family)

        self.chat_font_size = QSpinBox()
        self.chat_font_size.setRange(9, 24)
        self.chat_font_size.setSuffix(" px")
        form.addRow("Font size:", self.chat_font_size)

        self.chat_turn_spacing = QSpinBox()
        self.chat_turn_spacing.setRange(4, 48)
        self.chat_turn_spacing.setSuffix(" px")
        self.chat_turn_spacing.setToolTip(
            "Vertical gap between turn bubbles. Larger values give more "
            "breathing room between messages."
        )
        form.addRow("Spacing between turns:", self.chat_turn_spacing)

        self.chat_autoscroll_tolerance = QSpinBox()
        self.chat_autoscroll_tolerance.setRange(0, 200)
        self.chat_autoscroll_tolerance.setSuffix(" px")
        self.chat_autoscroll_tolerance.setToolTip(
            "How close to the bottom the scrollbar must be for streaming\n"
            "answers to auto-scroll. Larger = auto-follow re-engages more\n"
            "easily as you scroll back down; smaller = it lets go sooner\n"
            "when you scroll up. Default: 12 px."
        )
        form.addRow("Auto-scroll tolerance:", self.chat_autoscroll_tolerance)

        self.chat_user_align = QComboBox()
        self.chat_user_align.addItems(["left", "right"])
        self.chat_user_align.setToolTip(
            "Which side of the chat area your messages hug. Default: left."
        )
        form.addRow("Your messages align:", self.chat_user_align)

        self.chat_assistant_align = QComboBox()
        self.chat_assistant_align.addItems(["left", "right"])
        self.chat_assistant_align.setToolTip(
            "Which side of the chat area the AI's messages hug. Default: right."
        )
        form.addRow("AI messages align:", self.chat_assistant_align)

        self.chat_show_labels = QCheckBox("Show 'You' / 'AI' header on each turn")
        form.addRow("Labels:", self.chat_show_labels)

        # Per-role background tinting. Mode chooses where the picked
        # color shows up:
        #   never   – no background (default; matches the bare look)
        #   headers – just the You/AI header bar gets tinted
        #   all     – the whole turn bubble gets tinted
        # The renderer fades whichever hex color the picker returns to
        # a soft alpha so bright picks don't drown the message text.
        self.chat_user_bg_mode = QComboBox()
        self.chat_user_bg_mode.addItems(["never", "headers", "all"])
        self.chat_user_bg_mode.setToolTip(
            "Where to apply the user color: nowhere, just the 'You' "
            "header strip, or behind the entire user bubble."
        )
        form.addRow("Your background:", self.chat_user_bg_mode)

        self.chat_user_bg_color = ColorButton("#4a7dff")
        self.chat_user_bg_color.setToolTip(
            "Color used when 'Your background' is not 'never'. The "
            "renderer applies it at a soft transparency."
        )
        form.addRow("Your color:", self.chat_user_bg_color)

        self.chat_assistant_bg_mode = QComboBox()
        self.chat_assistant_bg_mode.addItems(["never", "headers", "all"])
        self.chat_assistant_bg_mode.setToolTip(
            "Where to apply the AI color: nowhere, just the 'AI' header "
            "strip, or behind the entire AI bubble."
        )
        form.addRow("AI background:", self.chat_assistant_bg_mode)

        self.chat_assistant_bg_color = ColorButton("#888888")
        self.chat_assistant_bg_color.setToolTip(
            "Color used when 'AI background' is not 'never'. The "
            "renderer applies it at a soft transparency."
        )
        form.addRow("AI color:", self.chat_assistant_bg_color)

        layout.addLayout(form)
        layout.addStretch()
        return page

    def _build_templates_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)

        body = QHBoxLayout()

        self.template_list = QListWidget()
        self.template_list.currentRowChanged.connect(self._select_template)
        self.template_list.setMaximumWidth(180)
        body.addWidget(self.template_list)

        edit_panel = QVBoxLayout()

        self.t_name = QLineEdit()
        self.t_name.setPlaceholderText("Template name")
        edit_panel.addWidget(self.t_name)

        self.t_text = QTextEdit()
        self.t_text.setPlaceholderText(
            "Template text — use {variable} placeholders, e.g.\n"
            "Explain {topic} in the style of {style}.\n\n"
            "Tip: put sentence context like ' Focus on ' / '.' into the "
            "variable's prefix/suffix so toggling it OFF in the overlay "
            "drops the whole segment."
        )
        self.t_text.setFixedHeight(130)
        edit_panel.addWidget(self.t_text)

        # Live-rendered preview: shows what Template.render would produce
        # using each variable's default value, respecting default_on. Same
        # idea as the overlay's prompt preview, but driven off the variable
        # cards instead of the overlay's runtime inputs.
        edit_panel.addWidget(QLabel("Prompt preview:"))
        self.t_preview = QTextEdit()
        self.t_preview.setReadOnly(True)
        self.t_preview.setObjectName("template_preview")
        self.t_preview.setStyleSheet(
            "QTextEdit#template_preview { font-style: italic; }"
        )
        self.t_preview.setPlaceholderText(
            "Renders the template with each variable's default value, "
            "skipping those with 'Default ON in overlay' unchecked."
        )
        self.t_preview.setFixedHeight(110)
        edit_panel.addWidget(self.t_preview)

        self.t_screenshot = QCheckBox("Remind to take screenshot")
        self.t_screenshot.setToolTip(
            "If checked, sending this template without an attached screenshot "
            "will prompt for confirmation."
        )
        edit_panel.addWidget(self.t_screenshot)

        self.t_use_memory = QCheckBox("Use long-term memory")
        self.t_use_memory.setToolTip(
            "When checked, this template participates in long-term memory: "
            "successful Sends are saved as past exchanges into ChromaDB, "
            "and the top-K most-similar past entries (raw exchanges + "
            "any summaries you curated) are injected into future Sends. "
            "Requires the global 'Enable long-term memory' switch on the "
            "Memory tab to be ON."
        )
        edit_panel.addWidget(self.t_use_memory)

        # Per-template global hotkey. Pressing it (anywhere) selects this
        # template and summons the overlay — a "speed dial" for prompts
        # the user reaches for often. Lives here rather than on the
        # Hotkeys tab because each template owns its own binding.
        hk_row = QHBoxLayout()
        hk_row.setContentsMargins(0, 0, 0, 0)
        hk_row.addWidget(QLabel("Hotkey:"))
        self.t_hotkey = HotkeyRow(
            "",  # no built-in default — Clear button just wipes the value
            on_commit=self._on_template_hotkey_commit,
            default_button_text="Clear",
            default_button_tooltip="Clear this template's hotkey",
        )
        self.t_hotkey.setToolTip(
            "Optional global hotkey for this template. Pressing it from "
            "anywhere selects this template and summons the overlay. "
            "Leave empty for no binding."
        )
        hk_row.addWidget(self.t_hotkey, 1)
        edit_panel.addLayout(hk_row)

        var_header = QHBoxLayout()
        var_header.addWidget(QLabel("Variables:"))
        var_header.addStretch()
        self.add_var_btn = QPushButton("+ Add variable")
        self.add_var_btn.clicked.connect(self._add_variable_via_button)
        var_header.addWidget(self.add_var_btn)
        edit_panel.addLayout(var_header)

        self.var_scroll = QScrollArea()
        self.var_scroll.setWidgetResizable(True)
        self.var_container = QWidget()
        self.var_container_layout = QVBoxLayout(self.var_container)
        self.var_container_layout.setContentsMargins(0, 0, 0, 0)
        self.var_container_layout.setSpacing(8)
        self.var_container_layout.addStretch()
        self.var_scroll.setWidget(self.var_container)
        edit_panel.addWidget(self.var_scroll, 1)

        btn_row = QHBoxLayout()
        btn_add = QPushButton("Add template")
        btn_del = QPushButton("Delete template")
        btn_apply = QPushButton("Apply to selected")
        btn_add.clicked.connect(self._add_template)
        btn_del.clicked.connect(self._delete_template)
        btn_apply.clicked.connect(self._apply_template_edits)
        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_del)
        btn_row.addWidget(btn_apply)
        edit_panel.addLayout(btn_row)

        body.addLayout(edit_panel, 1)
        layout.addLayout(body, 1)

        # Debounce timer for syncing variable cards with the text. Lives at
        # the SettingsWindow level so it persists across tab switches.
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.timeout.connect(self._sync_variables_with_text)
        self.t_text.textChanged.connect(self._on_text_changed)
        # The preview is light to recompute, so refresh on every keystroke
        # without debouncing — feels live.
        self.t_text.textChanged.connect(self._refresh_template_preview)

        return page

    def _refresh_template_preview(self) -> None:
        """Re-render the read-only preview from the current edit-panel state.

        Builds an ad-hoc :class:`Template` from the text + every variable
        card's current ``prefix``/``suffix``/``default``/``default_on``
        and renders it. We pass ``values={}`` so :meth:`Template.render`
        falls back to each variable's own ``default``; we pass an empty
        ``toggles`` so it likewise falls back to ``default_on``.

        Variables with ``default_on=False`` are dropped from the output —
        that's the whole point of the toggle, and the preview should
        reflect what the user actually gets when they Send.
        """
        if not hasattr(self, "t_preview"):
            return  # called before _build_templates_tab finished
        text = self.t_text.toPlainText()
        variables = [
            Variable(
                name=card.var_name,
                prefix=card.prefix_edit.text(),
                suffix=card.suffix_edit.text(),
                default=card.default_edit.text(),
                default_on=card.default_on_cb.isChecked(),
            )
            for card in self.var_cards.values()
        ]
        tmpl = Template(name="(preview)", text=text, variables=variables)
        try:
            rendered = tmpl.render(values={}, toggles={})
        except Exception:  # noqa: BLE001 — never let preview kill the UI
            log.exception("Template preview render failed")
            rendered = "(render failed — see logs)"
        self.t_preview.setPlainText(rendered)

    # ------------------------------------------------------------------ #
    # Loading current state into widgets
    # ------------------------------------------------------------------ #

    def _load_into_ui(self) -> None:
        # Set provider first so the model dropdown gets repopulated to that
        # provider's suggestions, *then* overlay the saved model string —
        # which may be a curated entry (selected) or a custom one (typed).
        provider = self.settings["provider"]
        idx = self.provider_combo.findText(provider)
        self.provider_combo.setCurrentIndex(max(0, idx))
        # currentTextChanged fires on setCurrentIndex above and has already
        # populated _PROVIDER_MODELS[provider]; just put the saved model
        # into the combo's edit field (matches a suggestion or stays as a
        # custom string).
        saved_model = str(self.settings.get("model") or "")
        if saved_model:
            self.model_combo.setCurrentText(saved_model)

        hk_cfg = self.settings.get("hotkeys") or {}
        for key, row in self.hotkey_rows.items():
            row.set_value(str(hk_cfg.get(key) or ""))

        log_cfg = self.settings.get("logging", {})
        self.log_enabled.setChecked(bool(log_cfg.get("enabled", True)))
        lvl_idx = self.log_level.findText(str(log_cfg.get("level", "INFO")).upper())
        self.log_level.setCurrentIndex(max(0, lvl_idx))

        self.framed_window_cb.setChecked(bool(self.settings.get("framed_window", False)))
        self.window_width.setValue(int(self.settings.get("window_width") or 540))
        self.window_height.setValue(int(self.settings.get("window_height") or 460))

        # Chat styles tab.
        cs = self.settings.get("chat_style") or {}
        family = str(cs.get("font_family") or "")
        # Saved value "" means "use system default" — pick the "(default)"
        # sentinel in the combo so it's visually obvious.
        if not family:
            family_label = "(default)"
        else:
            family_label = family
        # Honors the existing item if present, otherwise inserts as a typed entry.
        idx = self.chat_font_family.findText(family_label)
        if idx >= 0:
            self.chat_font_family.setCurrentIndex(idx)
        else:
            self.chat_font_family.setCurrentText(family_label)
        self.chat_font_size.setValue(int(cs.get("font_size") or 13))
        self.chat_turn_spacing.setValue(int(cs.get("turn_spacing") or 18))
        self.chat_autoscroll_tolerance.setValue(int(cs.get("autoscroll_tolerance", 12)))
        u_idx = self.chat_user_align.findText(str(cs.get("user_align") or "left"))
        self.chat_user_align.setCurrentIndex(max(0, u_idx))
        a_idx = self.chat_assistant_align.findText(str(cs.get("assistant_align") or "right"))
        self.chat_assistant_align.setCurrentIndex(max(0, a_idx))
        self.chat_show_labels.setChecked(bool(cs.get("show_labels", True)))

        # New background-tint controls.
        ub_idx = self.chat_user_bg_mode.findText(str(cs.get("user_bg_mode") or "never"))
        self.chat_user_bg_mode.setCurrentIndex(max(0, ub_idx))
        self.chat_user_bg_color.set_color(str(cs.get("user_bg_color") or "#4a7dff"))
        ab_idx = self.chat_assistant_bg_mode.findText(str(cs.get("assistant_bg_mode") or "never"))
        self.chat_assistant_bg_mode.setCurrentIndex(max(0, ab_idx))
        self.chat_assistant_bg_color.set_color(str(cs.get("assistant_bg_color") or "#888888"))

        mem_cfg = self.settings.get("memory", {})
        self.mem_enabled.setChecked(bool(mem_cfg.get("enabled", False)))
        backend = str(mem_cfg.get("backend", "gemini"))
        b_idx = self.mem_backend.findText(backend)
        # setCurrentIndex fires currentTextChanged, which already
        # populated _EMBED_MODELS[backend] into the combo; now overlay
        # the saved model on top (matches a suggestion or stays custom).
        self.mem_backend.setCurrentIndex(max(0, b_idx))
        saved_model = str(mem_cfg.get("model") or "")
        if saved_model:
            self.mem_model.setCurrentText(saved_model)
        self.mem_top_k.setValue(int(mem_cfg.get("top_k", 3) or 3))
        self.mem_local_offline_cb.setChecked(
            bool(mem_cfg.get("local_offline_mode", False))
        )
        # Refresh local-only row visibility based on the just-loaded backend.
        self._set_local_only_visible(self.mem_backend.currentText() == "local")
        self._refresh_mem_status()

        self.template_list.clear()
        for t in self.settings["templates"]:
            self.template_list.addItem(t["name"])
        if self.template_list.count() > 0:
            self.template_list.setCurrentRow(0)

    # ------------------------------------------------------------------ #
    # Template selection
    # ------------------------------------------------------------------ #

    def _select_template(self, row: int) -> None:
        templates = self.settings["templates"]
        if not (0 <= row < len(templates)):
            return
        t = templates[row]
        self._loading_template = True
        try:
            self.t_name.setText(t["name"])
            self.t_text.setPlainText(t["text"])
            self.t_screenshot.setChecked(t.get("include_screenshot", False))
            self.t_use_memory.setChecked(t.get("use_memory", False))
            self.t_hotkey.set_value(str(t.get("hotkey") or ""))
            self._rebuild_variable_cards(t)
        finally:
            self._loading_template = False
        # Render the preview after the loading guard releases — text +
        # cards are now consistent.
        self._refresh_template_preview()

    def _rebuild_variable_cards(self, t: dict) -> None:
        # Clear current cards
        for card in list(self.var_cards.values()):
            self.var_container_layout.removeWidget(card)
            card.deleteLater()
        self.var_cards.clear()

        # Build new cards. If the template has an explicit `variables` list,
        # use it; otherwise infer one Variable per placeholder for back-compat.
        raw_vars = t.get("variables")
        if raw_vars is None:
            raw_vars = []
            seen: set[str] = set()
            for name in VARIABLE_RE.findall(t["text"]):
                if name not in seen:
                    seen.add(name)
                    raw_vars.append({"name": name})

        for vd in raw_vars:
            self._append_card(vd)

    # ------------------------------------------------------------------ #
    # Variable card management
    # ------------------------------------------------------------------ #

    def _append_card(self, data: dict) -> None:
        card = VariableCard(data)
        card.remove_clicked.connect(self._remove_variable)
        # Any edit on a card (prefix/suffix/default/default_on) re-renders
        # the preview so the user sees the consequence immediately.
        card.changed.connect(self._refresh_template_preview)
        # Insert before the trailing stretch so cards stack at the top.
        self.var_container_layout.insertWidget(
            self.var_container_layout.count() - 1, card
        )
        self.var_cards[data["name"]] = card
        self._refresh_template_preview()

    def _remove_variable(self, name: str) -> None:
        """Remove a variable: drop its card and all {name} from the text."""
        text = self.t_text.toPlainText()
        new_text = text.replace(f"{{{name}}}", "")
        if new_text != text:
            self._loading_template = True
            try:
                self.t_text.setPlainText(new_text)
            finally:
                self._loading_template = False
        card = self.var_cards.pop(name, None)
        if card is not None:
            self.var_container_layout.removeWidget(card)
            card.deleteLater()
        log.debug("Variable removed: %s", name)
        self._refresh_template_preview()

    def _add_variable_via_button(self) -> None:
        name, ok = QInputDialog.getText(
            self, "Add variable",
            "Variable name (letters, digits, underscore):",
        )
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        if not re.fullmatch(r"\w+", name):
            QMessageBox.warning(
                self, "Invalid name",
                "Variable names must contain only letters, digits, and underscores.",
            )
            return
        if name in self.var_cards:
            QMessageBox.information(
                self, "Already exists",
                f"Variable {{{name}}} is already defined in this template.",
            )
            return
        # Insert {name} at cursor, then create the card.
        cursor = self.t_text.textCursor()
        cursor.insertText(f"{{{name}}}")
        self._append_card({"name": name})
        log.debug("Variable added via button: %s", name)

    def _on_text_changed(self) -> None:
        if self._loading_template:
            return
        self._sync_timer.start(self._SYNC_DEBOUNCE_MS)

    def _sync_variables_with_text(self) -> None:
        """Add cards for new {placeholders}; drop cards no longer in text."""
        if self._loading_template:
            return
        placeholders = set(VARIABLE_RE.findall(self.t_text.toPlainText()))
        existing = set(self.var_cards.keys())
        added = placeholders - existing
        removed = existing - placeholders
        for name in added:
            self._append_card({"name": name})
        for name in removed:
            card = self.var_cards.pop(name)
            self.var_container_layout.removeWidget(card)
            card.deleteLater()
        if added or removed:
            log.debug("Variable cards synced: +%s -%s", sorted(added), sorted(removed))

    # ------------------------------------------------------------------ #
    # Template CRUD
    # ------------------------------------------------------------------ #

    def _add_template(self) -> None:
        new = {
            "name": "New template",
            "text": "{prompt}",
            "include_screenshot": False,
            "hotkey": "",
            "variables": [
                {"name": "prompt", "prefix": "", "suffix": "",
                 "default": "", "default_on": True},
            ],
        }
        self.settings["templates"].append(new)
        self.template_list.addItem(new["name"])
        self.template_list.setCurrentRow(self.template_list.count() - 1)
        log.debug("Template added: %s", new["name"])

    def _delete_template(self) -> None:
        row = self.template_list.currentRow()
        if row < 0:
            return
        name = self.settings["templates"][row].get("name", "?")
        del self.settings["templates"][row]
        self.template_list.takeItem(row)
        log.debug("Template deleted: %s", name)

    def _apply_template_edits(self) -> None:
        row = self.template_list.currentRow()
        if row < 0:
            return
        self.settings["templates"][row] = {
            "name": self.t_name.text() or "Unnamed",
            "text": self.t_text.toPlainText(),
            "include_screenshot": self.t_screenshot.isChecked(),
            "use_memory": self.t_use_memory.isChecked(),
            "hotkey": self.t_hotkey.value(),
            "variables": [card.to_dict() for card in self._cards_in_text_order()],
        }
        self.template_list.item(row).setText(self.settings["templates"][row]["name"])

    def _cards_in_text_order(self) -> list[VariableCard]:
        """Return cards ordered by first {var} appearance in the template text."""
        text = self.t_text.toPlainText()
        ordered: list[VariableCard] = []
        seen: set[str] = set()
        for name in VARIABLE_RE.findall(text):
            if name in self.var_cards and name not in seen:
                seen.add(name)
                ordered.append(self.var_cards[name])
        # Any orphan cards (defined but not referenced) tack on at the end.
        for name, card in self.var_cards.items():
            if name not in seen:
                ordered.append(card)
        return ordered

    # ------------------------------------------------------------------ #
    # Memory section helpers
    # ------------------------------------------------------------------ #

    def _on_mem_enabled_toggled(self, on: bool) -> None:
        """Probe chromadb the instant the user ticks the master switch.

        If the package isn't importable, show an explicit dialog with the
        install command and auto-uncheck the box so the saved config
        reflects reality. This avoids the failure mode where the user
        enables memory in YAML/UI, then later loses tokens to a
        Summarize call that has nowhere to go.
        """
        if not on:
            return
        try:
            import chromadb  # noqa: F401
        except ImportError:
            QMessageBox.warning(
                self, "Long-term memory: dependencies missing",
                "Long-term memory requires the optional 'chromadb' "
                "package, which isn't installed.\n\n"
                "Install with:\n"
                "    pip install -r requirements-memory.txt\n\n"
                "Then re-open Settings and enable long-term memory again."
            )
            # Block signal to avoid recursive toggle handling.
            self.mem_enabled.blockSignals(True)
            self.mem_enabled.setChecked(False)
            self.mem_enabled.blockSignals(False)

    def _refresh_mem_status(self) -> None:
        """Show entry count from the live MemoryStore, or a hint if unavailable."""
        if self._memory is None or not self._memory.enabled:
            self.mem_status_label.setText("(memory disabled)")
            self.mem_clear_btn.setEnabled(False)
            return
        try:
            n = self._memory.count()
        except Exception:  # noqa: BLE001
            log.exception("memory.count failed in settings")
            n = 0
        self.mem_status_label.setText(f"{n} entr{'y' if n == 1 else 'ies'} on disk")
        self.mem_clear_btn.setEnabled(n > 0)

    def _test_embedding(self) -> None:
        """Build a throwaway :class:`MemoryStore` from the *current UI
        values* (not the saved file) and run :meth:`MemoryStore.verify`
        on a background thread.

        Background-thread dispatch is essential for the **local** backend
        — its first verify() triggers a ~80MB sentence-transformers
        download that, run on the Qt main thread, makes Windows mark
        the window as "Not Responding" until the download finishes.
        With this off-thread, the button stays in "Testing…" state but
        the window pumps events normally.

        Uses UI values (not saved settings) so the user can iterate on
        backend/model without Save → re-open between attempts. The live
        store in main.App is not touched.
        """
        from src.memory import MemoryStore  # local import — avoids cycle
        from src.worker import run_in_background

        if getattr(self, "_test_thread", None) is not None:
            return  # already in flight; ignore double-clicks

        cfg = {
            "enabled": True,  # force-on for the test even if master is off
            "backend": self.mem_backend.currentText(),
            "model": self.mem_model.currentText().strip(),
            "top_k": 1,
        }
        if not cfg["model"]:
            QMessageBox.warning(
                self, "Pick a model",
                "Enter or pick an embedding model first."
            )
            return

        is_local = cfg["backend"] == "local"
        self.mem_test_btn.setEnabled(False)
        # Label hints that the local path may take a while — the
        # sentence-transformers model is ~80MB on first use and the
        # download has no progress callback we can pipe into the UI.
        self.mem_test_btn.setText(
            "Downloading & testing…" if is_local else "Testing…"
        )

        def call():
            return MemoryStore(cfg).verify()

        self._test_thread, self._test_worker = run_in_background(
            self, call,
            on_finished=self._on_test_embedding_finished,
            on_failed=self._on_test_embedding_failed,
        )

    def _on_test_embedding_finished(self, result) -> None:
        self._test_thread = None
        self._test_worker = None
        self.mem_test_btn.setEnabled(True)
        self.mem_test_btn.setText("Test embedding")
        try:
            ok, message = result
        except (TypeError, ValueError):
            ok, message = False, f"Unexpected result: {result!r}"
        if ok:
            QMessageBox.information(self, "Embedding works", message)
        else:
            QMessageBox.warning(
                self, "Embedding failed",
                f"{message}\n\n"
                "Common causes:\n"
                "• Deprecated/wrong model name (try a different suggestion)\n"
                "• Missing or wrong API key for the selected backend\n"
                "• No network / firewall blocking the embeddings endpoint\n"
                "• ChromaDB dimension mismatch — use 'Clear all memory' "
                "after switching backends."
            )

    def _on_test_embedding_failed(self, message: str) -> None:
        """run_in_background's failure path — the test worker raised
        rather than returning a (bool, str) pair."""
        self._test_thread = None
        self._test_worker = None
        self.mem_test_btn.setEnabled(True)
        self.mem_test_btn.setText("Test embedding")
        QMessageBox.warning(self, "Test failed", message)

    def _handle_embedding_change(self, new_memory: dict) -> bool:
        """Block a Save that would orphan the existing ChromaDB collection.

        Switching ``backend`` or ``model`` produces vectors with a
        different shape / namespace from what's already stored —
        ChromaDB refuses to mix them in one collection, so the new
        config can neither retrieve nor save anything until the old
        collection is wiped. This used to be a silent foot-gun: save
        the new config, hit Send, get an opaque embedding-function-
        mismatch error, then figure out you need to click "Clear all
        memory" manually.

        New flow: detect the breaking change at Save time, prompt with
        an entry count + what's changing, and **clear inline** on
        confirmation. The clear runs against the *current* ``_memory``
        (whose config still matches the persisted EF), so it can open
        the collection cleanly and drop it. We only modify
        ``self.settings`` after the clear succeeds, so a cancelled or
        failed clear leaves the user exactly where they were.

        Returns ``True`` to proceed with the save (no breaking change,
        or wipe succeeded), ``False`` to abort (user cancelled, or
        wipe failed).
        """
        current = self.settings.get("memory") or {}
        backend_changed = (
            str(current.get("backend") or "") != new_memory["backend"]
        )
        model_changed = (
            str(current.get("model") or "") != new_memory["model"]
        )
        if not (backend_changed or model_changed):
            return True  # only enabled/top_k/offline_mode changed — safe

        if self._memory is None:
            return True  # no live store, nothing to lose

        # count() returns 0 if the store can't be loaded (e.g. user is
        # already in a degenerate state where the saved config doesn't
        # match the persisted EF). In that case the prompt skips and
        # the new config will eventually try to open the broken
        # collection too — that's a pre-existing condition we don't
        # try to repair from this dialog, and the user can fall back
        # to manually deleting settings/memory/ on disk.
        try:
            entries = int(self._memory.count())
        except Exception:  # noqa: BLE001
            log.exception("[settings] count() failed during embedding-change check")
            entries = 0
        if entries <= 0:
            return True

        changes: list[str] = []
        if backend_changed:
            changes.append(
                f"backend: {current.get('backend')!r} → {new_memory['backend']!r}"
            )
        if model_changed:
            changes.append(
                f"model: {current.get('model')!r} → {new_memory['model']!r}"
            )
        plural = "y" if entries == 1 else "ies"

        reply = QMessageBox.warning(
            self, "Embedding change requires memory wipe",
            "Switching embeddings produces vectors with a different "
            "shape or namespace from what's already stored — ChromaDB "
            "can't mix them in one collection, and trying would block "
            "retrieval and saves entirely.\n\n"
            "Changes:\n  • " + "\n  • ".join(changes) + "\n\n"
            f"This will permanently delete the {entries} existing "
            f"memory entr{plural} across all templates.\n\n"
            "Proceed and clear stored memory?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            log.info(
                "[settings] Embedding change cancelled by user "
                "(was %d entries) — settings not saved.", entries,
            )
            return False

        try:
            self._memory.clear()
            log.info(
                "[settings] Cleared %d memory entries for embedding switch "
                "(backend_changed=%s, model_changed=%s).",
                entries, backend_changed, model_changed,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("[settings] memory.clear failed during embedding switch")
            QMessageBox.critical(
                self, "Memory clear failed",
                "Couldn't clear the existing memory store before "
                f"applying the embedding change:\n\n{exc}\n\n"
                "Settings were not saved. Try the 'Clear all memory' "
                "button on the Memory tab first, then re-apply the "
                "change. If that also fails, close the app and delete "
                "the 'settings/memory/' directory manually.",
            )
            return False
        self._refresh_mem_status()
        return True

    def _clear_memory(self) -> None:
        if self._memory is None:
            return
        confirm = QMessageBox.question(
            self, "Clear memory",
            "Delete ALL stored memory entries across all templates?\n"
            "This can't be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self._memory.clear()
        except Exception:  # noqa: BLE001
            log.exception("memory.clear failed")
            QMessageBox.warning(self, "Clear failed",
                                "Could not clear memory (see log for details).")
        self._refresh_mem_status()

    # ------------------------------------------------------------------ #
    # Hotkey conflict detection
    # ------------------------------------------------------------------ #
    #
    # Each hotkey-bearing widget identifies itself with a "source key":
    #
    #     hotkey:<name>     — one of the rows on the Hotkeys tab
    #     template:<index>  — the per-template hotkey, indexed into
    #                          self.settings["templates"]
    #
    # The conflict check walks every source and reports those whose
    # current combo string equals the one being checked. The currently-
    # selected template's hotkey lives in the t_hotkey widget rather
    # than in self.settings (it's only flushed on _apply_template_edits),
    # so the scan special-cases that row.

    def _all_hotkey_sources(self) -> list[tuple[str, str, str]]:
        """Return (source_key, human_label, current_combo) for every
        hotkey-bearing widget / stored binding in the dialog.

        Used by both commit-time and save-time conflict checks.
        """
        out: list[tuple[str, str, str]] = []
        for key, row in self.hotkey_rows.items():
            label = self._HOTKEY_LABELS.get(key, key)
            out.append((f"hotkey:{key}", label, row.value()))

        current_row = (
            self.template_list.currentRow()
            if hasattr(self, "template_list") else -1
        )
        for i, t in enumerate(self.settings.get("templates", [])):
            tmpl_name = (t.get("name") or "(unnamed)")
            if i == current_row and hasattr(self, "t_hotkey"):
                # Reflect unsaved typing in the template editor.
                combo = self.t_hotkey.value()
                tmpl_name = self.t_name.text() or tmpl_name
            else:
                combo = str(t.get("hotkey") or "")
            out.append((f"template:{i}", f"Template → {tmpl_name}", combo))
        return out

    def _resolve_hotkey_conflict(self, source_key: str, combo: str) -> bool:
        """Commit-time gate: if ``combo`` is already bound elsewhere, ask
        the user whether to overwrite the other binding(s) or cancel.

        Returns ``True`` if the row owning ``source_key`` should adopt
        ``combo`` (no conflict, or user confirmed overwrite — in which
        case the colliding rows have already been cleared).
        Returns ``False`` if the user cancelled, leaving every row's
        existing value untouched.
        """
        combo_norm = (combo or "").strip()
        if not combo_norm:
            return True
        conflicts = [
            (src, label) for src, label, c in self._all_hotkey_sources()
            if src != source_key and c.strip() == combo_norm
        ]
        if not conflicts:
            return True

        bullet_list = "<br>".join(
            f"&nbsp;&nbsp;• {html.escape(label)}" for _, label in conflicts
        )
        plural = "s" if len(conflicts) > 1 else ""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Hotkey already in use")
        box.setText(
            f"<b>{html.escape(_prettify_combo(combo_norm))}</b> "
            f"is already bound to:<br>{bullet_list}"
        )
        box.setInformativeText(
            f"Overwrite the other binding{plural} (the conflicting row{plural} "
            "will be cleared), or cancel and keep your previous value?"
        )
        overwrite_btn = box.addButton(
            f"Overwrite", QMessageBox.ButtonRole.DestructiveRole
        )
        cancel_btn = box.addButton(
            "Cancel", QMessageBox.ButtonRole.RejectRole
        )
        box.setDefaultButton(cancel_btn)
        box.exec()
        if box.clickedButton() is not overwrite_btn:
            log.info("[hotkey] User cancelled overwrite of conflicting '%s' "
                     "from %s", combo_norm, source_key)
            return False
        self._clear_hotkey_in_sources(combo_norm, source_key)
        log.info("[hotkey] User accepted overwrite of '%s' (cleared %d "
                 "conflicting row(s))", combo_norm, len(conflicts))
        return True

    def _clear_hotkey_in_sources(self, combo_norm: str, keep_source: str) -> None:
        """Wipe ``combo_norm`` from every source EXCEPT ``keep_source``.

        Called after the user confirms an overwrite. Updates widget
        state for the Hotkeys tab + currently-selected template, and
        mutates ``self.settings["templates"]`` for the rest.
        """
        for key, row in self.hotkey_rows.items():
            if f"hotkey:{key}" == keep_source:
                continue
            if row.value().strip() == combo_norm:
                row.set_value("")
        current_row = (
            self.template_list.currentRow()
            if hasattr(self, "template_list") else -1
        )
        for i, t in enumerate(self.settings.get("templates", [])):
            if f"template:{i}" == keep_source:
                continue
            if i == current_row and hasattr(self, "t_hotkey"):
                if self.t_hotkey.value().strip() == combo_norm:
                    self.t_hotkey.set_value("")
            elif str(t.get("hotkey") or "").strip() == combo_norm:
                t["hotkey"] = ""

    def _on_template_hotkey_commit(self, combo: str) -> bool:
        """HotkeyRow.on_commit forwarder for the template editor.

        Looks up the currently-selected template's index so the conflict
        scan knows which "template:N" source to exclude. Falls through
        without a check when nothing is selected — there's no row for
        the captured combo to belong to.
        """
        idx = self.template_list.currentRow() if hasattr(self, "template_list") else -1
        if idx < 0:
            return True
        return self._resolve_hotkey_conflict(f"template:{idx}", combo)

    def _scan_save_time_conflicts(self) -> Optional[str]:
        """Save-time belt-and-suspenders: return a human-readable
        description of any duplicate combos still present, or ``None``
        if everything is unique.

        Commit-time validation should catch most conflicts, but edge
        cases slip through — clicking the Hotkeys tab's **Default**
        button can restore a combo that's now bound elsewhere, and
        templates added programmatically don't run through any gate.
        We refuse to save when duplicates exist and ask the user to
        resolve them.
        """
        # First, flush the currently-edited template so its hotkey is
        # reflected in self.settings before the scan.
        if self.template_list.currentRow() >= 0:
            self._apply_template_edits()

        by_combo: dict[str, list[str]] = {}
        for _src, label, combo in self._all_hotkey_sources():
            c = (combo or "").strip()
            if not c:
                continue
            by_combo.setdefault(c, []).append(label)
        clashes = {c: labels for c, labels in by_combo.items() if len(labels) > 1}
        if not clashes:
            return None
        lines: list[str] = []
        for combo, labels in clashes.items():
            lines.append(
                f"<b>{html.escape(_prettify_combo(combo))}</b> is bound to:<br>"
                + "<br>".join(f"&nbsp;&nbsp;• {html.escape(l)}" for l in labels)
            )
        return "<br><br>".join(lines)

    # ------------------------------------------------------------------ #
    # Save
    # ------------------------------------------------------------------ #

    def apply(self) -> bool:
        """Persist current edits to disk and tell the App to live-reload.

        Returns ``True`` on success, ``False`` if the write failed (in
        which case we leave the window open with the error already shown
        so the user can fix and retry). Shared body for :meth:`save`
        (which closes on success) and the **Apply** button (which keeps
        the window open).
        """
        log.info("[settings] Apply")
        # Capture any unsaved edits to the currently selected template.
        if self.template_list.currentRow() >= 0:
            self._apply_template_edits()

        # Save-time hotkey conflict scan. Commit-time resolution catches
        # the common case, but Default/Clear buttons and programmatic
        # template additions bypass that gate. We refuse to save while
        # any combo is bound twice so the duplicate doesn't silently
        # land in settings.yaml (where it'd cause one binding to shadow
        # the other at runtime).
        conflict_msg = self._scan_save_time_conflicts()
        if conflict_msg is not None:
            QMessageBox.warning(
                self, "Hotkey conflicts must be resolved",
                "The following hotkey combinations are bound in more than "
                "one place:<br><br>" + conflict_msg + "<br><br>"
                "Clear or reassign the duplicates and try again."
            )
            return False

        # Snapshot the new memory config from the UI; needs to happen
        # *before* we touch self.settings so the breaking-change detector
        # below can compare new vs. currently-saved.
        new_memory = {
            "enabled": self.mem_enabled.isChecked(),
            "backend": self.mem_backend.currentText(),
            "model": self.mem_model.currentText().strip(),
            "top_k": int(self.mem_top_k.value()),
            "local_offline_mode": self.mem_local_offline_cb.isChecked(),
        }
        if not self._handle_embedding_change(new_memory):
            # User cancelled the wipe confirmation, or the clear failed
            # — bail out before mutating settings.yaml so they can
            # adjust and try again.
            return False

        self.settings["provider"] = self.provider_combo.currentText()
        self.settings["model"] = self.model_combo.currentText().strip()
        self.settings["hotkeys"] = {
            key: row.value() for key, row in self.hotkey_rows.items()
        }
        self.settings["logging"] = {
            "enabled": self.log_enabled.isChecked(),
            "level": self.log_level.currentText(),
        }
        self.settings["framed_window"] = self.framed_window_cb.isChecked()
        self.settings["window_width"] = int(self.window_width.value())
        self.settings["window_height"] = int(self.window_height.value())
        # Pull the chat style off the new tab. The combo's "(default)"
        # sentinel maps back to an empty string so the renderer falls
        # through to QTextDocument's system font.
        family_label = self.chat_font_family.currentText().strip()
        family_value = "" if family_label in ("", "(default)") else family_label
        self.settings["chat_style"] = {
            "font_family": family_value,
            "font_size": int(self.chat_font_size.value()),
            "turn_spacing": int(self.chat_turn_spacing.value()),
            "user_align": self.chat_user_align.currentText(),
            "assistant_align": self.chat_assistant_align.currentText(),
            "show_labels": self.chat_show_labels.isChecked(),
            "user_bg_mode": self.chat_user_bg_mode.currentText(),
            "user_bg_color": self.chat_user_bg_color.color(),
            "assistant_bg_mode": self.chat_assistant_bg_mode.currentText(),
            "assistant_bg_color": self.chat_assistant_bg_color.color(),
            "autoscroll_tolerance": int(self.chat_autoscroll_tolerance.value()),
        }
        self.settings["memory"] = new_memory

        try:
            save_settings(self.settings)
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to save settings")
            QMessageBox.critical(self, "Save failed", str(exc))
            return False

        self.settings_saved.emit(self.settings)
        # App's _on_settings_saved replaces its live MemoryStore with a
        # freshly-built one. Our `self._memory` reference is still
        # pointing at the previous instance — rebuild it so Clear /
        # status read the new config. (Both instances target the same
        # on-disk ChromaDB dir; this is just about embedding-fn config.)
        from src.memory import MemoryStore  # local — avoid top-level cycle
        self._memory = MemoryStore(self.settings.get("memory"))
        self._refresh_mem_status()
        return True

    def save(self) -> None:
        """Apply + close. Stays open if Apply failed so the error is visible."""
        log.info("[settings] Save")
        if self.apply():
            self.close()
