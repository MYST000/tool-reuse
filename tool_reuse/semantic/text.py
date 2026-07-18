from __future__ import annotations

import math
import re
from collections import Counter


TOKEN_RE = re.compile(r"[\w]+|[./:@?&=+-]", re.UNICODE)
CJK_RE = re.compile(r"[\u3400-\u9fff]")


def tokens(text: str) -> list[str]:
    lowered = text.lower()
    result = TOKEN_RE.findall(lowered)
    cjk = CJK_RE.findall(lowered)
    result.extend(cjk)
    result.extend(a + b for a, b in zip(cjk, cjk[1:]))
    return result


def bm25_scores(
    query: str, documents: list[str], k1: float = 1.5, b: float = 0.75
) -> list[float]:
    if not documents:
        return []
    query_terms = list(dict.fromkeys(tokens(query)))
    doc_tokens = [tokens(document) for document in documents]
    lengths = [len(items) for items in doc_tokens]
    average_length = sum(lengths) / len(lengths) if lengths else 0.0
    frequencies = [Counter(items) for items in doc_tokens]
    document_frequency = {
        term: sum(1 for frequency in frequencies if term in frequency)
        for term in query_terms
    }

    raw_scores: list[float] = []
    document_count = len(documents)
    for frequency, length in zip(frequencies, lengths):
        score = 0.0
        for term in query_terms:
            term_frequency = frequency.get(term, 0)
            if term_frequency == 0:
                continue
            df = document_frequency[term]
            inverse_document_frequency = math.log(
                1 + (document_count - df + 0.5) / (df + 0.5)
            )
            denominator = (
                term_frequency + k1 * (1 - b + b * length / average_length)
                if average_length
                else term_frequency
            )
            score += inverse_document_frequency * (
                term_frequency * (k1 + 1) / denominator
            )
        raw_scores.append(score)

    maximum = max(raw_scores, default=0.0)
    if maximum <= 0:
        return [0.0] * len(raw_scores)
    return [score / maximum for score in raw_scores]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]
