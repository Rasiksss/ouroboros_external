"""N-gram extractor with Russian morphological normalization.

Adapted from `financier_extractor.utils.ngrams.NgramPreprocessor`. Heart
of AIFA's table reranker: every metric name and every table row is
normalized (lemmas for Russian, stems for English) and tokenized into
uni/bi/tri-grams. The reranker scores tables by how many of the metric's
n-grams appear in the table's rows.

Heavy initialization (pymorphy3 + nltk corpus) is deferred to the first
call — keeps backend startup fast even when AIFA isn't being used.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass

from finengine.skills.aifa.text_utils import clean_markdown


@dataclass(frozen=True)
class NGrams:
    unigrams: frozenset[str]
    bigrams: frozenset[str]
    trigrams: frozenset[str]


class NgramPreprocessor:
    """Lazy-init wrapper. The pymorphy3 analyzer + NLTK stopwords add
    ~150 MB of memory and a couple seconds to import; we only pay that
    cost when the AIFA path is actually used."""

    def __init__(self) -> None:
        self._ru_morph = None
        self._en_stemmer = None
        self._stopwords: set[str] = set()
        self._initialized = False

    def _ensure_loaded(self) -> None:
        if self._initialized:
            return
        from nltk.corpus import stopwords as _sw  # type: ignore
        from nltk.stem import PorterStemmer  # type: ignore
        try:
            from pymorphy3 import MorphAnalyzer  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "AIFA mode requires pymorphy3. Install it: pip install pymorphy3"
            ) from e
        try:
            self._stopwords = set(_sw.words("russian"))
        except LookupError:
            import nltk  # type: ignore
            nltk.download("stopwords", quiet=True)
            self._stopwords = set(_sw.words("russian"))
        self._ru_morph = MorphAnalyzer(lang="ru")
        self._en_stemmer = PorterStemmer()
        self._initialized = True

    @functools.lru_cache(maxsize=4096)
    def _normalize_word(self, word: str) -> str:
        self._ensure_loaded()
        if re.search(r"[а-яА-Я]", word):
            return self._ru_morph.parse(word)[0].normal_form  # type: ignore[union-attr]
        return self._en_stemmer.stem(word)  # type: ignore[union-attr]

    def _to_tokens(self, text: str, drop_stopwords: bool = True) -> list[str]:
        self._ensure_loaded()
        text = clean_markdown(text.lower())
        out: list[str] = []
        for word in text.split():
            normalized = self._normalize_word(word)
            if not normalized:
                continue
            if drop_stopwords and normalized in self._stopwords:
                continue
            out.append(normalized)
        return out

    def extract_ngrams(self, text: str) -> NGrams:
        tokens = self._to_tokens(text)
        if not tokens:
            return NGrams(frozenset(), frozenset(), frozenset())
        unis = frozenset(tokens)
        bis = frozenset(f"{a} {b}" for a, b in zip(tokens, tokens[1:]))
        tris = frozenset(f"{a} {b} {c}" for a, b, c in zip(tokens, tokens[1:], tokens[2:]))
        return NGrams(unis, bis, tris)

    def extract_ngrams_batch(self, texts: list[str]) -> list[NGrams]:
        return [self.extract_ngrams(t) for t in texts]


# Module-level singleton — first .extract_ngrams() call lazy-loads pymorphy3.
PREPROCESSOR = NgramPreprocessor()
