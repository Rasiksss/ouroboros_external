"""Build and query FAISS vector index."""
from __future__ import annotations
import json
import pickle
from pathlib import Path
from typing import Any

import numpy as np

from finengine.config import settings
from finengine.models.document import Chunk, DocumentMeta, ExtractedItem


class VectorIndex:
    """FAISS-backed vector index with metadata store."""

    def __init__(self, index_dir: Path | None = None):
        self.index_dir = index_dir or settings.index_dir
        self._model = None
        self._index = None
        self._chunks: list[Chunk] = []
        self._docs: dict[str, DocumentMeta] = {}
        self._load()

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(settings.embedding_model)
        return self._model

    def _encode_passages(self, texts: list[str]) -> np.ndarray:
        model = self._get_model()
        if settings.embedding_model_family == "e5":
            texts = [f"passage: {t}" for t in texts]
        emb = model.encode(texts, show_progress_bar=False, normalize_embeddings=True, batch_size=32)
        return np.array(emb, dtype=np.float32)

    def _encode_query(self, text: str) -> np.ndarray:
        model = self._get_model()
        if settings.embedding_model_family == "e5":
            text = f"query: {text}"
        emb = model.encode([text], show_progress_bar=False, normalize_embeddings=True)
        return np.array(emb, dtype=np.float32)

    def _load(self):
        index_path = self.index_dir / "faiss.index"
        meta_path = self.index_dir / "meta.pkl"
        if meta_path.exists():
            with open(meta_path, "rb") as f:
                data = pickle.load(f)
                self._chunks = data["chunks"]
                self._docs = data["docs"]
        if index_path.exists():
            import faiss
            self._index = faiss.read_index(str(index_path))

    def _save(self):
        index_path = self.index_dir / "faiss.index"
        meta_path = self.index_dir / "meta.pkl"
        self.index_dir.mkdir(parents=True, exist_ok=True)
        if self._index is not None:
            import faiss
            faiss.write_index(self._index, str(index_path))
        elif index_path.exists():
            index_path.unlink()
        with open(meta_path, "wb") as f:
            pickle.dump({"chunks": self._chunks, "docs": self._docs}, f)

    def add_document(self, meta: DocumentMeta, chunks: list[Chunk]):
        """Register document metadata; embed chunks if any.

        Empty chunks list is a valid call: it stores only `meta` so the doc shows
        up in `list_documents`/`get_document` even though no FAISS data exists.
        Used by the full-context architecture where we don't need vector search.
        """
        import faiss

        if chunks:
            texts = [c.content for c in chunks]
            embeddings = self._encode_passages(texts)
            if self._index is None:
                dim = embeddings.shape[1]
                self._index = faiss.IndexFlatIP(dim)  # cosine on normalized
            self._index.add(embeddings)
            self._chunks.extend(chunks)

        self._docs[meta.doc_id] = meta
        self._save()

    def remove_document(self, doc_id: str):
        """Remove all chunks for a document (rebuilds index)."""
        import faiss

        remaining = [c for c in self._chunks if c.doc_id != doc_id]
        if doc_id in self._docs:
            del self._docs[doc_id]

        if not remaining:
            self._chunks = []
            self._index = None
            self._save()
            return

        texts = [c.content for c in remaining]
        embeddings = self._encode_passages(texts)

        dim = embeddings.shape[1]
        self._index = faiss.IndexFlatIP(dim)
        self._index.add(embeddings)
        self._chunks = remaining
        self._save()

    def search(
        self,
        query: str,
        top_k: int = 8,
        doc_ids: list[str] | None = None,
        doc_type_filter: list[str] | None = None,
        min_score: float = 0.3,
        owner_id: int | None = None,
    ) -> list[ExtractedItem]:
        """Semantic search over indexed chunks.

        owner_id: if set, only chunks whose document's owner_id matches will
        be returned. Pass None to skip the filter (admin use).
        """
        if self._index is None or not self._chunks:
            return []

        q_emb = self._encode_query(query)

        # Search wider than top_k to allow filtering — even more so when an
        # owner-id filter is in play, since most chunks may belong to others.
        k = min(top_k * 4, len(self._chunks))
        scores, indices = self._index.search(q_emb, k)

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(self._chunks):
                continue
            chunk = self._chunks[idx]

            # Apply filters
            if doc_ids and chunk.doc_id not in doc_ids:
                continue
            if owner_id is not None:
                doc = self._docs.get(chunk.doc_id)
                if doc is None or doc.owner_id != owner_id:
                    continue
            if doc_type_filter:
                chunk_type = chunk.metadata.get("doc_type", "")
                if chunk_type not in doc_type_filter:
                    continue
            if score < min_score:
                continue

            results.append(ExtractedItem(
                content=chunk.content,
                score=float(score),
                chunk_type=chunk.chunk_type,
                metadata={
                    **chunk.metadata,
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "page": chunk.page,
                    "section": chunk.section,
                    "speaker": chunk.speaker,
                },
            ))

            if len(results) >= top_k:
                break

        return results

    def get_chunks_by_type(
        self,
        chunk_type: str,
        doc_id: str | None = None,
    ) -> list[ExtractedItem]:
        """Return all chunks of a given type, optionally filtered by doc_id."""
        results = []
        for chunk in self._chunks:
            if chunk.chunk_type != chunk_type:
                continue
            if doc_id and chunk.doc_id != doc_id:
                continue
            results.append(ExtractedItem(
                content=chunk.content,
                score=1.0,
                chunk_type=chunk.chunk_type,
                metadata={
                    **chunk.metadata,
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "page": chunk.page,
                    "section": chunk.section,
                    "speaker": chunk.speaker,
                },
            ))
        return results

    def list_documents(self) -> list[DocumentMeta]:
        return list(self._docs.values())

    def get_document(self, doc_id: str) -> DocumentMeta | None:
        return self._docs.get(doc_id)

    def update_document_meta(self, meta: DocumentMeta) -> None:
        """Replace the stored DocumentMeta for `meta.doc_id` and persist.

        Used by the extract endpoint to write the model-extracted company
        name back into the index, so subsequent doc-list / chat reads see
        the clean name rather than the raw filename. No-op when the
        doc_id isn't registered (caller's responsibility to add first).
        """
        if meta.doc_id in self._docs:
            self._docs[meta.doc_id] = meta
            self._save()

    @property
    def total_chunks(self) -> int:
        return len(self._chunks)

    @property
    def total_documents(self) -> int:
        return len(self._docs)
