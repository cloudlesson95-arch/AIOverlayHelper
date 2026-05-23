"""Main overlay window — frameless, always-on-top, summoned by hotkey."""
from __future__ import annotations
import html
from typing import Any, Optional

import mistune
from PIL import Image
from PyQt6.QtCore import QEvent, QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QGuiApplication,
    QImage,
    QKeySequence,
    QMouseEvent,
    QPixmap,
    QShortcut,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.ai_client import AIClient, Message, Usage, estimate_cost
from src.conversations import save_conversations
from src.hotkeys import prettify_combo
from src.logger import get_logger
from src.memory import MemoryStore, MemoryUnavailable, format_memories_for_prompt
from src.screenshot import capture_region
from src.templates import Template
from src.ui.curate_dialog import CurateDialog
from src.ui.proposal_dialog import ProposalDialog
from src.ui.region_selector import RegionSelector
from src.variable_resolver import (
    TemplateProposal,
    extract_variables_from_image,
    propose_template_from_image,
)
from src.worker import run_ai_call, run_in_background

log = get_logger(__name__)


STYLE = """
#container {
    background-color: rgba(28, 28, 32, 235);
    border-radius: 12px;
    border: 1px solid rgba(255, 255, 255, 40);
}
QLabel { color: #eaeaea; }
QLineEdit, QTextEdit, QComboBox {
    background-color: rgba(50, 50, 55, 200);
    color: #ffffff;
    border: 1px solid rgba(255,255,255,30);
    border-radius: 6px;
    padding: 6px;
}
QPushButton {
    background-color: #4a7dff;
    color: white;
    border: none;
    padding: 8px 16px;
    border-radius: 6px;
    font-weight: 600;
}
QPushButton:hover { background-color: #5a8dff; }
QPushButton#copy_btn {
    background-color: #3a3a45;
    padding: 8px 12px;
}
QPushButton#copy_btn:hover { background-color: #4a4a55; }
QPushButton#clear_btn {
    background-color: rgba(180,80,80,220);
    padding: 0;
    font-size: 11px;
    font-weight: 700;
    min-width: 20px;
}
QPushButton#clear_btn:hover { background-color: rgba(220,90,90,240); }
QLabel#thumb { border: 1px solid rgba(255,255,255,40); border-radius: 4px; }
QLabel#status_none { color: #ff7676; font-weight: 600; }
QLabel#status_ready { color: #76ff8a; font-weight: 600; }
QTextEdit#preview {
    background-color: rgba(40,40,48,180);
    color: #d8dadf;
    font-style: italic;
    border: 1px solid rgba(255,255,255,20);
}
QLineEdit:disabled {
    color: rgba(255,255,255,90);
    background-color: rgba(35,35,40,160);
}
QLabel#usage {
    color: rgba(220,220,230,140);
    font-size: 11px;
    padding: 2px 4px;
}
"""

# Built dynamically per conversation render so settings.chat_style can
# influence font/size/alignment/spacing without restart. QTextDocument
# supports a limited HTML/CSS subset (background-color, border, padding,
# margin, font-size, font-family); no border-radius / flexbox, so we
# fake "chat alignment" by floating margins instead of using a layout
# engine — the bubble that "hugs the left" just has a big right margin,
# and vice versa.

# Fallback values when settings.chat_style is missing fields.
#
# Background modes (per role, since users disagreed with both having color
# by default): "never" = no tint, "headers" = color the You/AI label
# strip only, "all" = color the whole bubble. Color is the role's hex
# accent that the renderer fades to a soft alpha (~0.2 for bubble,
# ~0.3 for label) so the picker stays simple (just hex) and the
# rendering stays readable regardless of which color the user picks.
_DEFAULT_CHAT_STYLE = {
    "font_family": "",
    "font_size": 13,
    "turn_spacing": 18,
    "user_align": "left",
    "assistant_align": "right",
    "show_labels": True,
    "user_bg_mode": "never",
    "user_bg_color": "#4a7dff",
    "assistant_bg_mode": "never",
    "assistant_bg_color": "#888888",
    # Pixel tolerance for the stick-to-bottom auto-scroll: if the scrollbar
    # value is within this many px of the maximum at re-render time we treat
    # the user as "at the bottom" and follow the stream. Larger = more
    # forgiving (auto-follow re-engages more easily); smaller = pickier.
    "autoscroll_tolerance": 12,
}

# How much horizontal room the OPPOSITE side of a bubble gets — i.e.
# how far a left-aligned bubble is from the right edge, and vice versa.
# Bigger = narrower bubbles, more "chat app" feel.
_BUBBLE_OFFSET_PX = 80


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    """Convert ``#RRGGBB`` (or ``#RGB``) to a CSS ``rgba(r,g,b,a)`` string.

    Used by the chat-style renderer so the user only has to pick a hue
    (via the Settings color picker) and the CSS automatically applies it
    at a soft alpha — bright colors don't drown out the message text.
    Falls back to a neutral gray on malformed input so a typo in
    settings.yaml can't blank the whole stylesheet.
    """
    if not hex_color:
        return f"rgba(128,128,128,{alpha})"
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return f"rgba(128,128,128,{alpha})"
    try:
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
    except ValueError:
        return f"rgba(128,128,128,{alpha})"
    return f"rgba({r},{g},{b},{alpha})"


