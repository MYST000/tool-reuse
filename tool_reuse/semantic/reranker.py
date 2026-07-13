from __future__ import annotations

import math
from typing import Protocol


class Reranker(Protocol):
    model_id: str

    def score(self, query: str, documents: list[str]) -> list[float]: ...


class CrossEncoderReranker:
    def __init__(self, model_id: str, *, device: str | None = None):
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is not installed; install it before using a CrossEncoder reranker"
            ) from exc
        self.model_id = model_id
        self._model = CrossEncoder(model_id, device=device)

    def score(self, query: str, documents: list[str]) -> list[float]:
        raw = self._model.predict([(query, document) for document in documents])
        return [_probability(float(value)) for value in raw]


def _probability(value: float) -> float:
    if 0.0 <= value <= 1.0:
        return value
    return 1.0 / (1.0 + math.exp(-value))
