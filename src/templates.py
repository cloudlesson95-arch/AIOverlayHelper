"""Prompt template parsing and rendering.

A template is a string with ``{variable}`` placeholders plus a list of
:class:`Variable` configurations. Each variable carries optional
``prefix``/``suffix`` text, a default value, and a default toggle state.

Rendering:

* If the variable is **on**, ``{name}`` in the text is replaced with
  ``prefix + value + suffix``.
* If the variable is **off**, ``{name}`` is replaced with the empty
  string. This is how a user can drop ", Focus on {focus}." by toggling
  ``focus`` off — they put " Focus on " in prefix and "." in suffix, and
  ``text`` only carries the ``{focus}`` marker.

Backward compatibility: templates loaded without a ``variables`` block
get inferred :class:`Variable` objects (empty prefix/suffix/default,
``default_on=True``) for every placeholder in the text.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

from src.logger import get_logger

log = get_logger(__name__)

VARIABLE_RE = re.compile(r"\{(\w+)\}")


@dataclass
class Variable:
    """Per-variable rendering config.

    - ``prefix`` / ``suffix``: literal text inserted around the value when
      the variable is on. Use these to carry sentence context that should
      disappear when the variable is toggled off.
    - ``default``: pre-filled into the overlay's input field.
    - ``default_on``: initial toggle state in the overlay.
    """

    name: str
    prefix: str = ""
    suffix: str = ""
    default: str = ""
    default_on: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "prefix": self.prefix,
            "suffix": self.suffix,
            "default": self.default,
            "default_on": self.default_on,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Variable":
        return cls(
            name=d["name"],
            prefix=d.get("prefix", ""),
            suffix=d.get("suffix", ""),
            default=d.get("default", ""),
            default_on=bool(d.get("default_on", True)),
        )


@dataclass
class Template:
    name: str
    text: str
    include_screenshot: bool = False
    use_memory: bool = False
    # Optional per-template global hotkey (Qt QKeySequence format, e.g.
    # "Ctrl+Alt+1"). When set, pressing the combo anywhere selects this
    # template and summons the overlay. Empty string = no binding.
    # Set in Settings → Templates, not in the Hotkeys tab.
    hotkey: str = ""
    variables: list[Variable] = field(default_factory=list)

    @property
    def placeholder_names(self) -> list[str]:
        """Variable names that appear as ``{name}`` in ``text``, in order."""
        seen: set[str] = set()
        result: list[str] = []
        for m in VARIABLE_RE.findall(self.text):
            if m not in seen:
                seen.add(m)
                result.append(m)
        return result

    def variable(self, name: str) -> Variable:
        """Look up a variable's config; falls back to defaults if absent."""
        for v in self.variables:
            if v.name == name:
                return v
        return Variable(name=name)

    def render(self, values: dict[str, str], toggles: dict[str, bool]) -> str:
        """Build the final prompt.

        ``values``: per-variable user input (string).
        ``toggles``: per-variable on/off; missing entries fall back to the
        variable's ``default_on``.
        """
        result = self.text
        for var_name in self.placeholder_names:
            var = self.variable(var_name)
            is_on = toggles.get(var_name, var.default_on)
            if is_on:
                value = values.get(var_name, var.default)
                replacement = f"{var.prefix}{value}{var.suffix}"
            else:
                replacement = ""
            result = result.replace(f"{{{var_name}}}", replacement)
        log.debug("Template '%s' rendered (%d chars).", self.name, len(result))
        return result

    def to_dict(self) -> dict:
        d: dict = {
            "name": self.name,
            "text": self.text,
            "include_screenshot": self.include_screenshot,
            "use_memory": self.use_memory,
        }
        if self.hotkey:
            d["hotkey"] = self.hotkey
        if self.variables:
            d["variables"] = [v.to_dict() for v in self.variables]
        return d


# ------------------------------------------------------------------ #
# Loading
# ------------------------------------------------------------------ #

def _infer_variables(text: str) -> list[Variable]:
    """Make a Variable for each ``{placeholder}`` in ``text`` (in order)."""
    seen: set[str] = set()
    out: list[Variable] = []
    for m in VARIABLE_RE.findall(text):
        if m not in seen:
            seen.add(m)
            out.append(Variable(name=m))
    return out


def template_from_dict(raw: dict) -> Template:
    """Build a Template from a raw YAML dict (handles backward compat)."""
    text = raw["text"]
    raw_vars = raw.get("variables")
    if raw_vars is None:
        variables = _infer_variables(text)
    else:
        variables = [Variable.from_dict(v) for v in raw_vars]
    return Template(
        name=raw["name"],
        text=text,
        include_screenshot=bool(raw.get("include_screenshot", False)),
        use_memory=bool(raw.get("use_memory", False)),
        hotkey=str(raw.get("hotkey") or ""),
        variables=variables,
    )


def load_templates(settings: dict) -> list[Template]:
    """Build Template objects from the raw settings dict."""
    templates = [template_from_dict(t) for t in settings.get("templates", [])]
    log.debug("Loaded %d templates: %s", len(templates), [t.name for t in templates])
    return templates