def _build_markdown_css(style: dict) -> str:
    """Return the per-render ``<style>`` block, parameterized by config.

    Implementation notes (the hard-won kind):

    * QTextDocument's CSS parser is lenient but fragile — a single
      syntax glitch can make it skip the rest of the stylesheet. We
      keep this function paranoid: no ``/* ... */`` comments inside
      ``<style>`` (they've been observed to break the parse), no
      quotes around font-family values (same), one rule per line
      so a future bad rule can't poison good ones that follow.
    * No rule-merging. Two rules with the same selector don't merge
      — the second *replaces* the first. We collapse each class into
      a single rule containing every property it needs.
    * No descendant selectors. ``.turn-assistant p`` is silently
      dropped. Use bare element selectors (``p``, ``li``) or bare
      class selectors (``.user-text``).
    * Qt hard-codes a per-``<p>`` top/bottom block margin that no CSS
      rule overrides reliably. :func:`_ai_markdown_html` rewrites
      mistune's ``<p>`` to ``<div class='ai-p'>`` so we own spacing.
    * Labels are pinned small (cap 11px) regardless of ``font_size``
      — they're metadata, not body text.
    """
    s = {**_DEFAULT_CHAT_STYLE, **(style or {})}
    family = (s.get("font_family") or "").strip()
    size = int(s.get("font_size") or _DEFAULT_CHAT_STYLE["font_size"])
    spacing = int(s.get("turn_spacing") or _DEFAULT_CHAT_STYLE["turn_spacing"])
    u_align = s.get("user_align") or "left"
    a_align = s.get("assistant_align") or "right"
    # Label is a slight emphasis above body — scales with font_size so
    # the visual hierarchy is preserved whether the user is at 11px or 24px.
    label_size = size + 2

    # Per-role background controls (added per user request — they didn't
    # want any tint by default, but wanted both presence AND color
    # configurable). Modes: "never" / "headers" / "all".
    u_bg_mode = s.get("user_bg_mode") or "never"
    u_bg_color = s.get("user_bg_color") or "#4a7dff"
    a_bg_mode = s.get("assistant_bg_mode") or "never"
    a_bg_color = s.get("assistant_bg_color") or "#888888"

    # Unquoted font name — QTextDocument's CSS parser has been observed
    # to choke on quoted values (single OR double) and skip the rest of
    # the stylesheet. The downside: families with spaces ("Segoe UI")
    # are passed through bare; Qt accepts the literal string here.
    family_decl = f"font-family: {family}; " if family else ""
    font_rule = f"{family_decl}font-size: {size}px;"
    label_font_rule = f"{family_decl}font-size: {label_size}px;"

    def _side(align: str) -> str:
        """Per-role left/right indent. No accent border — just margin
        shifts so each role hugs its configured side of the response area."""
        off = _BUBBLE_OFFSET_PX
        if align == "right":
            return f"margin-left: {off}px; margin-right: 0;"
        return f"margin-left: 0; margin-right: {off}px;"

    def _bubble_bg(mode: str, color: str) -> str:
        """Background-color rule for the whole turn bubble (mode='all')."""
        if mode == "all":
            return f" background-color: {_hex_to_rgba(color, 0.20)};"
        return ""

    def _label_bg(mode: str, color: str) -> str:
        """Background + padding rule for the header strip (mode='headers').
        Slightly higher alpha than the bubble so a smaller colored band
        still reads."""
        if mode == "headers":
            return (f" background-color: {_hex_to_rgba(color, 0.30)};"
                    f" padding: 2px 8px;")
        return ""

    # One rule per line so any future syntax bug stays contained.
    lines = [
        "<style>",
        f"body {{ {font_rule} }}",
        f"p {{ {font_rule} margin: 4px 0; }}",
        f"li {{ {font_rule} margin: 0; padding: 0; }}",
        "ul, ol { margin: 4px 0; padding-left: 22px; }",
        f"h1, h2, h3 {{ {font_rule} margin: 6px 0 2px 0; }}",
        f"blockquote {{ {font_rule} margin: 4px 0; }}",
        (f"pre {{ background-color: rgba(20,20,24,180); padding: 8px;"
         f" margin: 4px 0; font-size: {size}px; }}"),
        (f"code {{ background-color: rgba(20,20,24,180); padding: 1px 4px;"
         f" font-size: {size}px; }}"),
        "pre code { background: none; padding: 0; }",
        (f".turn-user {{ padding: 8px 12px; margin-top: {spacing}px;"
         f" margin-bottom: {spacing}px; {_side(u_align)}"
         f"{_bubble_bg(u_bg_mode, u_bg_color)} }}"),
        (f".turn-assistant {{ padding: 8px 12px; margin-top: {spacing}px;"
         f" margin-bottom: {spacing}px; {_side(a_align)}"
         f"{_bubble_bg(a_bg_mode, a_bg_color)} }}"),
        # Label classes split by role so each can carry its own optional
        # header tint without descendant selectors (which QTextDocument
        # doesn't honor).
        (f".label-user {{ display: block; {label_font_rule}"
         f" opacity: 0.8; margin: 0 0 4px 0;"
         f"{_label_bg(u_bg_mode, u_bg_color)} }}"),
        (f".label-assistant {{ display: block; {label_font_rule}"
         f" opacity: 0.8; margin: 0 0 4px 0;"
         f"{_label_bg(a_bg_mode, a_bg_color)} }}"),
        (f".user-text {{ {font_rule} background: none; padding: 0;"
         f" margin: 0; white-space: pre-wrap; }}"),
        f".ai-p {{ {font_rule} margin: 4px 0; padding: 0; }}",
        "</style>",
    ]
    return "\n".join(lines) + "\n"


def _ai_markdown_html(content: str) -> str:
    """Render markdown for AI turns, then swap ``<p>`` → ``<div class='ai-p'>``.

    Qt's HTML importer applies a hard-coded top/bottom block margin to
    every ``<p>`` element that no CSS rule reliably overrides. The
    practical fix is to not give it a ``<p>`` in the first place — we
    rewrite mistune's output into a styled ``<div>``, which we *can*
    fully control via ``.ai-p`` in :func:`_build_markdown_css`.

    Self-closing ``<p />`` doesn't appear in mistune output, so a plain
    string-replace suffices.
    """
    raw = mistune.html(content)
    return raw.replace("<p>", "<div class='ai-p'>").replace("</p>", "</div>")


