"""AI-assisted variable extraction and template proposal.

Two entry points, both built on :meth:`AIClient.extract_structured`:

* :func:`extract_variables_from_image` — *Case 1.* Given a list of variable
  names from an existing template, ask the model to propose values for each
  by inspecting the image. Returns a dict ``{name: proposed_value}``.

* :func:`propose_template_from_image` — *Case 2.* Open analysis. Ask the
  model to design a whole new template (text + variables + values +
  screenshot flag) from the image, with no prior schema. Returns a
  :class:`TemplateProposal` ready to drop into ``settings.yaml``.

Both calls run through the provider's native structured-output path
(OpenAI/Gemini ``response_format``, Anthropic forced tool use, Ollama JSON
schema). If the provider returns something invalid, the SDK raises and we
let that propagate — the caller surfaces it to the user.
"""
from __future__ import annotations

from PIL import Image
from pydantic import BaseModel, Field, create_model

from src.ai_client import AIClient
from src.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------- #
# Case 1 — fill values for an existing template's known variables
# ---------------------------------------------------------------------- #

def _schema_for_variables(variable_names: list[str]) -> type[BaseModel]:
    """Build a Pydantic model with one ``str`` field per variable name.

    The model is regenerated per call because the field set is dynamic; it
    only lives for the duration of the extraction.
    """
    fields = {
        name: (
            str,
            Field(default="",
                  description=f"Proposed value for the {{{name}}} placeholder"),
        )
        for name in variable_names
    }
    return create_model("ExtractedDefaults", **fields)


def extract_variables_from_image(
    ai: AIClient,
    image: Image.Image,
    variable_names: list[str],
    template_text: str = "",
) -> dict[str, str]:
    """Propose values for each named variable by inspecting the screenshot.

    ``template_text`` is optional context — the full template string with
    placeholders — passed to the model so it understands how each variable
    will be used.

    Returns a dict mapping variable name to proposed value (may be empty
    string for variables the model couldn't infer).
    """
    if not variable_names:
        return {}

    schema = _schema_for_variables(variable_names)
    parts = [
        "Look at the attached screenshot and propose useful values for "
        f"the following variables: "
        f"{', '.join('{' + n + '}' for n in variable_names)}.",
    ]
    if template_text:
        parts.append(
            "\nThe variables are placeholders in this prompt template "
            f"(so you understand their role):\n  {template_text}"
        )
    parts.append(
        "\nProduce concise values, suitable as direct substitutions. "
        "Use an empty string for any variable you cannot infer with confidence."
    )
    prompt = "\n".join(parts)

    log.info("[resolver] Extracting %d variable(s): %s",
             len(variable_names), variable_names)
    result = ai.extract_structured(schema, prompt, image)
    return result.model_dump()


# ---------------------------------------------------------------------- #
# Case 2 — open analyze: AI designs a whole template from the image
# ---------------------------------------------------------------------- #

class ProposedVariable(BaseModel):
    """One element in a :class:`TemplateProposal`."""

    name: str = Field(
        description="snake_case identifier, e.g. 'language' or 'error_type'. "
                    "Must match a {placeholder} in the proposal's text.",
    )
    value: str = Field(
        description="The value extracted from the screenshot for this variable.",
    )
    prefix: str = Field(
        default="",
        description="Literal text inserted BEFORE the value when the variable "
                    "is included. Use this to carry sentence context that "
                    "should disappear if the variable is toggled off "
                    "(e.g., ' Focus on ').",
    )
    suffix: str = Field(
        default="",
        description="Literal text inserted AFTER the value when included "
                    "(e.g., '.' or ': ').",
    )


class TemplateProposal(BaseModel):
    """A complete draft template produced from an image."""

    name: str = Field(
        description="Short title for this template, 3-6 words.",
    )
    text: str = Field(
        description="The prompt text, with {placeholder} markers — one per "
                    "variable, matching its `name`. The text should make "
                    "sense even if optional variables are toggled off; put "
                    "any per-variable sentence context into that variable's "
                    "prefix/suffix instead of in the text itself.",
    )
    variables: list[ProposedVariable] = Field(
        default_factory=list,
        description="One entry per {placeholder} in `text`.",
    )
    include_screenshot: bool = Field(
        default=True,
        description="True if this prompt is most useful when a screenshot "
                    "is attached at send time (i.e., it asks about visual "
                    "content). False for prompts that don't need an image.",
    )


def propose_template_from_image(
    ai: AIClient,
    image: Image.Image,
    user_hint: str = "",
) -> TemplateProposal:
    """Ask the AI to design a complete prompt template from the image.

    ``user_hint`` is an optional free-text nudge from the user ("focus on
    the error message" / "treat this as a UI to critique" / etc.).
    """
    parts = [
        "Look at the attached screenshot and design a useful prompt template "
        "that would help analyze, explain, or work with what is shown.",
        "",
        "Design rules:",
        "- Pick a small set of variables (1-4) that capture the most "
        "important facts about what's in the image.",
        "- Each variable's `prefix`/`suffix` should carry the sentence "
        "context that would disappear if the variable were toggled off. "
        "For example, prefix=' Focus on ' and suffix='.' lets toggling "
        "drop the whole ' Focus on <value>.' clause.",
        "- The `text` field must reference every variable as {name}.",
        "- Fill each variable's `value` from what you observe in the screenshot.",
        "- Set `include_screenshot` to true if the prompt genuinely benefits "
        "from sending the image at use time; false otherwise.",
    ]
    if user_hint:
        parts.append(f"\nUser hint: {user_hint}")
    prompt = "\n".join(parts)

    log.info("[resolver] Proposing template from image (hint=%r)", user_hint)
    return ai.extract_structured(TemplateProposal, prompt, image)
