"""Long-term memory store for cross-session learning (RAG).

After each successful Send on a template with ``use_memory: true``, the
(user_prompt, assistant_response) pair is embedded and persisted. Before
each subsequent Send on that same template, the new user prompt is used
to retrieve the top-k most semantically similar past entries; the
overlay then prepends them as a synthetic system message to the messages
sent to the AI.

Design highlights:

* **Per-template scope.** One ChromaDB collection holds everything, with
  ``template_name`` in metadata; queries filter on it so threads from
  different templates never cross-pollinate.
* **Lazy init.** ``chromadb`` and any embedding library are only imported
  the first time a memory operation actually runs, so users who never
  enable memory don't pay the import cost (or need the packages).
* **Backend switch.** ``backend: "gemini" | "openai" | "local"`` chooses
  the embedding provider. Cloud backends reuse the user's existing
  ``GEMINI_API_KEY`` / ``OPENAI_API_KEY``. Local uses
  sentence-transformers (downloads ~80MB on first run).
* **Embedding dim is fixed per collection.** If you switch backend with
  existing data, ChromaDB will refuse the new dim. The Settings dialog
  exposes a "Clear all memory" button to handle this.

Failures are non-fatal: a missing API key, a network blip, or an
``import chromadb`` ImportError logs a warning and returns empty
results / skips the save — the user's Send never fails because memory
is broken.
"""
from __future__ import annotations
import datetime
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from src.config import SETTINGS_DIR
from src.logger import get_logger

log = get_logger(__name__)

MEMORY_DIR: Path = SETTINGS_DIR / "memory"
COLLECTION_NAME = "exchanges"

# Sensible per-backend default model when settings.yaml has the backend
# but not a model (e.g., user toggled backend in the UI but didn't pick
# a model name).
_DEFAULT_MODELS = {
    "gemini": "gemini-embedding-001",
    "openai": "text-embedding-3-small",
    "local": "all-MiniLM-L6-v2",
}


class _LazyEmbeddingFn:
    """Embedding-function wrapper that defers heavy construction.

    Passed to :meth:`chromadb.PersistentClient.get_or_create_collection`
    instead of the real :class:`SentenceTransformerEmbeddingFunction`
    (or our Gemini wrapper, or OpenAI's). For *existing* collections,
    ChromaDB only reads collection metadata and never calls the
    function, so the model is never loaded just to display a count or
    open Settings — which previously froze the UI for ~1s while
    sentence-transformers read 80MB of weights and pinged HF Hub.

    The real underlying function is constructed on the first invocation
    (whether ChromaDB calls ``__call__``, ``embed_query`` or
    ``embed_documents`` — newer chromadb versions split add/query into
    those two methods). The constructor is guarded by a lock so a
    concurrent first-add and first-query don't race to build two
    copies of the model.

    ``name`` is returned eagerly via :meth:`name` — ChromaDB compares it
    against the embedding-function name stored in the collection's
    metadata, and a mismatch is a *hard error* on collection open. We
    pass the same name the real backend's class would report (e.g.
    ``"sentence_transformer"``, lowercase snake_case from chromadb's
    static ``name()`` method) so the round-trip is silent.
    """

    def __init__(self, factory: Callable[[], Any], name: str) -> None:
        self._factory = factory
        self._real: Any = None
        self._name = name
        self._lock = threading.Lock()

    def _materialize(self) -> Any:
        """Build the real embedding function on first use, cache thereafter.
        Lock-protected double-check so two ChromaDB threads can't race
        the constructor."""
        if self._real is None:
            with self._lock:
                if self._real is None:
                    log.info("[memory] Materializing real embedding fn (%s).",
                             self._name)
                    self._real = self._factory()
        return self._real

    def __call__(self, input: Any) -> Any:
        return self._materialize()(input)

    def embed_query(self, input: Any) -> Any:
        """ChromaDB's new query path goes through this, not __call__."""
        real = self._materialize()
        # Real EFs have either embed_query (modern chromadb) or only
        # __call__ (older). Prefer the specific method, fall back to
        # the callable form so we work across versions.
        if hasattr(real, "embed_query"):
            return real.embed_query(input=input)
        return real(input)

    def embed_documents(self, input: Any) -> Any:
        """ChromaDB's new add path goes through this, not __call__."""
        real = self._materialize()
        if hasattr(real, "embed_documents"):
            return real.embed_documents(input=input)
        return real(input)

    def name(self) -> str:
        return self._name


