"""Text → vector embedding via sentence-transformers.

BAAI/bge-small-en-v1.5 — 384-dim, ~130MB model. Downloaded on first use
to ``~/.knowledge/models/`` (overridable via ``HF_HOME`` etc., but the
default keeps everything under the tool's own home dir for tidiness).

Embeddings are L2-normalized at the model layer (``normalize_embeddings=
True``). That means cosine similarity == dot product, and sqlite-vec's
default distance metric (L2 on normalized vectors) gives the same ranking
as cosine distance — exactly what we want.

The module-level ``Embedder`` instance is a lazy singleton: the model is
loaded on the first ``encode`` call, then reused. Re-instantiating the
class repeatedly is safe — the underlying model is cached per-instance,
but callers should use ``get_embedder()`` to share the loaded model.
"""

from __future__ import annotations

import numpy as np

from . import config, paths

_DEFAULT: "Embedder | None" = None


class Embedder:
    def __init__(self) -> None:
        self._model = None

    def _ensure_loaded(self) -> None:
        if self._model is None:
            import logging
            import os
            import warnings

            # Resolve model name: honour user override from settings, fall
            # back to the built-in default.
            from . import settings as settings_mod
            try:
                _s = settings_mod.load_settings()
                _user_model = (_s.embedding_model or "").strip()
            except Exception:
                _user_model = ""

            if _user_model:
                # User supplied their own model — revision and safetensors
                # are their trust decision; we don't pin or override.
                model_name = _user_model
                model_revision = None
                model_kwargs: dict = {}
            else:
                # Default model: pin to the verified on-disk commit SHA
                # (supply-chain safety — see config.MODEL_REVISION).
                # Prefer safetensors over pickle (.bin) for the same reason:
                # safetensors is mmapped and cannot execute code on load.
                model_name = config.MODEL
                model_revision = config.MODEL_REVISION
                model_kwargs = {"use_safetensors": True}

            # The model is downloaded once and cached at paths.models_dir().
            # Every subsequent process load reads from disk, not network.
            #
            # The "unauthenticated requests to the HF Hub" warning fires
            # because SentenceTransformer(...) init unconditionally calls
            # the Hub API to check for model updates. Setting a logger
            # level doesn't help — the warning is emitted by the HTTP
            # layer. The real fix is offline mode: once the cache exists,
            # skip the update check entirely. On first run the env var is
            # NOT set, so the download proceeds normally; every run after
            # that is purely disk-bound.
            model_slug = model_name.replace("/", "--")
            model_dir = paths.models_dir() / f"models--{model_slug}"
            if model_dir.exists():
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

            # Belt-and-suspenders for the first-run case where offline
            # mode isn't yet on but we still want quieter output.
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
            warnings.filterwarnings(
                "ignore", message=r".*unauthenticated requests.*"
            )
            logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

            # Imported lazily so `knowledge --help` and fast commands don't
            # pay the torch/sentence-transformers import cost (~1-2s).
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                model_name,
                cache_folder=str(paths.models_dir()),
                revision=model_revision,
                model_kwargs=model_kwargs or None,
            )

    def encode(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        """Return an ``(N, EMBEDDING_DIM)`` float32 array. L2-normalized."""
        self._ensure_loaded()
        assert self._model is not None
        embs = self._model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=len(texts) > 64,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return embs.astype(np.float32)


def get_embedder() -> Embedder:
    """Shared singleton so repeated ``build``/``search`` calls reuse the model."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Embedder()
    return _DEFAULT
