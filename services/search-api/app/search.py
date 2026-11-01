"""Pure search logic: tokenizing, typo correction and BM25 ranking.

No I/O here, so every function is unit-testable without Redis.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset({"a", "an", "and", "for", "in", "of", "on", "the", "to", "with"})

# Field weights for BM25F-style scoring: a hit in the product name matters
# more than a hit in the description.
FIELD_WEIGHTS = {"name": 3.0, "category": 2.0, "description": 1.0}
K1 = 1.2
B = 0.75


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


def edit_distance(a: str, b: str, limit: int) -> int:
    """Levenshtein distance, giving up early once it exceeds `limit`."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def max_typos(token: str) -> int:
    # Short tokens tolerate fewer edits, otherwise "tv" would match "pc".
    if len(token) <= 3:
        return 0
    if len(token) <= 6:
        return 1
    return 2


@dataclass
class Index:
    docs: dict[int, dict]
    # term -> {doc_id -> {field -> term frequency}}
    postings: dict[str, dict[int, dict[str, int]]] = field(default_factory=dict)
    field_lengths: dict[int, dict[str, int]] = field(default_factory=dict)
    avg_field_length: dict[str, float] = field(default_factory=dict)

    @classmethod
    def build(cls, docs: dict[int, dict]) -> Index:
        index = cls(docs=docs)
        totals: Counter[str] = Counter()
        for doc_id, doc in docs.items():
            lengths = {}
            for fname in FIELD_WEIGHTS:
                tokens = tokenize(str(doc.get(fname) or ""))
                lengths[fname] = len(tokens)
                totals[fname] += len(tokens)
                for term, tf in Counter(tokens).items():
                    index.postings.setdefault(term, {}).setdefault(doc_id, {})[fname] = tf
            index.field_lengths[doc_id] = lengths
        n = max(len(docs), 1)
        index.avg_field_length = {f: max(totals[f] / n, 1.0) for f in FIELD_WEIGHTS}
        return index

    def doc_frequency(self, term: str) -> int:
        return len(self.postings.get(term, {}))

    def correct(self, token: str) -> str:
        """Return the closest known term, preferring common terms on ties."""
        if token in self.postings:
            return token
        limit = max_typos(token)
        if limit == 0:
            return token
        best, best_key = token, None
        for term in self.postings:
            d = edit_distance(token, term, limit)
            if d > limit:
                continue
            key = (d, -self.doc_frequency(term), term)
            if best_key is None or key < best_key:
                best, best_key = term, key
        return best

    def score(self, terms: list[str]) -> dict[int, float]:
        n = len(self.docs)
        scores: dict[int, float] = {}
        for term in set(terms):
            postings = self.postings.get(term)
            if not postings:
                continue
            df = len(postings)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for doc_id, tfs in postings.items():
                # BM25F: combine weighted, length-normalised term frequencies
                # across fields, then saturate once.
                weighted_tf = 0.0
                for fname, tf in tfs.items():
                    norm = 1 - B + B * self.field_lengths[doc_id][fname] / self.avg_field_length[fname]
                    weighted_tf += FIELD_WEIGHTS[fname] * tf / norm
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * weighted_tf / (K1 + weighted_tf)
        return scores


@dataclass
class SearchResult:
    query: str
    corrected_query: str | None
    total: int
    results: list[dict]


def search(index: Index, query: str, limit: int = 10) -> SearchResult:
    tokens = tokenize(query)
    corrected = [index.correct(t) for t in tokens]
    scores = index.score(corrected)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    results = [
        {
            "id": doc_id,
            "name": index.docs[doc_id].get("name"),
            "category": index.docs[doc_id].get("category"),
            "price": index.docs[doc_id].get("price"),
            "score": round(score, 4),
        }
        for doc_id, score in ranked[:limit]
    ]
    return SearchResult(
        query=query,
        corrected_query=" ".join(corrected) if corrected != tokens else None,
        total=len(ranked),
        results=results,
    )
