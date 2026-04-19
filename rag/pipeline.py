"""RAG pipeline: ingest agilent.com and augment chat messages with retrieved context."""
from __future__ import annotations

import logging
import os

from .crawler import crawl
from .store import ingest_docs, query, count

logger = logging.getLogger(__name__)

RAG_MAX_PAGES = int(os.environ.get("RAG_MAX_PAGES", "40"))
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "5"))

SEED_URLS = [
    "https://www.agilent.com/",
    "https://www.agilent.com/en/products",
    "https://www.agilent.com/en/solutions",
    "https://www.agilent.com/en/about-agilent",
    "https://www.agilent.com/en/services",
    "https://www.agilent.com/en/promotions",
    "https://www.agilent.com/en/industries",
]

_SYSTEM_TEMPLATE = (
    "You are OneAI, Agilent's internal knowledge-base assistant. "
    "Answer using ONLY the context excerpts below from agilent.com. "
    "If the answer is not in the context, say you don't have that information. "
    "Always be concise and accurate.\n\n"
    "CONTEXT FROM AGILENT.COM:\n{context}"
)


def ingest(max_pages: int = RAG_MAX_PAGES) -> dict:
    """Crawl agilent.com and index into the vector store. Returns summary dict."""
    docs = crawl(SEED_URLS, max_pages=max_pages)
    chunks = ingest_docs(docs)
    return {"pages": len(docs), "chunks": chunks}


def retrieve_context(question: str) -> str | None:
    """Return a formatted context string of top-k relevant chunks, or None if KB empty."""
    chunks = query(question, n_results=RAG_TOP_K)
    if not chunks:
        return None
    lines = []
    for i, c in enumerate(chunks, 1):
        lines.append(f"[{i}] {c['title']} — {c['url']}\n{c['text']}")
    return "\n\n---\n\n".join(lines)


def augment_messages(messages: list[dict], question: str) -> list[dict]:
    """Prepend a RAG system message to the conversation messages.

    If the KB is empty, returns messages unchanged so SAGE still responds.
    """
    context = retrieve_context(question)
    if not context:
        return messages

    system_msg = {
        "role": "system",
        "content": _SYSTEM_TEMPLATE.format(context=context),
    }
    # Drop any prior system messages; inject fresh one from retrieval
    filtered = [m for m in messages if m.get("role") != "system"]
    return [system_msg] + filtered


def kb_count() -> int:
    return count()