class OverlayWindow(QWidget):
    """Translucent frameless window with template picker + var inputs + response."""

    # Emitted when the user accepts a proposed template in ProposalDialog.
    # Main listens, appends to settings.yaml, reloads templates, and tells
    # the overlay to select the new one.
    template_saved_signal = pyqtSignal(dict)

    # Fallbacks if the caller doesn't pass a hotkeys dict (or one is missing
    # a key). These are also what the Settings recorder's "Default" button
    # restores to — kept in sync with config.DEFAULT_SETTINGS["hotkeys"].
    _DEFAULT_LOCAL_SHORTCUTS = {
        "send_prompt": "Ctrl+Return",
        "next_template": "Ctrl+Alt+Right",
        "prev_template": "Ctrl+Alt+Left",
    }

    def __init__(self, ai_client: AIClient, templates: list[Template],
                 initial_conversations: Optional[dict[str, list[Message]]] = None,
                 memory: Optional[MemoryStore] = None,
                 hotkeys: Optional[dict[str, str]] = None,
                 framed_window: bool = False,
                 window_size: Optional[tuple[int, int]] = None,
                 chat_style: Optional[dict] = None):
        super().__init__()
        self.ai_client = ai_client
        self.templates = templates
        self.memory = memory
        self._hotkeys: dict[str, str] = dict(hotkeys or {})
        self._framed_window: bool = bool(framed_window)
        # Initial size — applied in _setup_window. Live-resized on Save.
        self._window_size: tuple[int, int] = window_size or (540, 460)
        # Chat-rendering settings; merged with _DEFAULT_CHAT_STYLE inside
        # _build_markdown_css so a partial dict is fine.
        self._chat_style: dict = dict(chat_style or {})
        # Live QShortcut handles — replaced on update_runtime when a new
        # binding is saved in Settings.
        self._send_shortcut: Optional[QShortcut] = None
        self._next_template_shortcut: Optional[QShortcut] = None
        self._prev_template_shortcut: Optional[QShortcut] = None
        self.current_template: Optional[Template] = None
        self.var_inputs: dict[str, QLineEdit] = {}
        self.var_toggles: dict[str, QCheckBox] = {}
        self._drag_pos: Optional[QPoint] = None
        # Keep refs to in-flight worker + thread so Python doesn't GC them.
        # Streaming AI call (Send):
        self._ai_thread = None
        self._ai_worker = None
        # One-shot structured calls (Extract / Analyze):
        self._aux_thread = None
        self._aux_worker = None
        # Memory retrieval (Send path, before AI call). On the local backend
        # first invocation since app start loads ~80MB of weights — done on
        # the main thread it freezes the UI for ~1s, so we dispatch it
        # async like Test embedding does.
        self._mem_query_thread = None
        self._mem_query_worker = None
        # Memory save (Send path, after AI finishes). Fire-and-forget —
        # we don't gate UI on it.
        self._mem_save_thread = None
        self._mem_save_worker = None
        # Token usage accumulation (session = since process start).
        self._session_usage = Usage()
        self._session_cost: float = 0.0
        # True when any cost lookup since startup hit an unknown model — we
        # mark the cost figure with "~?" so the user knows it's incomplete.
        self._cost_has_unknown = False
        # Number of memories that were prepended to the most recent Send.
        # Shown in the usage strip after each call; reset per Send.
        self._last_memory_hits: int = 0
        # The user+assistant texts of the just-sent exchange, captured at
        # dispatch time so we can pass them to memory.add() on finished.
        # Saved (and cleared) only on success — partial responses don't
        # become long-term knowledge.
        self._pending_save: Optional[tuple[str, str, bool]] = None
        # Per-template conversation history. Keyed by template name; persisted
        # to settings/conversations.json on every mutation so threads survive
        # restart. Cleared by the "New thread" button.
        self._conversations: dict[str, list[Message]] = (
            initial_conversations if initial_conversations is not None else {}
        )
        # While streaming, the assistant's reply accumulates here and shows
        # at the bottom of the response area as a "live" turn. On finished
        # it gets promoted to a real Message in the conversation list.
        self._streaming_text: str = ""
        # Region selection state. Per-template slots are the default: taking
        # a shot on template A and switching to B hides the preview without
        # losing A's capture. When the "Use globally" toggle is on, captures
        # go into a single shared slot visible from every template.
        self._selector: Optional[RegionSelector] = None
        self._screenshots: dict[str, Image.Image] = {}
        self._global_screenshot: Optional[Image.Image] = None
        self._use_global_screenshot: bool = False

        self._setup_window()
        self._build_ui()
        self._setup_shortcuts()

        if templates:
            self._select_template(0)
        log.debug("OverlayWindow built with %d templates", len(templates))

    # ------------------------------------------------------------------ #
    # Setup
    # ------------------------------------------------------------------ #

    def _setup_window(self) -> None:
        self.setWindowTitle("AI Overlay")
        self._apply_window_chrome(initial=True)
        w, h = self._window_size
        self.resize(max(320, int(w)), max(240, int(h)))

    def _apply_window_chrome(self, initial: bool = False) -> None:
        """Apply window flags + translucency based on ``self._framed_window``.

        Two modes:

        * **Frameless (default)**: original always-on-top translucent popup
          with no OS chrome. Custom drag handled by our mouse events.
        * **Framed**: a normal OS window — title bar, close/maximize/
          minimize buttons, resizable, taskbar entry. Loses translucency
          (Qt can't honor it under standard chrome on Windows) and loses
          always-on-top so the user can alt-tab past it like any window.

        Called once at construction with ``initial=True`` (no need to
        ``show()`` again — the caller will), and once per Save when the
        toggle flips. On a re-apply, we preserve visibility — Qt hides
        the window when flags change, so we re-show it if it was visible.
        """
        was_visible = (not initial) and self.isVisible()
        if self._framed_window:
            self.setWindowFlags(Qt.WindowType.Window)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        else:
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        if was_visible:
            self.show()

    def _build_ui(self) -> None:
        container = QFrame(self)
        container.setObjectName("container")
        container.setStyleSheet(STYLE)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(container)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # Top row: template picker + Take screenshot button
        top = QHBoxLayout()
        top.addWidget(QLabel("Template:"))
        self.template_combo = QComboBox()
        for t in self.templates:
            self.template_combo.addItem(t.name)
        self.template_combo.currentIndexChanged.connect(self._select_template)
        top.addWidget(self.template_combo, 1)
        self.screenshot_btn = QPushButton("Take screenshot")
        self.screenshot_btn.clicked.connect(self._take_screenshot)
        top.addWidget(self.screenshot_btn)
        layout.addLayout(top)

        # Live-rendered prompt preview (read-only). The screenshot-scope
        # toggles sit stacked on the right of the preview block, vertically
        # aligned under the Take screenshot button in the top row.
        preview_row = QHBoxLayout()
        preview_row.setContentsMargins(0, 0, 0, 0)
        preview_col = QVBoxLayout()
        preview_col.setContentsMargins(0, 0, 0, 0)
        preview_col.addWidget(QLabel("Prompt preview:"))
        self.preview = QTextEdit()
        self.preview.setObjectName("preview")
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("Final prompt will appear here…")
        self.preview.setFixedHeight(96)
        preview_col.addWidget(self.preview)
        preview_row.addLayout(preview_col, 1)

        checks_col = QVBoxLayout()
        checks_col.setContentsMargins(0, 0, 0, 0)
        checks_col.setSpacing(4)
        self.global_shot_check = QCheckBox("Use globally")
        self.global_shot_check.setToolTip(
            "Off: the screenshot is tied to the template it was taken on —\n"
            "switching templates hides the preview, switching back restores it.\n"
            "On: one shared screenshot is reused across every template."
        )
        self.global_shot_check.setChecked(self._use_global_screenshot)
        self.global_shot_check.toggled.connect(self._on_global_shot_toggled)
        checks_col.addWidget(self.global_shot_check)
        self.keep_shot_check = QCheckBox("Keep attached")
        self.keep_shot_check.setToolTip(
            "On: the screenshot stays attached after Send so the next prompt\n"
            "reuses it (until you clear it or Retake). Off: each Send clears\n"
            "the screenshot. Scope follows Use globally: per-template by\n"
            "default, shared across templates when Use globally is on."
        )
        self.keep_shot_check.setChecked(True)
        checks_col.addWidget(self.keep_shot_check)
        checks_col.addStretch(1)
        preview_row.addLayout(checks_col)
        layout.addLayout(preview_row)

        # Variable inputs grid: [toggle][name][value]
        self.var_form_host = QWidget()
        self.var_form_layout = QGridLayout(self.var_form_host)
        self.var_form_layout.setContentsMargins(0, 0, 0, 0)
        self.var_form_layout.setColumnStretch(2, 1)
        layout.addWidget(self.var_form_host)

        # Screenshot status row: thumbnail + clear + status label
        shot_row = QHBoxLayout()
        self.thumb_label = QLabel()
        self.thumb_label.setObjectName("thumb")
        self.thumb_label.setFixedSize(96, 60)
        self.thumb_label.setScaledContents(False)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        shot_row.addWidget(self.thumb_label)
        self.thumb_clear_btn = QPushButton("✕")
        self.thumb_clear_btn.setObjectName("clear_btn")
        self.thumb_clear_btn.setFixedSize(22, 22)
        self.thumb_clear_btn.setToolTip("Remove screenshot")
        self.thumb_clear_btn.clicked.connect(self._clear_screenshot)
        shot_row.addWidget(self.thumb_clear_btn)
        self.screenshot_status = QLabel()
        shot_row.addWidget(self.screenshot_status, 1)
        layout.addLayout(shot_row)

        # AI actions on the screenshot. Hidden when no screenshot is attached.
        # Extract: fill the current template's variables from the image.
        # Analyze: ask the AI to design a brand-new template from the image.
        self.ai_actions_row = QWidget()
        ai_actions_layout = QHBoxLayout(self.ai_actions_row)
        ai_actions_layout.setContentsMargins(0, 0, 0, 0)
        self.extract_btn = QPushButton("Extract values")
        self.extract_btn.setObjectName("copy_btn")
        self.extract_btn.setToolTip(
            "Ask the AI to fill this template's variables from the screenshot."
        )
        self.extract_btn.clicked.connect(self._extract_values)
        ai_actions_layout.addWidget(self.extract_btn)
        self.analyze_btn = QPushButton("Analyze image…")
        self.analyze_btn.setObjectName("copy_btn")
        self.analyze_btn.setToolTip(
            "Ask the AI to design a whole new template from this screenshot."
        )
        self.analyze_btn.clicked.connect(self._analyze_image)
        ai_actions_layout.addWidget(self.analyze_btn)
        ai_actions_layout.addStretch()
        layout.addWidget(self.ai_actions_row)

        # Send / Copy / New thread row
        action_row = QHBoxLayout()
        # Label text is overwritten by _setup_shortcuts to reflect the
        # actual configured send hotkey — this is just a placeholder.
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send)
        action_row.addWidget(self.send_btn, 1)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setObjectName("copy_btn")
        self.copy_btn.setToolTip("Copy the latest response (markdown source) to the clipboard")
        self.copy_btn.clicked.connect(self._copy_reply)
        action_row.addWidget(self.copy_btn)
        self.curate_btn = QPushButton("Curate…")
        self.curate_btn.setObjectName("copy_btn")
        self.curate_btn.setToolTip(
            "Review, edit, or delete individual turns in this template's thread."
        )
        self.curate_btn.clicked.connect(self._curate_thread)
        action_row.addWidget(self.curate_btn)
        self.new_thread_btn = QPushButton("New thread")
        self.new_thread_btn.setObjectName("copy_btn")
        self.new_thread_btn.setToolTip(
            "Clear the conversation history for the current template."
        )
        self.new_thread_btn.clicked.connect(self._new_thread)
        action_row.addWidget(self.new_thread_btn)
        self.fullscreen_btn = QPushButton("⛶")
        self.fullscreen_btn.setObjectName("copy_btn")
        self.fullscreen_btn.setFixedWidth(36)
        self.fullscreen_btn.setToolTip(
            "Open the current thread in a maximized read-only viewer "
            "(handy for long responses)."
        )
        self.fullscreen_btn.clicked.connect(self._open_chat_viewer)
        action_row.addWidget(self.fullscreen_btn)
        layout.addLayout(action_row)

        # Response area
        self.response = QTextEdit()
        self.response.setReadOnly(True)
        self.response.setPlaceholderText("Response will appear here...")
        layout.addWidget(self.response, 1)
        # Stick-to-bottom plumbing. We need to know whether the user is
        # currently pinned to the bottom across TWO kinds of events:
        #   1. Document rebuilds in _render_conversation (handled inline).
        #   2. Surrounding-UI changes that resize the response widget —
        #      e.g. attaching a screenshot adds the thumb + AI-actions
        #      rows above us, shrinking our height and pushing content
        #      out of view. The scrollbar maximum jumps without any
        #      valueChanged signal, so we install a resize filter that
        #      snaps back to bottom whenever we WERE at the bottom.
        self._user_at_bottom: bool = True
        self.response.verticalScrollBar().valueChanged.connect(
            self._on_response_scrolled
        )
        self.response.installEventFilter(self)

        # Token / cost status strip at the bottom.
        self.usage_label = QLabel()
        self.usage_label.setObjectName("usage")
        self.usage_label.setToolTip(
            "Tokens used since this run started.\n"
            "Cost is estimated from model rates; unknown models show '~?'."
        )
        layout.addWidget(self.usage_label)
        self._refresh_usage_label(last=None)

        # Initialize screenshot row to the "no screenshot" state.
        self._set_screenshot(None)

    def _setup_shortcuts(self) -> None:
        """(Re)build the local QShortcuts from the current hotkey config.

        Idempotent — old shortcut objects are disposed first so calling
        this from ``update_runtime`` after a Settings save cleanly swaps
        the bindings.

        Local shortcuts only fire while the overlay window has focus —
        that's why next/prev template live here rather than as global
        pynput hooks alongside summon_overlay. Dismissing the overlay
        is handled by the global summon_overlay hotkey (which toggles)
        and the tray icon; there's no dedicated hide shortcut anymore.
        """
        for old in (
            self._send_shortcut,
            self._next_template_shortcut, self._prev_template_shortcut,
        ):
            if old is not None:
                # setEnabled(False) takes effect immediately — without
                # it, the old QShortcut stays armed until Qt's event
                # loop processes the deferred deleteLater(), which on
                # a busy main thread is long enough that the user can
                # press the old combo and see the old action fire.
                old.setEnabled(False)
                try:
                    old.activated.disconnect()
                except (TypeError, RuntimeError):
                    pass
                old.setParent(None)
                old.deleteLater()
        send_seq = self._hotkey("send_prompt")
        next_seq = self._hotkey("next_template")
        prev_seq = self._hotkey("prev_template")
        self._send_shortcut = QShortcut(QKeySequence(send_seq), self)
        self._send_shortcut.activated.connect(self.send)
        if next_seq:
            self._next_template_shortcut = QShortcut(QKeySequence(next_seq), self)
            self._next_template_shortcut.activated.connect(
                lambda: self.cycle_template(1)
            )
        else:
            self._next_template_shortcut = None
        if prev_seq:
            self._prev_template_shortcut = QShortcut(QKeySequence(prev_seq), self)
            self._prev_template_shortcut.activated.connect(
                lambda: self.cycle_template(-1)
            )
        else:
            self._prev_template_shortcut = None
        # Send button label tracks the bound combo so users always see
        # the current shortcut — never a stale hardcoded "(Ctrl+Enter)".
        # Prettified so "Return" displays as "Enter" to match the keycap.
        if hasattr(self, "send_btn"):
            self.send_btn.setText(f"Send  ({prettify_combo(send_seq)})")

    def _hotkey(self, name: str) -> str:
        """Look up a local hotkey by name, falling back to the built-in default."""
        return (
            self._hotkeys.get(name)
            or self._DEFAULT_LOCAL_SHORTCUTS.get(name, "")
        )

    # ------------------------------------------------------------------ #
    # Behavior
    # ------------------------------------------------------------------ #

    def _select_template(self, index: int) -> None:
        if not (0 <= index < len(self.templates)):
            return
        self.current_template = self.templates[index]
        log.debug("Selected template: %s (placeholders=%s)",
                  self.current_template.name,
                  self.current_template.placeholder_names)
        self._rebuild_var_inputs()
        # Refresh the screenshot row from this template's stored slot
        # (preview shows only for the template the shot was taken on).
        self._refresh_screenshot_ui()
        # Show this template's conversation (might be empty).
        self._streaming_text = ""
        self._render_conversation()

    def _rebuild_var_inputs(self) -> None:
        # Clear old widgets out of the grid
        while self.var_form_layout.count():
            item = self.var_form_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.var_inputs.clear()
        self.var_toggles.clear()
        if not self.current_template:
            self._update_preview()
            return

        # One row per placeholder in the template text. Look up Variable
        # config (prefix/suffix/default/default_on) by name.
        for row, name in enumerate(self.current_template.placeholder_names):
            var = self.current_template.variable(name)

            toggle = QCheckBox()
            toggle.setChecked(var.default_on)
            toggle.setToolTip(
                "On: include this variable in the prompt.\n"
                "Off: drop its prefix, value, and suffix entirely."
            )
            label = QLabel(f"{name}:")
            edit = QLineEdit()
            edit.setText(var.default)
            edit.setPlaceholderText(f"value for {{{name}}}")
            edit.setEnabled(var.default_on)

            toggle.toggled.connect(
                lambda on, e=edit: (e.setEnabled(on), self._update_preview())
            )
            edit.textChanged.connect(self._update_preview)

            self.var_form_layout.addWidget(toggle, row, 0)
            self.var_form_layout.addWidget(label, row, 1)
            self.var_form_layout.addWidget(edit, row, 2)

            self.var_inputs[name] = edit
            self.var_toggles[name] = toggle

        self._update_preview()
        self._refresh_extract_button()

    def _current_values_and_toggles(self) -> tuple[dict[str, str], dict[str, bool]]:
        values = {name: edit.text() for name, edit in self.var_inputs.items()}
        toggles = {name: cb.isChecked() for name, cb in self.var_toggles.items()}
        return values, toggles

    def _update_preview(self) -> None:
        if not self.current_template:
            self.preview.clear()
            return
        values, toggles = self._current_values_and_toggles()
        self.preview.setPlainText(self.current_template.render(values, toggles))

    def send(self) -> None:
        if not self.current_template:
            return
        if self._ai_thread is not None or self._mem_query_thread is not None:
            log.debug("Send ignored — previous call still in flight.")
            return
        if self._selector is not None:
            log.debug("Send ignored — region selection in progress.")
            return

        values, toggles = self._current_values_and_toggles()
        prompt = self.current_template.render(values, toggles)
        shot = self._current_screenshot()
        has_shot = shot is not None

        if (self.current_template.include_screenshot
                and not has_shot
                and not self._confirm_send_without_screenshot()):
            log.info("[send] Cancelled at screenshot-missing confirmation.")
            return

        log.info("[send] template='%s' provider=%s/%s screenshot=%s",
                 self.current_template.name,
                 self.ai_client.provider, self.ai_client.model, has_shot)
        self._dispatch_send(prompt, shot)

    def _confirm_send_without_screenshot(self) -> bool:
        """Modal warning when a 'remind to take screenshot' template has none.

        Returns True to proceed with the send, False to cancel.
        """
        box = QMessageBox(self)
        box.setWindowTitle("No screenshot attached")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("This prompt requires a screenshot.")
        box.setInformativeText("Are you sure you want to send without it?")
        send_btn = box.addButton("Send", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        return box.clickedButton() is send_btn

    # ------------------------------------------------------------------ #
    # Screenshot capture (independent of Send)
    # ------------------------------------------------------------------ #

    def _take_screenshot(self) -> None:
        if self._selector is not None:
            return
        if self._ai_thread is not None:
            log.debug("Take screenshot ignored — AI call in flight.")
            return
        self.hide()
        self._selector = RegionSelector()
        self._selector.selected.connect(self._on_region_selected)
        self._selector.cancelled.connect(self._on_region_cancelled)
        self._selector.show_selector()

    def _on_region_selected(self, rect: QRect) -> None:
        log.info("[screenshot] Region captured: %dx%d at (%d,%d)",
                 rect.width(), rect.height(), rect.x(), rect.y())
        image = capture_region(rect.x(), rect.y(), rect.width(), rect.height())
        self._selector = None
        self._set_screenshot(image)
        self.show_overlay()

    def _on_region_cancelled(self) -> None:
        log.info("[screenshot] Selection cancelled.")
        self._selector = None
        self.show_overlay()

    def _current_screenshot(self) -> Optional[Image.Image]:
        if self._use_global_screenshot:
            return self._global_screenshot
        if not self.current_template:
            return None
        return self._screenshots.get(self.current_template.name)

    def _set_screenshot(self, image: Optional[Image.Image]) -> None:
        # Route into the global slot or the per-template dict depending on
        # whichever scope the user currently has selected.
        if self._use_global_screenshot:
            self._global_screenshot = image
        else:
            key = self.current_template.name if self.current_template else ""
            if image is None:
                self._screenshots.pop(key, None)
            elif key:
                self._screenshots[key] = image
        self._refresh_screenshot_ui()

    def _on_global_shot_toggled(self, on: bool) -> None:
        # Carry whatever is currently visible into the new scope so the
        # preview doesn't blank out under the user. Going ON: lift the
        # active template's shot into the shared slot. Going OFF: drop
        # the shared shot back into the current template's slot.
        carried = self._current_screenshot()
        self._use_global_screenshot = on
        if on:
            self._global_screenshot = carried
            if self.current_template:
                self._screenshots.pop(self.current_template.name, None)
        else:
            if carried is not None and self.current_template:
                self._screenshots[self.current_template.name] = carried
            self._global_screenshot = None
        self._refresh_screenshot_ui()

    def _refresh_screenshot_ui(self) -> None:
        image = self._current_screenshot()
        if image is None:
            self.thumb_label.clear()
            self.thumb_label.hide()
            self.thumb_clear_btn.hide()
            self.screenshot_status.setObjectName("status_none")
            self.screenshot_status.setText("Screenshot is not used")
            self.screenshot_btn.setText("Take screenshot")
            self.ai_actions_row.hide()
        else:
            pixmap = self._pil_to_pixmap(image).scaled(
                self.thumb_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.thumb_label.setPixmap(pixmap)
            self.thumb_label.show()
            self.thumb_clear_btn.show()
            self.screenshot_status.setObjectName("status_ready")
            self.screenshot_status.setText(
                f"Screenshot will be sent  ({image.width}×{image.height})"
            )
            self.screenshot_btn.setText("Retake screenshot")
            self.ai_actions_row.show()
        self._refresh_extract_button()
        # Restyle: setObjectName changes don't repaint automatically.
        self.screenshot_status.style().unpolish(self.screenshot_status)
        self.screenshot_status.style().polish(self.screenshot_status)

    def _refresh_extract_button(self) -> None:
        """Show Extract only when there's a screenshot AND the template has variables."""
        has_shot = self._current_screenshot() is not None
        has_vars = bool(
            self.current_template
            and self.current_template.placeholder_names
        )
        self.extract_btn.setVisible(has_shot and has_vars)

    def _clear_screenshot(self) -> None:
        log.info("[screenshot] Cleared by user.")
        self._set_screenshot(None)

    # ------------------------------------------------------------------ #
    # AI-assisted variable extraction & template proposal
    # ------------------------------------------------------------------ #

    def _is_busy(self) -> bool:
        """True if any AI call is in flight (streaming, structured, or the
        memory-retrieve we now do async ahead of streaming). Memory *save*
        is fire-and-forget so it doesn't count — the user can compose
        their next prompt while a save runs in the background."""
        return (
            self._ai_thread is not None
            or self._aux_thread is not None
            or self._mem_query_thread is not None
        )

    def _extract_values(self) -> None:
        if self._is_busy() or self._selector is not None:
            return
        screenshot = self._current_screenshot()
        if screenshot is None or not self.current_template:
            return
        names = self.current_template.placeholder_names
        if not names:
            return

        log.info("[extract] template=%s vars=%s",
                 self.current_template.name, names)
        ai_client = self.ai_client
        template_text = self.current_template.text

        def call() -> dict[str, str]:
            return extract_variables_from_image(
                ai_client, screenshot, names, template_text=template_text,
            )

        self._begin_aux("Extracting…", self.extract_btn)
        self._aux_thread, self._aux_worker = run_in_background(
            self, call,
            on_finished=self._on_extract_result,
            on_failed=self._on_aux_error,
        )

    def _analyze_image(self) -> None:
        if self._is_busy() or self._selector is not None:
            return
        screenshot = self._current_screenshot()
        if screenshot is None:
            return

        log.info("[analyze] proposing template from screenshot")
        ai_client = self.ai_client

        def call() -> TemplateProposal:
            return propose_template_from_image(ai_client, screenshot)

        self._begin_aux("Analyzing…", self.analyze_btn)
        self._aux_thread, self._aux_worker = run_in_background(
            self, call,
            on_finished=self._on_analyze_result,
            on_failed=self._on_aux_error,
        )

    # ----- aux busy-state management -----

    def _begin_aux(self, status_text: str, active_btn: QPushButton) -> None:
        # Disable all action buttons; flag the active one with the running label.
        self.send_btn.setEnabled(False)
        self.extract_btn.setEnabled(False)
        self.analyze_btn.setEnabled(False)
        self._aux_active_btn = active_btn
        self._aux_active_btn_text = active_btn.text()
        active_btn.setText(status_text)

    def _end_aux(self) -> None:
        if getattr(self, "_aux_active_btn", None) is not None:
            self._aux_active_btn.setText(self._aux_active_btn_text)
            self._aux_active_btn = None
        self._aux_thread = None
        self._aux_worker = None
        self.send_btn.setEnabled(True)
        self.extract_btn.setEnabled(True)
        self.analyze_btn.setEnabled(True)

    # ----- result handlers -----

    def _on_extract_result(self, values: Any) -> None:
        if not isinstance(values, dict):
            self._on_aux_error(f"unexpected extract result type: {type(values).__name__}")
            return
        log.info("[extract] received %d values", len(values))
        applied = 0
        for name, raw in values.items():
            value = str(raw) if raw is not None else ""
            if name in self.var_inputs:
                self.var_inputs[name].setText(value)
                # Auto-toggle ON when we got a non-empty value (per user choice).
                if value and name in self.var_toggles:
                    self.var_toggles[name].setChecked(True)
                applied += 1
        log.debug("[extract] applied to %d/%d inputs", applied, len(values))
        self._update_preview()
        self._record_usage()
        self._end_aux()

    def _on_analyze_result(self, proposal: Any) -> None:
        if not isinstance(proposal, TemplateProposal):
            self._on_aux_error(f"unexpected analyze result type: {type(proposal).__name__}")
            return
        log.info("[analyze] proposal received: name=%r vars=%d",
                 proposal.name, len(proposal.variables))
        self._record_usage()
        self._end_aux()
        dialog = ProposalDialog(proposal, parent=self)
        dialog.saved.connect(self._on_proposal_saved)
        dialog.exec()

    def _on_proposal_saved(self, template_dict: dict) -> None:
        # App listens to this and handles persistence + reload.
        log.info("[analyze] emitting template_saved_signal for %r", template_dict.get("name"))
        self.template_saved_signal.emit(template_dict)

    def _on_aux_error(self, message: str) -> None:
        log.error("AI auxiliary call failed: %s", message)
        self._end_aux()
        QMessageBox.warning(self, "AI call failed", message)

    @staticmethod
    def _pil_to_pixmap(image: Image.Image) -> QPixmap:
        if image.mode != "RGB":
            image = image.convert("RGB")
        data = image.tobytes("raw", "RGB")
        qimg = QImage(
            data, image.width, image.height,
            image.width * 3, QImage.Format.Format_RGB888,
        )
        # Copy: QImage borrows `data`; without copy() the pixmap can outlive it.
        return QPixmap.fromImage(qimg.copy())

    def _dispatch_send(self, prompt: str, image: Optional[Image.Image]) -> None:
        """Kick off a Send: append the user turn, persist, then either
        run memory.query() on a background thread (memory active) or
        jump straight to the AI call.

        Memory retrieval is async because the local sentence-transformers
        backend loads ~80MB of weights on its first invocation since
        app start — done on the main Qt thread it freezes the window
        for ~1s. The completion is wired to :meth:`_continue_send_after_memory`
        which is responsible for the rest of the dispatch.
        """
        conv = self._current_conversation()
        conv.append(Message(role="user", content=prompt, image=image))
        # Screenshot is now baked into the user turn. Clear the live attachment
        # so the next compose doesn't re-include it — unless "Keep attached" is
        # on, in which case the user wants to reuse the same shot across sends.
        if image is not None and not self.keep_shot_check.isChecked():
            self._set_screenshot(None)
        self._persist_conversations()

        # Render immediately so the user sees their turn while memory
        # retrieves (which might take a second on first local-model use).
        self._streaming_text = ""
        self._render_conversation()
        self.send_btn.setEnabled(False)

        if not self._memory_active():
            self._last_memory_hits = 0
            self._continue_send_after_memory(prompt, image, [])
            return

        # Memory active: dispatch the query on a worker thread so the
        # UI stays responsive while sentence-transformers loads / a cloud
        # embed round-trips.
        self.send_btn.setText("Retrieving memory…")
        tmpl_name = self.current_template.name  # type: ignore[union-attr]
        memory = self.memory
        log.info("[memory] Retrieving for template '%s' (async)", tmpl_name)

        def _fetch() -> list[str]:
            assert memory is not None
            return memory.query(tmpl_name, prompt)

        # Capture prompt/image in lambdas so the continuation has them.
        # On failure we still proceed — empty memory list is a valid
        # outcome (degrade gracefully when the store breaks).
        self._mem_query_thread, self._mem_query_worker = run_in_background(
            self, _fetch,
            on_finished=lambda mems: self._continue_send_after_memory(
                prompt, image, list(mems) if mems else []),
            on_failed=lambda _msg: self._continue_send_after_memory(
                prompt, image, []),
        )

    def _continue_send_after_memory(
        self,
        prompt: str,
        image: Optional[Image.Image],
        memories: list[str],
    ) -> None:
        """Second half of the Send pipeline — runs on the Qt main thread
        after :meth:`_dispatch_send`'s memory query resolves (or
        immediately if memory wasn't active).

        Builds the outgoing message list, optionally prepending retrieved
        memories, sets up the pending-save snapshot, and finally
        dispatches the streaming AI worker.
        """
        # Clear out the in-flight memory query refs so a follow-up Send
        # isn't blocked by the busy check.
        self._mem_query_thread = None
        self._mem_query_worker = None

        conv = self._current_conversation()
        outgoing: list[Message] = list(conv)
        self._last_memory_hits = len(memories)
        if memories:
            preface = format_memories_for_prompt(memories)
            outgoing.insert(0, Message(role="user", content=preface))
            log.info("[memory] Injected %d memory entr(ies) for template '%s'",
                     len(memories),
                     self.current_template.name if self.current_template else "(?)")
        # Refresh so any retrieve-time error indicator surfaces immediately.
        self._refresh_usage_label(last=None)

        # Remember what to save on successful completion. Skipped on
        # error so partial / failed responses don't become long-term
        # lessons.
        self._pending_save = None
        if self._memory_active():
            self._pending_save = (
                prompt,
                "",  # filled in on _on_ai_finished from streaming buffer
                image is not None,
            )

        self.send_btn.setText("Streaming…")
        # Pass a copy so the worker's view of history is stable even if
        # the user starts a new thread mid-stream.
        self._ai_thread, self._ai_worker = run_ai_call(
            self, self.ai_client, outgoing,
            on_chunk=self._on_ai_chunk,
            on_finished=self._on_ai_finished,
            on_failed=self._on_ai_error,
        )

    def _memory_active(self) -> bool:
        """True when both the global memory store is enabled and the current
        template opts in via ``use_memory``."""
        if self.memory is None or not self.memory.enabled:
            return False
        if self.current_template is None:
            return False
        return bool(getattr(self.current_template, "use_memory", False))

    def _on_ai_chunk(self, piece: str) -> None:
        self._streaming_text += piece
        self._render_conversation()

    def _on_ai_finished(self) -> None:
        log.debug("Response complete (%d chars).", len(self._streaming_text))
        conv = self._current_conversation()
        assistant_text = self._streaming_text
        if assistant_text:
            conv.append(Message(role="assistant", content=assistant_text))
            self._persist_conversations()
        # Memory write — only on success, only if this template opted in,
        # and only if we have an assistant response worth keeping.
        # Dispatched async so the embed (which may have to load the local
        # model the first time) doesn't freeze the UI during what looks
        # to the user like the moment a response finished.
        if (self._pending_save is not None and assistant_text
                and self._memory_active()):
            self._dispatch_memory_save(assistant_text)
        self._pending_save = None
        self._streaming_text = ""
        self._render_conversation()
        self._record_usage()
        self._reset_send_button()

    def _dispatch_memory_save(self, assistant_text: str) -> None:
        """Fire-and-forget background save of the just-completed exchange.

        Doesn't gate the UI (button is already re-enabled before this
        returns) — the user can compose their next prompt while the
        embed/save happens in the worker thread. Errors only land in
        the log + usage-strip indicator; we don't pop a dialog because
        the user didn't explicitly invoke a memory action.
        """
        if self._pending_save is None or self.memory is None:
            return
        user_text, _, had_image = self._pending_save
        memory = self.memory
        template_name = self.current_template.name if self.current_template else ""

        def _save() -> None:
            try:
                memory.add(
                    template_name,
                    user_text=user_text,
                    assistant_text=assistant_text,
                    had_image=had_image,
                )
            except MemoryUnavailable as exc:
                log.warning("[memory] Send-path save skipped: %s", exc)
            except Exception:  # noqa: BLE001
                log.exception("memory.add raised unexpectedly")

        def _done(_: object) -> None:
            self._mem_save_thread = None
            self._mem_save_worker = None
            # Surface any error MemoryStore recorded during the save
            # (e.g. dimensionality mismatch, network for cloud embed).
            self._refresh_usage_label(last=None)

        def _failed(message: str) -> None:
            log.warning("[memory] Background save worker failed: %s", message)
            self._mem_save_thread = None
            self._mem_save_worker = None
            self._refresh_usage_label(last=None)

        self._mem_save_thread, self._mem_save_worker = run_in_background(
            self, _save,
            on_finished=_done,
            on_failed=_failed,
        )

    def _on_ai_error(self, message: str) -> None:
        log.error("AI call failed: %s", message)
        # Preserve whatever streamed in by promoting it to a turn, then
        # append a visible [Error] note in the response area.
        conv = self._current_conversation()
        if self._streaming_text:
            conv.append(Message(role="assistant", content=self._streaming_text))
            self._persist_conversations()
        # Failed / partial responses do not become memory entries.
        self._pending_save = None
        self._streaming_text = ""
        self._render_conversation(extra_html=(
            f"<p style='color:#ff9a9a'>[Error] {html.escape(message)}</p>"
        ))
        self._reset_send_button()

    def _copy_reply(self) -> None:
        """Copy the latest assistant turn (streaming or completed) to the clipboard."""
        if self._streaming_text:
            text = self._streaming_text
        else:
            conv = self._current_conversation()
            text = next(
                (m.content for m in reversed(conv) if m.role == "assistant"),
                "",
            )
        if not text:
            log.debug("Copy ignored — no assistant text yet.")
            return
        QGuiApplication.clipboard().setText(text)
        log.info("[copy] Response copied to clipboard (%d chars).", len(text))

    # ------------------------------------------------------------------ #
    # Conversation history
    # ------------------------------------------------------------------ #

    def _current_conversation(self) -> list[Message]:
        if not self.current_template:
            return []
        return self._conversations.setdefault(self.current_template.name, [])

    def _persist_conversations(self) -> None:
        """Write the whole conversations dict to disk. Cheap, JSON is small."""
        save_conversations(self._conversations)

    def _open_chat_viewer(self) -> None:
        """Pop out the current thread into a maximized read-only viewer.

        The viewer shows the same HTML the inline response area renders,
        so streaming-in-progress and `[Error]` notes are NOT included —
        only the committed turns. We deliberately don't try to mirror the
        live stream into the popup: it would race with the overlay's own
        render, and the popup is meant for *reading*, not for active use.
        """
        if not self.current_template:
            return
        title = f"Chat — {self.current_template.name}"
        viewer = ChatViewer(title, self._build_conversation_html(), parent=self)
        viewer.showMaximized()

    def _build_conversation_html(self) -> str:
        """Same renderer as the inline response area, minus the streaming
        bubble — extracted so :class:`ChatViewer` can share the markup."""
        show_labels = self._chat_style.get(
            "show_labels", _DEFAULT_CHAT_STYLE["show_labels"]
        )
        parts: list[str] = [_build_markdown_css(self._chat_style)]
        for msg in self._current_conversation():
            if msg.role == "user":
                user_html = html.escape(msg.content)
                if show_labels:
                    label = (
                        "<div class='label-user'>You · with screenshot</div>"
                        if msg.had_image
                        else "<div class='label-user'>You</div>"
                    )
                else:
                    label = ""
                parts.append(
                    f"<div class='turn-user'>{label}"
                    f"<div class='user-text'>{user_html}</div></div>"
                )
            else:
                label = "<div class='label-assistant'>AI</div>" if show_labels else ""
                parts.append(
                    "<div class='turn-assistant'>"
                    f"{label}"
                    f"{_ai_markdown_html(msg.content)}</div>"
                )
        return "".join(parts)

    def _new_thread(self) -> None:
        """Wipe the current template's conversation, with confirmation."""
        if not self.current_template:
            return
        if self._is_busy():
            log.debug("New thread ignored — call in flight.")
            return
        name = self.current_template.name
        turns = self._conversations.get(name) or []
        # Only prompt when there's something to lose — empty threads clear silently.
        if turns:
            box = QMessageBox(self)
            box.setWindowTitle("Start new thread")
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(
                f"Clear the conversation history for '{name}'?"
            )
            box.setInformativeText(
                f"This will permanently remove {len(turns)} turn(s) from this "
                "template's thread. Long-term memory (if enabled) is not "
                "affected — saved exchanges and summaries persist in ChromaDB."
            )
            clear_btn = box.addButton("Clear thread", QMessageBox.ButtonRole.DestructiveRole)
            cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(cancel_btn)
            box.exec()
            if box.clickedButton() is not clear_btn:
                log.debug("[thread] New thread cancelled by user for '%s'", name)
                return
        had = bool(turns)
        self._conversations[name] = []
        self._streaming_text = ""
        self._persist_conversations()
        log.info("[thread] Cleared conversation for '%s' (had_turns=%s)", name, had)
        self._render_conversation()

    def _curate_thread(self) -> None:
        """Open the curation dialog for the current template's thread."""
        if not self.current_template:
            return
        if self._is_busy():
            log.debug("Curate ignored — call in flight.")
            return
        turns = self._current_conversation()
        dialog = CurateDialog(
            self.current_template.name, turns,
            ai_client=self.ai_client, memory=self.memory,
            parent=self,
        )
        # Each summarization is an API call — fold its tokens into the
        # session strip just like a normal Send would.
        dialog.summary_saved.connect(self._record_usage)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            log.debug("[curate] Cancelled, no changes applied.")
            return
        new_turns = dialog.get_turns()
        self._conversations[self.current_template.name] = new_turns
        self._streaming_text = ""
        self._persist_conversations()
        log.info("[curate] Applied changes (%d turns now)", len(new_turns))
        self._render_conversation()

    def _autoscroll_tolerance(self) -> int:
        return int(
            self._chat_style.get(
                "autoscroll_tolerance",
                _DEFAULT_CHAT_STYLE["autoscroll_tolerance"],
            )
        )

    def _on_response_scrolled(self, value: int) -> None:
        # Track whether the user is pinned to the bottom so resize handlers
        # (e.g. after a screenshot row appears) know whether to snap back.
        sb = self.response.verticalScrollBar()
        self._user_at_bottom = value >= sb.maximum() - self._autoscroll_tolerance()

    def _scroll_response_to_bottom(self) -> None:
        cursor = self.response.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.response.setTextCursor(cursor)
        self.response.ensureCursorVisible()

    def eventFilter(self, obj, event):  # type: ignore[override]
        # When the response widget shrinks/grows because surrounding rows
        # appeared or disappeared (screenshot thumbnail, AI-action buttons,
        # variable form changes), the scrollbar maximum jumps without any
        # valueChanged. If we were at the bottom before the resize, snap
        # back so the latest turn doesn't slide out of view. Defer one
        # event loop tick so the layout has finished settling.
        if obj is self.response and event.type() == QEvent.Type.Resize:
            if self._user_at_bottom:
                QTimer.singleShot(0, self._scroll_response_to_bottom)
        return super().eventFilter(obj, event)

    def _render_conversation(self, extra_html: str = "") -> None:
        """Re-render the response area with all turns + any live streaming text."""
        # Stick-to-bottom: capture whether the user was already at the bottom
        # BEFORE rebuilding the document, so streaming chunks don't steal the
        # scrollbar from a user who has scrolled up to re-read an earlier turn.
        # We also snapshot the prior scroll value because setHtml resets the
        # scrollbar to 0 — without restoring it, a scrolled-up user would get
        # snapped back to the top instead of staying where they were.
        sb = self.response.verticalScrollBar()
        tolerance = self._autoscroll_tolerance()
        prev_value = sb.value()
        # OR with the tracked flag so we honor stick-to-bottom even when the
        # widget just resized (current sb.value() may sit far below max
        # because the new maximum is larger than the user has yet scrolled to).
        was_at_bottom = self._user_at_bottom or prev_value >= sb.maximum() - tolerance

        show_labels = self._chat_style.get(
            "show_labels", _DEFAULT_CHAT_STYLE["show_labels"]
        )
        parts: list[str] = [_build_markdown_css(self._chat_style)]
        for msg in self._current_conversation():
            if msg.role == "user":
                user_html = html.escape(msg.content)
                if show_labels:
                    label = (
                        "<div class='label-user'>You · with screenshot</div>"
                        if msg.had_image
                        else "<div class='label-user'>You</div>"
                    )
                else:
                    label = ""
                parts.append(
                    f"<div class='turn-user'>{label}"
                    f"<div class='user-text'>{user_html}</div></div>"
                )
            else:
                label = "<div class='label-assistant'>AI</div>" if show_labels else ""
                parts.append(
                    "<div class='turn-assistant'>"
                    f"{label}"
                    f"{_ai_markdown_html(msg.content)}</div>"
                )
        if self._streaming_text:
            # Show the streaming response as plain text in an assistant block.
            stream_html = html.escape(self._streaming_text)
            stream_label = (
                "<div class='label-assistant'>AI · streaming…</div>"
                if show_labels else ""
            )
            parts.append(
                "<div class='turn-assistant'>"
                f"{stream_label}"
                f"<div class='user-text'>{stream_html}</div></div>"
            )
        if extra_html:
            parts.append(extra_html)
        self.response.setHtml("".join(parts))
        # Only follow the stream if the user was already pinned to the bottom.
        # If they had scrolled up, restore their prior scroll value so the
        # rebuild doesn't snap them to the top (setHtml resets to 0).
        if was_at_bottom:
            self._scroll_response_to_bottom()
        else:
            sb.setValue(min(prev_value, sb.maximum()))

    # ------------------------------------------------------------------ #
    # Token usage / cost tracking
    # ------------------------------------------------------------------ #

    def _record_usage(self) -> None:
        """Pull AIClient.last_usage into the session totals and refresh the strip."""
        last = self.ai_client.last_usage
        if last is None:
            log.debug("No usage info from last call.")
            self._refresh_usage_label(last=None)
            return
        self._session_usage = self._session_usage.add(last)
        cost = estimate_cost(self.ai_client.provider, self.ai_client.model, last)
        if cost is None:
            self._cost_has_unknown = True
        else:
            self._session_cost += cost
        log.debug("Usage recorded: last=%d/%d session=%d/%d cost+=%s",
                  last.input_tokens, last.output_tokens,
                  self._session_usage.input_tokens, self._session_usage.output_tokens,
                  cost)
        self._refresh_usage_label(last=last)

    def _refresh_usage_label(self, last: Optional[Usage]) -> None:
        sess = self._session_usage
        parts = []
        if last is not None:
            parts.append(f"Last: {last.input_tokens:,}↑ {last.output_tokens:,}↓")
        if self._last_memory_hits:
            parts.append(f"🔍 {self._last_memory_hits} memories")
        # Surface any pending memory failure inline. Previously these only
        # hit the logs; the user reasonably complained that memory was
        # silently broken with no app-level signal. Full error goes into
        # the tooltip so the strip itself stays compact.
        mem_err = getattr(self.memory, "last_error", None) if self.memory else None
        if mem_err:
            parts.append("⚠ memory error")
        parts.append(
            f"Session: {sess.input_tokens:,}↑ {sess.output_tokens:,}↓"
        )
        cost_str = f"${self._session_cost:.4f}"
        if self._cost_has_unknown:
            cost_str += " (~?)"
        parts.append(f"~{cost_str}")
        self.usage_label.setText("   ·   ".join(parts))
        if mem_err:
            self.usage_label.setToolTip(
                f"Long-term memory error: {mem_err}\n\n"
                "Open Settings → Memory and click 'Test embedding' to "
                "diagnose. Common causes: wrong/deprecated embedding "
                "model, missing API key, or chromadb dimension mismatch "
                "after switching backends."
            )
        else:
            # Restore the default tooltip from _build_ui.
            self.usage_label.setToolTip(
                "Tokens used since this run started.\n"
                "Cost is estimated from model rates; unknown models show '~?'."
            )

    def _reset_send_button(self) -> None:
        self.send_btn.setEnabled(True)
        self.send_btn.setText(f"Send  ({prettify_combo(self._hotkey('send_prompt'))})")
        self._ai_thread = None
        self._ai_worker = None

    # ------------------------------------------------------------------ #
    # Window dragging (frameless windows don't drag for free)
    # ------------------------------------------------------------------ #

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_pos = None
        event.accept()

    # ------------------------------------------------------------------ #
    # Public methods called by App
    # ------------------------------------------------------------------ #

    def show_overlay(self) -> None:
        """Summon the window and bring it to the foreground."""
        log.info("[overlay] Show overlay")
        self.show()
        self.raise_()
        self.activateWindow()

    def cycle_template(self, delta: int) -> None:
        """Move template selection by ``delta`` slots and wrap around.

        ``delta=+1`` → next, ``delta=-1`` → previous. Used by the local
        Next/Previous template shortcuts. No-op when there are no
        templates loaded. Setting the combo's index emits
        currentIndexChanged → _select_template, which rebuilds the
        variable form and refreshes the preview.
        """
        if not self.templates:
            return
        count = len(self.templates)
        cur = self.template_combo.currentIndex()
        if cur < 0:
            cur = 0
        new_idx = (cur + delta) % count
        if new_idx == cur:
            return
        log.info("[template] Cycle %s → index %d (%s)",
                 "next" if delta > 0 else "prev",
                 new_idx, self.templates[new_idx].name)
        self.template_combo.setCurrentIndex(new_idx)

    def select_template_by_name(self, name: str) -> None:
        """Select the template with the given name and summon the overlay.

        Used by per-template global hotkeys. Names not present in the
        currently-loaded list are logged and ignored — the user may have
        deleted the template since registering the hotkey.
        """
        for i, t in enumerate(self.templates):
            if t.name == name:
                self.template_combo.setCurrentIndex(i)
                self.show_overlay()
                return
        log.warning("[template] Hotkey targeted missing template: %r", name)

    def update_runtime(
        self,
        ai_client: AIClient,
        templates: list[Template],
        select_name: Optional[str] = None,
        memory: Optional[MemoryStore] = None,
        hotkeys: Optional[dict[str, str]] = None,
        framed_window: Optional[bool] = None,
        window_size: Optional[tuple[int, int]] = None,
        chat_style: Optional[dict] = None,
    ) -> None:
        """Live-reload after settings change.

        If ``select_name`` is given and matches a template, that one is
        focused after the reload (used when a new template is added via
        Analyze image…).  ``memory`` lets the App swap in a freshly-built
        store after a Settings save (e.g. user changed embedding backend).
        ``hotkeys`` replaces the local Esc/Ctrl+Return bindings; the
        Send button label is refreshed from the new send hotkey.
        """
        log.debug("Overlay runtime update: %d templates (select=%r, memory=%s, hotkeys=%s)",
                  len(templates), select_name, memory is not None,
                  hotkeys is not None)
        self.ai_client = ai_client
        self.templates = templates
        if memory is not None:
            self.memory = memory
        if hotkeys is not None:
            self._hotkeys = dict(hotkeys)
            self._setup_shortcuts()
        if framed_window is not None and bool(framed_window) != self._framed_window:
            self._framed_window = bool(framed_window)
            self._apply_window_chrome()
        if window_size is not None:
            w, h = window_size
            new_size = (max(320, int(w)), max(240, int(h)))
            if new_size != self._window_size:
                self._window_size = new_size
                self.resize(*new_size)
        if chat_style is not None:
            self._chat_style = dict(chat_style)
            # Re-render the response area so font/alignment changes are
            # visible without waiting for the next turn.
            self._render_conversation()
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        for t in templates:
            self.template_combo.addItem(t.name)
        self.template_combo.blockSignals(False)
        if templates:
            target = 0
            if select_name:
                for i, t in enumerate(templates):
                    if t.name == select_name:
                        target = i
                        break
            self.template_combo.setCurrentIndex(target)
            self._select_template(target)


# ---------------------------------------------------------------------- #
# Chat fullscreen viewer
# ---------------------------------------------------------------------- #

class ChatViewer(QDialog):
    """Maximized read-only window for reading a long thread comfortably.

    Snapshot semantics: the HTML is captured when the dialog opens. New
    turns that arrive while it's open don't update the popup. That's a
    deliberate trade — keeping it live would mean re-rendering on every
    streamed chunk while the user is mid-read, which is jarring.
    """

    def __init__(self, title: str, conversation_html: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        # Standard window chrome: title bar, close, maximize, resize. We
        # want this regardless of the overlay's frameless/framed mode —
        # the viewer is its own thing.
        self.setWindowFlags(Qt.WindowType.Window)
        self.resize(900, 700)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        viewer = QTextEdit()
        viewer.setReadOnly(True)
        viewer.setHtml(conversation_html)
        layout.addWidget(viewer, 1)

        bottom = QHBoxLayout()
        bottom.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        layout.addLayout(bottom)

        QShortcut(QKeySequence("Esc"), self, activated=self.accept)
