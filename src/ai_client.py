"""Provider-agnostic AI client.

Add a new provider by writing a ``_stream_<name>()`` generator and adding
a branch in :meth:`AIClient.stream`. The rest of the app never has to
change. :meth:`AIClient.send` is just a wrapper that joins all chunks
— use it from synchronous callers (CLI, tests); the Qt overlay uses
``stream()`` so it can paint chunks as they arrive.
"""
from __future__ import annotations
import base64
import io
import os
from dataclasses import dataclass
from typing import Iterator, Literal, Optional, TypeVar

from PIL import Image
from pydantic import BaseModel

from src.logger import get_logger

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------- #
# Chat message
# ---------------------------------------------------------------------- #

@dataclass
class Message:
    """One turn in a conversation.

    ``image`` is only meaningful on user messages — assistant turns are
    text-only. When an assistant response in history references "the
    screenshot", the image still lives on the original user turn, and
    we re-send the full history (including any images) on every API
    call so the model continues to see them.

    ``had_image`` is a UI-display marker. It's True whenever this turn
    ever carried an image — set automatically when ``image`` is provided,
    and preserved across persistence even though raw image bytes aren't
    saved to disk. The overlay uses it to keep showing "with screenshot"
    on reloaded turns.
    """
    role: Literal["user", "assistant"]
    content: str
    image: Optional[Image.Image] = None
    had_image: bool = False

    def __post_init__(self) -> None:
        if self.image is not None and not self.had_image:
            self.had_image = True


# ---------------------------------------------------------------------- #
# Token usage + cost estimation
# ---------------------------------------------------------------------- #

@dataclass
class Usage:
    """Per-call token counts."""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens,
                     self.output_tokens + other.output_tokens)


# Per-1M-token prices in USD (input, output). Keys match the provider's
# model identifier as the user would type it into settings. Unknown models
# return ``None`` from :func:`estimate_cost` so the UI hides the dollar
# value rather than guessing.
RATES: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    # Anthropic — Claude 4 family
    "claude-haiku-4-5": (0.80, 4.00),
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-7": (15.00, 75.00),
    # Gemini
    "gemini-2.5-flash": (0.075, 0.30),
    "gemini-2.5-pro": (1.25, 5.00),
    "gemini-2.0-flash": (0.075, 0.30),
}


def estimate_cost(provider: str, model: str, usage: Usage) -> Optional[float]:
    """Return USD cost for a call, or None when we don't know the rate.

    Local providers (ollama) always return 0.0.
    """
    if provider == "ollama":
        return 0.0
    rate = RATES.get(model)
    if rate is None:
        return None
    in_rate, out_rate = rate
    return (usage.input_tokens * in_rate
            + usage.output_tokens * out_rate) / 1_000_000

log = get_logger(__name__)


