from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .text import normalize_vector, tokens


class Embedder(Protocol):
    provider_name: str
    model_id: str
    index_id: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


@dataclass
class HashingEmbedder:
    """Dependency-free deterministic embedder for tests, not production quality."""

    dimensions: int = 384
    provider_name: str = "hashing"
    query_prefix: str = ""
    document_prefix: str = ""

    @property
    def model_id(self) -> str:
        return f"hashing-v1-{self.dimensions}"

    @property
    def index_id(self) -> str:
        return _embedding_index_id(
            self.model_id,
            {
                "query_prefix": self.query_prefix,
                "document_prefix": self.document_prefix,
            },
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(self.document_prefix + text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(self.query_prefix + text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        items = tokens(text)
        features = items + [f"{a}::{b}" for a, b in zip(items, items[1:])]
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            number = int.from_bytes(digest, "big")
            index = number % self.dimensions
            sign = 1.0 if (number >> 63) == 0 else -1.0
            vector[index] += sign
        return normalize_vector(vector)


class SentenceTransformerEmbedder:
    provider_name = "sentence-transformers"

    def __init__(
        self,
        model_id: str,
        *,
        query_prefix: str = "",
        document_prefix: str = "",
        device: str | None = None,
    ):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is not installed; install it before using "
                "this provider"
            ) from exc
        self.model_id = model_id
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix
        self.index_id = _embedding_index_id(
            model_id,
            {
                "query_prefix": query_prefix,
                "document_prefix": document_prefix,
            },
        )
        self._model = SentenceTransformer(model_id, device=device)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        values = self._model.encode(
            [self.document_prefix + text for text in texts],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [list(map(float, vector)) for vector in values]

    def embed_query(self, text: str) -> list[float]:
        vector = self._model.encode(
            self.query_prefix + text,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return list(map(float, vector))


class OpenAICompatibleEmbedder:
    provider_name = "openai-compatible"

    def __init__(
        self,
        model_id: str,
        *,
        base_url: str,
        api_key: str | None = None,
        dimensions: int | None = None,
        query_prefix: str = "",
        document_prefix: str = "",
        timeout: float = 60.0,
    ):
        self.model_id = model_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.dimensions = dimensions
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix
        self.timeout = timeout
        self.index_id = _embedding_index_id(
            model_id,
            {
                "base_url": self.base_url,
                "dimensions": dimensions,
                "query_prefix": query_prefix,
                "document_prefix": document_prefix,
            },
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._request([self.document_prefix + text for text in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._request([self.query_prefix + text])[0]

    def _request(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, object] = {"model": self.model_id, "input": texts}
        if self.dimensions is not None:
            payload["dimensions"] = self.dimensions
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        data = sorted(body["data"], key=lambda item: item["index"])
        return [normalize_vector(list(map(float, item["embedding"]))) for item in data]


def create_embedder(
    provider: str,
    model: str | None = None,
    *,
    base_url: str | None = None,
    api_key_env: str = "EMBEDDING_API_KEY",
    dimensions: int | None = None,
    query_prefix: str = "",
    document_prefix: str = "",
    device: str | None = None,
) -> Embedder:
    if provider == "hashing":
        return HashingEmbedder(
            dimensions=dimensions or 384,
            query_prefix=query_prefix,
            document_prefix=document_prefix,
        )
    if provider == "sentence-transformers":
        return SentenceTransformerEmbedder(
            model or "BAAI/bge-small-zh-v1.5",
            query_prefix=query_prefix,
            document_prefix=document_prefix,
            device=device,
        )
    if provider == "openai-compatible":
        if not model or not base_url:
            raise ValueError("openai-compatible requires --model and --base-url")
        return OpenAICompatibleEmbedder(
            model,
            base_url=base_url,
            api_key=os.environ.get(api_key_env),
            dimensions=dimensions,
            query_prefix=query_prefix,
            document_prefix=document_prefix,
        )
    raise ValueError(f"Unknown embedding provider: {provider}")


def _embedding_index_id(model_id: str, config: dict[str, object]) -> str:
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"{model_id}#config-{digest}"