class MemoryUnavailable(RuntimeError):
    """Raised when the memory store can't be initialised (missing deps,
    missing key, etc.). Callers should catch this and degrade gracefully.
    """


class MemoryStore:
    """Wraps a single ChromaDB collection used by all memory-enabled templates.

    Methods may raise :class:`MemoryUnavailable`; callers should treat
    that as "memory is off this Send" rather than a fatal error.
    """

    def __init__(self, config: Optional[dict] = None) -> None:
        config = dict(config or {})
        self.enabled: bool = bool(config.get("enabled", False))
        self.backend: str = str(config.get("backend", "gemini"))
        self.model: str = str(
            config.get("model") or _DEFAULT_MODELS.get(self.backend, "")
        )
        self.top_k: int = int(config.get("top_k", 3) or 3)
        # Local backend only: skip the HF Hub "is there a newer revision?"
        # HEAD on every model load. Enabled via Settings → Memory; takes
        # effect on next app start (see _local_embedding_fn). When True,
        # the local backend works fully offline once the model is cached.
        self.local_offline_mode: bool = bool(config.get("local_offline_mode", False))
        self._client: Any = None
        self._collection: Any = None
        # Short, user-facing summary of the most recent failure (or None
        # after the next successful op). The overlay reads this after
        # every memory call so it can surface "⚠ memory error" in the
        # usage strip — previously these errors only hit the logs.
        self.last_error: Optional[str] = None
        log.debug("MemoryStore configured: enabled=%s backend=%s model=%s top_k=%d",
                  self.enabled, self.backend, self.model, self.top_k)

    # ------------------------------------------------------------------ #
    # Error tracking
    # ------------------------------------------------------------------ #

    def _record_error(self, op: str, exc: BaseException) -> None:
        """Capture a one-line summary of a failed operation for the UI."""
        raw = str(exc).strip() or repr(exc)
        # OpenAI's NotFoundError stringifies into a long JSON-ish blob —
        # trim it so the usage strip / tooltip stays readable.
        if len(raw) > 240:
            raw = raw[:240] + "…"
        self.last_error = f"[{op}] {raw}"

    def _clear_error(self) -> None:
        self.last_error = None

    # ------------------------------------------------------------------ #
    # Lazy init
    # ------------------------------------------------------------------ #

    def _ensure_loaded(self) -> None:
        if self._collection is not None:
            return
        try:
            import chromadb
        except ImportError as exc:
            raise MemoryUnavailable(
                "chromadb is not installed. Run "
                "`pip install -r requirements-memory.txt` to enable memory."
            ) from exc

        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        embedding_fn = self._build_embedding_fn()
        try:
            self._client = chromadb.PersistentClient(path=str(MEMORY_DIR))
            self._collection = self._client.get_or_create_collection(
                name=COLLECTION_NAME,
                embedding_function=embedding_fn,
            )
        except Exception as exc:  # noqa: BLE001 — chroma can raise plenty
            raise MemoryUnavailable(
                f"Failed to open memory store: {exc}"
            ) from exc
        log.info("Memory store ready (backend=%s, model=%s, %d entries).",
                 self.backend, self.model, self._collection.count())

    def _build_embedding_fn(self) -> Any:
        """Return a lazy proxy embedding function.

        The proxy is what ChromaDB sees; the real backend (sentence-
        transformers / OpenAI client / Gemini OpenAI-compat) is
        constructed only when ChromaDB actually invokes the function
        for an embed. Opening Settings (which just calls ``count()``)
        thus does NOT load the local model anymore.

        ``name`` is read dynamically from each backend's class (e.g.
        ChromaDB's ``SentenceTransformerEmbeddingFunction.name() ==
        "sentence_transformer"`` — lowercase snake_case, not the class
        name) so the proxy reports the *exact* string ChromaDB persists
        in collection metadata. A mismatch makes ChromaDB refuse to
        open the collection — that's what bit us when we first guessed
        ``"SentenceTransformerEmbeddingFunction"``.
        """
        if self.backend == "local":
            return _LazyEmbeddingFn(
                self._local_embedding_fn,
                name=self._backend_name_for("local"),
            )
        if self.backend == "gemini":
            return _LazyEmbeddingFn(
                self._gemini_embedding_fn,
                name=self._backend_name_for("gemini"),
            )
        if self.backend == "openai":
            return _LazyEmbeddingFn(
                self._openai_embedding_fn,
                name=self._backend_name_for("openai"),
            )
        raise MemoryUnavailable(f"Unknown embedding backend: {self.backend!r}")

    def _backend_name_for(self, backend: str) -> str:
        """Return the name string the real embedding function would emit.

        ChromaDB's built-in EFs expose their persisted name as a
        ``@staticmethod name()`` on the class, so we can read it
        without instantiating (no model load). Importing the class
        itself doesn't trigger sentence-transformers / openai — those
        are pulled in lazily inside the constructor. Falls back to a
        hardcoded canonical name if the static-method read fails (e.g.
        future chromadb refactor), so a missing-method bug here never
        blocks the user.
        """
        if backend == "local":
            return self._read_class_name(
                "chromadb.utils.embedding_functions",
                "SentenceTransformerEmbeddingFunction",
                fallback="sentence_transformer",
            )
        if backend == "openai":
            return self._read_class_name(
                "chromadb.utils.embedding_functions",
                "OpenAIEmbeddingFunction",
                fallback="openai",
            )
        if backend == "gemini":
            # Our custom Gemini wrapper class doesn't override name(),
            # so it inherits the default — class name. Hardcoded here
            # because the class lives inside _gemini_embedding_fn and
            # isn't importable without invoking it.
            return "_GeminiOpenAICompatEmbedding"
        return backend

    @staticmethod
    def _read_class_name(module: str, cls_name: str, fallback: str) -> str:
        try:
            mod = __import__(module, fromlist=[cls_name])
            cls = getattr(mod, cls_name)
            n = cls.name()  # @staticmethod on chromadb's built-in EFs
            if isinstance(n, str) and n:
                return n
        except Exception:  # noqa: BLE001
            log.debug("Could not read %s.name() statically; using fallback %r.",
                      cls_name, fallback)
        return fallback

    def _local_embedding_fn(self) -> Any:
        # Apply HF Hub offline mode BEFORE the import chain reaches
        # `huggingface_hub.constants`, which captures HF_HUB_OFFLINE into
        # a module constant at import time. After that, changes to the
        # env var don't take effect — that's why the Settings tooltip
        # for this option says "Requires restart". Setting it here works
        # because nothing in the app imports huggingface_hub at top
        # level; the first import comes from sentence_transformers when
        # we construct SentenceTransformerEmbeddingFunction below.
        if self.local_offline_mode:
            os.environ["HF_HUB_OFFLINE"] = "1"
            log.info("[memory] HF_HUB_OFFLINE=1 — skipping HuggingFace Hub checks.")
        try:
            from chromadb.utils import embedding_functions
        except ImportError as exc:
            raise MemoryUnavailable(
                "chromadb embedding_functions module is unavailable."
            ) from exc
        try:
            return embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=self.model or _DEFAULT_MODELS["local"],
            )
        except ImportError as exc:
            raise MemoryUnavailable(
                "sentence-transformers is not installed. Run "
                "`pip install sentence-transformers` for the local backend."
            ) from exc

    def _openai_embedding_fn(self) -> Any:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise MemoryUnavailable("OPENAI_API_KEY is not set in the environment.")
        try:
            from chromadb.utils import embedding_functions
        except ImportError as exc:
            raise MemoryUnavailable(
                "chromadb embedding_functions module is unavailable."
            ) from exc
        return embedding_functions.OpenAIEmbeddingFunction(
            api_key=api_key,
            model_name=self.model or _DEFAULT_MODELS["openai"],
        )

    def _gemini_embedding_fn(self) -> Any:
        # No first-party Gemini embedding function ships with chromadb,
        # so we wrap the OpenAI-compat endpoint Gemini exposes.
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise MemoryUnavailable("GEMINI_API_KEY is not set in the environment.")
        model_name = self.model or _DEFAULT_MODELS["gemini"]

        try:
            from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
        except ImportError as exc:
            raise MemoryUnavailable("chromadb types module is unavailable.") from exc

        class _GeminiOpenAICompatEmbedding(EmbeddingFunction[Documents]):
            def __call__(self, input: Documents) -> Embeddings:
                from openai import OpenAI
                client = OpenAI(
                    api_key=api_key,
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                )
                response = client.embeddings.create(
                    model=model_name,
                    input=list(input),
                )
                return [item.embedding for item in response.data]

        return _GeminiOpenAICompatEmbedding()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def add(
        self,
        template_name: str,
        user_text: str,
        assistant_text: str,
        had_image: bool = False,
    ) -> None:
        """Persist one user → assistant exchange.

        Silently no-ops on empty assistant_text. Unlike :meth:`add_document`,
        this swallows ``MemoryUnavailable`` and logs a warning — the Send
        path shouldn't fail the user's chat because long-term memory broke.
        Other exceptions still propagate.
        """
        if not assistant_text.strip():
            return
        document = f"User: {user_text}\n\nAssistant: {assistant_text}"
        try:
            self.add_document(
                template_name,
                document,
                metadata={"kind": "exchange", "had_image": bool(had_image)},
            )
        except MemoryUnavailable as exc:
            log.warning("Skipping memory.add (Send path): %s", exc)
            self._record_error("save", exc)

    def add_document(
        self,
        template_name: str,
        document: str,
        metadata: Optional[dict] = None,
    ) -> None:
        """Persist an arbitrary document (e.g. a curated summary).

        Use this when the entry isn't a literal Q/A pair — summaries
        distilled from multi-turn exchanges, manual notes, etc.
        ``metadata`` is merged with the always-present ``template_name``
        and ``timestamp`` fields.

        Raises :class:`MemoryUnavailable` if the store can't be loaded
        (missing deps, missing key, etc.). Callers that explicitly invoked
        a save (e.g. the Summarize button) should surface this to the
        user; background savers (the Send path) should catch and log.
        """
        if not document.strip():
            return
        self._ensure_loaded()

        meta: dict = {
            "template_name": template_name,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        }
        if metadata:
            # Caller can override our defaults, but template_name is enforced.
            meta.update(metadata)
            meta["template_name"] = template_name
        entry_id = f"{template_name}_{uuid.uuid4().hex}"
        try:
            self._collection.add(
                documents=[document],
                metadatas=[meta],
                ids=[entry_id],
            )
            log.info("[memory] Saved %s for '%s' (%d chars).",
                     meta.get("kind", "document"), template_name, len(document))
            self._clear_error()
        except Exception as exc:  # noqa: BLE001
            log.exception("memory.add_document failed for template '%s'",
                          template_name)
            self._record_error("save", exc)
            raise

    def query(
        self,
        template_name: str,
        query_text: str,
        k: Optional[int] = None,
    ) -> list[str]:
        """Return up to k past exchange texts for the given template."""
        if not query_text.strip():
            return []
        try:
            self._ensure_loaded()
        except MemoryUnavailable as exc:
            log.warning("Skipping memory.query: %s", exc)
            return []

        k = k if k is not None else self.top_k
        try:
            result = self._collection.query(
                query_texts=[query_text],
                n_results=max(1, k),
                where={"template_name": template_name},
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Memory query failed for template '%s'", template_name)
            self._record_error("retrieve", exc)
            return []
        self._clear_error()
        docs = result.get("documents") or [[]]
        return list(docs[0])

    def verify(self) -> tuple[bool, str]:
        """Real round-trip embed of a tiny string, for the **Test
        embedding** button in Settings.

        Forces lazy init, then runs ``self._collection.query`` with a
        throwaway string. The query path goes through the configured
        embedding function, so this is a faithful test:

        * On **cloud** backends, it makes one real API call (catches
          bad API keys, deprecated models, network issues).
        * On **local**, it triggers the sentence-transformers download
          on first run (~80MB) — that's the *intended* side effect: the
          button doubles as "install the local model so the first real
          Send isn't a silent 60s hang".

        Returns ``(ok, message)``. The message is user-facing — caller
        can drop it straight into a :class:`QMessageBox`.
        """
        try:
            self._ensure_loaded()
        except MemoryUnavailable as exc:
            return False, str(exc)
        try:
            self._collection.query(query_texts=["ping"], n_results=1)
        except Exception as exc:  # noqa: BLE001
            log.exception("Memory verify failed")
            self._record_error("verify", exc)
            return False, f"{type(exc).__name__}: {exc}"
        self._clear_error()
        return True, (
            f"OK — backend={self.backend}, model={self.model}, "
            f"{self.count()} entr{'y' if self.count() == 1 else 'ies'} on disk."
        )

    def probe(self) -> Optional[str]:
        """Test-load the store without touching it.

        Returns ``None`` on success, or a short user-facing reason string
        when the store can't be used (memory disabled, missing dep,
        missing API key, etc.). Lets the UI gray out memory-dependent
        actions with a meaningful tooltip *before* the user clicks.
        """
        if not self.enabled:
            return "Long-term memory is disabled in Settings."
        try:
            self._ensure_loaded()
        except MemoryUnavailable as exc:
            return str(exc)
        return None

    def count(self, template_name: Optional[str] = None) -> int:
        """Total entries (optionally filtered by template). 0 if unavailable."""
        try:
            self._ensure_loaded()
        except MemoryUnavailable:
            return 0
        try:
            if template_name is None:
                return int(self._collection.count())
            result = self._collection.get(where={"template_name": template_name})
            return len(result.get("ids", []) or [])
        except Exception:
            log.exception("Memory count failed")
            return 0

    def clear(self, template_name: Optional[str] = None) -> int:
        """Delete entries (all or per-template). Returns number removed."""
        try:
            self._ensure_loaded()
        except MemoryUnavailable as exc:
            log.warning("Skipping memory.clear: %s", exc)
            return 0
        try:
            if template_name is None:
                # Easiest path: drop the whole collection and recreate it.
                # Recreation happens lazily via _ensure_loaded on next call.
                self._client.delete_collection(COLLECTION_NAME)
                self._collection = None
                log.info("[memory] Cleared entire memory store.")
                return -1  # caller may show "all" rather than a count
            result = self._collection.get(where={"template_name": template_name})
            ids = result.get("ids", []) or []
            if ids:
                self._collection.delete(ids=ids)
                log.info("[memory] Cleared %d entr(ies) for template '%s'.",
                         len(ids), template_name)
            return len(ids)
        except Exception:
            log.exception("Memory clear failed")
            return 0


# ---------------------------------------------------------------------- #
# Prompt formatting
# ---------------------------------------------------------------------- #

def format_memories_for_prompt(memories: list[str]) -> str:
    """Render retrieved memory entries into a single system-prompt string."""
    if not memories:
        return ""
    parts = [
        "Below are past exchanges from this template that may be relevant. "
        "Use them as context for the new question; mention them only if "
        "they actually help."
    ]
    for i, mem in enumerate(memories, 1):
        parts.append(f"\n[Past exchange {i}]\n{mem}")
    return "\n".join(parts)
