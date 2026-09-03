"""Deterministic, zero-network embedders for the golden-query eval harness.

The real embedder (``BAAI/bge-small-en-v1.5``, see ``knowledge/embedder.py``)
is never loaded here — every existing test in this repo stubs the embedder
for the same reasons (determinism, no network, no ~130MB model download), and
this harness follows the same convention.

Two independent fakes, for two different jobs:

* :class:`HashingBowEmbedder` — a feature-hashed bag-of-words vectorizer.
  Tokenizes on camelCase/snake_case/word boundaries, hashes each token into
  one of ``dim`` buckets, and L2-normalizes. This is "real-ish": it responds
  to actual token overlap between a natural-language query and real,
  chunked source code, so it can drive the main golden-query recall/MRR
  suite over the fixture tree without needing a judge model.

* :class:`ControlledAngleEmbedder` — places a text at an exact, arithmetic
  angle in the (x, y) plane when it starts with ``"a<degrees>"``, so KNN
  ordering is provably exact. Used only by the two regression guards (project
  scoping, supersede/authority), where the point is to control distances
  precisely, not to look like a realistic query. Independently implemented
  here (not imported) so this package has no dependency on any file outside
  ``tests/eval/``.
"""

from __future__ import annotations

import hashlib
import math
import re

import numpy as np

DIM = 384

# Splits on runs of letters/digits — underscores and other punctuation are
# already word boundaries because they are excluded from the character class.
_WORD_RE = re.compile(r"[A-Za-z]+|[0-9]+")
# Inserts a boundary between a lowercase/digit and a following uppercase
# letter, so a token like "AuthManager" (one _WORD_RE match) splits into
# "Auth" and "Manager" before lowercasing.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in _WORD_RE.findall(text):
        spaced = _CAMEL_BOUNDARY_RE.sub(" ", raw)
        tokens.extend(spaced.lower().split())
    return tokens


class HashingBowEmbedder:
    """Feature-hashed bag-of-words. Deterministic, no training, no network."""

    def __init__(self, dim: int = DIM):
        self.dim = dim

    def _vectorize(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        for token in _tokenize(text):
            bucket = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16)
            vec[bucket % self.dim] += 1.0
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._vectorize(t) for t in texts]).astype(np.float32)


class ControlledAngleEmbedder:
    """Unit vector at a controlled angle; text must start with ``"a<degrees>"``.

    Same technique as ``tests/test_vec_prefilter_scoping.py``'s private
    ``_AngleEmbedder`` (independent implementation, not imported — this
    module must not depend on anything outside ``tests/eval/``). Text with
    no leading angle token lands on the -x axis (maximally far), keeping
    stray input out of any top-k window instead of colliding with a query.
    """

    _ANGLE = re.compile(r"^a(\d+(?:\.\d+)?)")

    def __init__(self, dim: int = DIM):
        self.dim = dim

    def encode(self, texts: list[str]) -> np.ndarray:
        rows = []
        for t in texts:
            m = self._ANGLE.match(t)
            theta = math.radians(float(m.group(1))) if m else math.pi
            v = np.zeros(self.dim, dtype=np.float32)
            v[0] = math.cos(theta)
            v[1] = math.sin(theta)
            rows.append(v)
        return np.stack(rows).astype(np.float32)