class AIClient:
    """One-shot prompt-and-response client. No state, no history."""

    def __init__(self, provider: str, model: str):
        self.provider = provider
        self.model = model
        # Populated by stream() / extract_structured() when the provider
        # returns usage info. The overlay reads this after each completion
        # to update the session total. None if the provider didn't return
        # usage (rare; some Ollama models, some Gemini configurations).
        self.last_usage: Optional[Usage] = None
        log.debug("AIClient init: provider=%s model=%s", provider, model)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def stream(self, messages: list[Message]) -> Iterator[str]:
        """Yield text chunks for a multi-turn conversation.

        ``messages`` is the full conversation history including the new
        user turn. Images attached to user messages are passed through.
        """
        if not messages:
            raise ValueError("stream() requires at least one message")
        latest = messages[-1]
        log.info("Streaming to %s/%s (%d turns, latest=%d chars, image=%s)",
                 self.provider, self.model, len(messages),
                 len(latest.content), latest.image is not None)
        log.debug("Latest preview: %s", latest.content[:200])
        self.last_usage = None
        if self.provider == "openai":
            yield from self._stream_openai(messages)
        elif self.provider == "anthropic":
            yield from self._stream_anthropic(messages)
        elif self.provider == "ollama":
            yield from self._stream_ollama(messages)
        elif self.provider == "gemini":
            yield from self._stream_gemini(messages)
        else:
            raise ValueError(f"Unknown provider: {self.provider!r}")
        if self.last_usage is not None:
            log.debug("Usage: in=%d out=%d",
                      self.last_usage.input_tokens, self.last_usage.output_tokens)

    def send(self, prompt: str, image: Optional[Image.Image] = None) -> str:
        """Synchronous one-shot helper: a single-message stream, collected."""
        messages = [Message(role="user", content=prompt, image=image)]
        return "".join(self.stream(messages))

    def extract_structured(self, schema: type[T], prompt: str,
                           image: Optional[Image.Image] = None) -> T:
        """Return a validated ``schema`` instance via the provider's structured-output API.

        Each provider has a native structured-output path; we use it directly
        rather than prompt-then-parse-JSON. If the provider returns something
        that doesn't match the schema, the underlying SDK raises — we let that
        propagate so the caller sees the real error.
        """
        log.info("Structured extract on %s/%s (schema=%s prompt=%d chars image=%s)",
                 self.provider, self.model, schema.__name__,
                 len(prompt), image is not None)
        self.last_usage = None
        if self.provider == "openai":
            result = self._extract_openai(schema, prompt, image)
        elif self.provider == "anthropic":
            result = self._extract_anthropic(schema, prompt, image)
        elif self.provider == "ollama":
            result = self._extract_ollama(schema, prompt, image)
        elif self.provider == "gemini":
            result = self._extract_gemini(schema, prompt, image)
        else:
            raise ValueError(f"Unknown provider: {self.provider!r}")
        if self.last_usage is not None:
            log.debug("Usage: in=%d out=%d",
                      self.last_usage.input_tokens, self.last_usage.output_tokens)
        return result

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _image_to_b64(image: Image.Image, fmt: str = "PNG") -> str:
        buf = io.BytesIO()
        image.save(buf, format=fmt)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _openai_compatible_content(self, prompt: str,
                                   image: Optional[Image.Image]) -> list[dict]:
        """Build a single user-message ``content`` list for OpenAI-shape APIs."""
        content: list[dict] = [{"type": "text", "text": prompt}]
        if image is not None:
            b64 = self._image_to_b64(image)
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
        return content

    def _to_openai_messages(self, messages: list[Message]) -> list[dict]:
        """Convert internal Message list to OpenAI/Gemini chat-completions shape."""
        api: list[dict] = []
        for m in messages:
            if m.role == "user":
                api.append({"role": "user",
                            "content": self._openai_compatible_content(m.content, m.image)})
            else:
                api.append({"role": "assistant", "content": m.content})
        return api

    def _to_anthropic_messages(self, messages: list[Message]) -> list[dict]:
        """Convert internal Message list to Anthropic messages shape."""
        api: list[dict] = []
        for m in messages:
            if m.role == "user":
                content: list[dict] = []
                if m.image is not None:
                    b64 = self._image_to_b64(m.image)
                    content.append({
                        "type": "image",
                        "source": {"type": "base64",
                                   "media_type": "image/png",
                                   "data": b64},
                    })
                content.append({"type": "text", "text": m.content})
                api.append({"role": "user", "content": content})
            else:
                api.append({"role": "assistant", "content": m.content})
        return api

    def _to_ollama_messages(self, messages: list[Message]) -> list[dict]:
        """Convert internal Message list to Ollama chat shape."""
        api: list[dict] = []
        for m in messages:
            msg: dict = {"role": m.role, "content": m.content}
            if m.role == "user" and m.image is not None:
                msg["images"] = [self._image_to_b64(m.image)]
            api.append(msg)
        return api

    # ------------------------------------------------------------------ #
    # Providers — streaming
    # ------------------------------------------------------------------ #

    def _stream_openai(self, messages: list[Message]) -> Iterator[str]:
        from openai import OpenAI

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set in environment.")
        client = OpenAI(api_key=api_key)

        log.debug("OpenAI stream: model=%s turns=%d", self.model, len(messages))
        stream = client.chat.completions.create(
            model=self.model,
            messages=self._to_openai_messages(messages),
            stream=True,
            stream_options={"include_usage": True},
        )
        for chunk in stream:
            if getattr(chunk, "usage", None):
                self.last_usage = Usage(
                    input_tokens=chunk.usage.prompt_tokens or 0,
                    output_tokens=chunk.usage.completion_tokens or 0,
                )
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    def _stream_gemini(self, messages: list[Message]) -> Iterator[str]:
        from openai import OpenAI

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set in environment.")

        client = OpenAI(
            api_key=api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )

        log.debug("Gemini stream: model=%s turns=%d", self.model, len(messages))
        stream = client.chat.completions.create(
            model=self.model,
            messages=self._to_openai_messages(messages),
            stream=True,
            stream_options={"include_usage": True},
        )
        for chunk in stream:
            if getattr(chunk, "usage", None):
                self.last_usage = Usage(
                    input_tokens=chunk.usage.prompt_tokens or 0,
                    output_tokens=chunk.usage.completion_tokens or 0,
                )
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    def _stream_anthropic(self, messages: list[Message]) -> Iterator[str]:
        from anthropic import Anthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in environment.")
        client = Anthropic(api_key=api_key)

        log.debug("Anthropic stream: model=%s turns=%d", self.model, len(messages))
        with client.messages.stream(
            model=self.model,
            max_tokens=2048,
            messages=self._to_anthropic_messages(messages),
        ) as stream:
            for text in stream.text_stream:
                if text:
                    yield text
            final = stream.get_final_message()
            if getattr(final, "usage", None):
                self.last_usage = Usage(
                    input_tokens=getattr(final.usage, "input_tokens", 0) or 0,
                    output_tokens=getattr(final.usage, "output_tokens", 0) or 0,
                )

    def _stream_ollama(self, messages: list[Message]) -> Iterator[str]:
        import ollama  # pip install ollama

        log.debug("Ollama stream: model=%s turns=%d", self.model, len(messages))
        for chunk in ollama.chat(
            model=self.model,
            messages=self._to_ollama_messages(messages),
            stream=True,
        ):
            piece = chunk.get("message", {}).get("content", "")
            if piece:
                yield piece
            if chunk.get("done"):
                self.last_usage = Usage(
                    input_tokens=int(chunk.get("prompt_eval_count") or 0),
                    output_tokens=int(chunk.get("eval_count") or 0),
                )

    # ------------------------------------------------------------------ #
    # Providers — structured extraction
    # ------------------------------------------------------------------ #

    def _extract_openai(self, schema: type[T], prompt: str,
                        image: Optional[Image.Image]) -> T:
        from openai import OpenAI

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set in environment.")
        client = OpenAI(api_key=api_key)

        log.debug("OpenAI structured: model=%s schema=%s", self.model, schema.__name__)
        response = client.beta.chat.completions.parse(
            model=self.model,
            messages=[{"role": "user",
                       "content": self._openai_compatible_content(prompt, image)}],
            response_format=schema,
        )
        if getattr(response, "usage", None):
            self.last_usage = Usage(
                input_tokens=response.usage.prompt_tokens or 0,
                output_tokens=response.usage.completion_tokens or 0,
            )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError("OpenAI returned no parsed payload "
                               "(refusal or schema mismatch).")
        return parsed

    def _extract_gemini(self, schema: type[T], prompt: str,
                        image: Optional[Image.Image]) -> T:
        from openai import OpenAI

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set in environment.")
        client = OpenAI(
            api_key=api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )

        log.debug("Gemini structured: model=%s schema=%s", self.model, schema.__name__)
        response = client.beta.chat.completions.parse(
            model=self.model,
            messages=[{"role": "user",
                       "content": self._openai_compatible_content(prompt, image)}],
            response_format=schema,
        )
        if getattr(response, "usage", None):
            self.last_usage = Usage(
                input_tokens=response.usage.prompt_tokens or 0,
                output_tokens=response.usage.completion_tokens or 0,
            )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError("Gemini returned no parsed payload "
                               "(refusal or schema mismatch).")
        return parsed

    def _extract_anthropic(self, schema: type[T], prompt: str,
                           image: Optional[Image.Image]) -> T:
        from anthropic import Anthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in environment.")
        client = Anthropic(api_key=api_key)

        content: list[dict] = []
        if image is not None:
            b64 = self._image_to_b64(image)
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": b64,
                },
            })
        content.append({"type": "text", "text": prompt})

        tool_name = "submit"
        json_schema = schema.model_json_schema()
        log.debug("Anthropic structured: model=%s schema=%s", self.model, schema.__name__)
        response = client.messages.create(
            model=self.model,
            max_tokens=2048,
            tools=[{
                "name": tool_name,
                "description": f"Submit the extracted {schema.__name__}.",
                "input_schema": json_schema,
            }],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": content}],
        )
        if getattr(response, "usage", None):
            self.last_usage = Usage(
                input_tokens=getattr(response.usage, "input_tokens", 0) or 0,
                output_tokens=getattr(response.usage, "output_tokens", 0) or 0,
            )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                return schema.model_validate(block.input)
        raise RuntimeError("Anthropic returned no tool_use block "
                           "for the forced extraction tool.")

    def _extract_ollama(self, schema: type[T], prompt: str,
                        image: Optional[Image.Image]) -> T:
        import ollama  # pip install ollama (>= 0.4 for JSON-schema `format`)

        msg: dict = {"role": "user", "content": prompt}
        if image is not None:
            msg["images"] = [self._image_to_b64(image)]

        log.debug("Ollama structured: model=%s schema=%s", self.model, schema.__name__)
        response = ollama.chat(
            model=self.model,
            messages=[msg],
            format=schema.model_json_schema(),
        )
        self.last_usage = Usage(
            input_tokens=int(response.get("prompt_eval_count") or 0),
            output_tokens=int(response.get("eval_count") or 0),
        )
        return schema.model_validate_json(response["message"]["content"])
