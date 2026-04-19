"""ChromaDB vector store for the Agilent KB RAG."""
from __future__ import annotations

import hashlib
import logging
import os

logger = logging.getLogger(__name__)

_CHROMA_PATH = os.environ.get("RAG_CHROMA_PATH", "./rag_data")
_COLLECTION_NAME = "agilent_kb"
_CHUNK_SIZE = int(os.environ.get("RAG_CHUNK_SIZE", "400"))   # words
_CHUNK_OVERLAP = int(os.environ.get("RAG_CHUNK_OVERLAP", "40"))  # words

_client = None
_collection = None


def _col():
    global _client, _collection
    if _collection is None:
        import chromadb
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        _client = chromadb.PersistentClient(path=_CHROMA_PATH)
        _collection = _client.get_or_create_collection(
            name=_COLLECTION_NAME,
            embedding_function=DefaultEmbeddingFunction(),
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def _chunk(text: str) -> list[str]:
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunks.append(" ".join(words[i : i + _CHUNK_SIZE]))
        i += _CHUNK_SIZE - _CHUNK_OVERLAP
    return chunks


def ingest_docs(docs: list[dict]) -> int:
    """Chunk docs and upsert into ChromaDB. Returns number of chunks stored."""
    col = _col()
    ids, documents, metadatas = [], [], []

    for doc in docs:
        for idx, chunk in enumerate(_chunk(doc["text"])):
            doc_id = hashlib.md5(f"{doc['url']}_{idx}".encode()).hexdigest()
            ids.append(doc_id)
            documents.append(chunk)
            metadatas.append({"url": doc["url"], "title": doc["title"][:200]})

    if ids:
        col.upsert(ids=ids, documents=documents, metadatas=metadatas)
        logger.info("[Store] Upserted %d chunks from %d docs", len(ids), len(docs))

    return len(ids)


def query(question: str, n_results: int = 5) -> list[dict]:
    """Return top-k chunks relevant to question as list of {text, url, title}."""
    col = _col()
    total = col.count()
    if total == 0:
        return []
    results = col.query(query_texts=[question], n_results=min(n_results, total))
    return [
        {"text": doc, "url": meta["url"], "title": meta["title"]}
        for doc, meta in zip(results["documents"][0], results["metadatas"][0])
    ]


def count() -> int:
    """Return number of chunks currently stored."""
    try:
        return _col().count()
    except Exception:
        return 0


def clear() -> None:
    """Drop and recreate the collection."""
    global _client, _collection
    if _client is not None:
        try:
            _client.delete_collection(_COLLECTION_NAME)
        except Exception:
            pass
        _collection = None
