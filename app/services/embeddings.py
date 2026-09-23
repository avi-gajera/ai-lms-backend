"""Embedding model factory (LangChain `Embeddings` interface).

Default: local `BAAI/bge-small-en-v1.5` via `langchain-huggingface` — free, offline, fast on CPU,
384-dim, normalised (so cosine == dot product). Queries get BGE's retrieval instruction prefix;
passages are embedded as-is (asymmetric retrieval). Swapping to an API embedding model means returning
a different `Embeddings` here; nothing else changes. `fake` is a deterministic hashing embedder for
tests (no model download).
"""

import hashlib
import math
import re
from functools import lru_cache

from langchain_core.embeddings import Embeddings

from app.config import get_settings


class HashingEmbeddings(Embeddings):
    """Deterministic bag-of-words hashing embedder: similar texts → similar vectors. Test-only."""

    def __init__(self, dim: int = 384):
        self.dim = dim

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(word.encode()).hexdigest(), 16)
            vec[h % self.dim] += 1.0 if (h >> 8) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


@lru_cache
def get_embeddings() -> Embeddings:
    s = get_settings()
    if s.embedding_provider == "fake":
        return HashingEmbeddings(s.embedding_dim)

    from langchain_huggingface import HuggingFaceEmbeddings

    query_kwargs = {"normalize_embeddings": True}
    if s.embedding_query_instruction:
        query_kwargs["prompt"] = s.embedding_query_instruction
    return HuggingFaceEmbeddings(
        model_name=s.embedding_model,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
        query_encode_kwargs=query_kwargs,
    )
