"""Web crawler for agilent.com — extracts clean text from HTML pages."""
from __future__ import annotations

import logging
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "OneAI-RAG/1.0 (internal knowledge-base indexer)",
    "Accept": "text/html,application/xhtml+xml",
}
_SKIP_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".zip", ".css", ".js"}


def _is_agilent(url: str) -> bool:
    return "agilent.com" in urlparse(url).netloc


def _skip_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _SKIP_EXTS)


def _extract(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()
    title = (soup.title.string or url).strip()
    text = " ".join(soup.get_text(separator=" ").split())
    if len(text) < 150:
        return None
    return {"url": url, "title": title, "text": text}


def crawl(seed_urls: list[str], max_pages: int = 50, delay: float = 0.5) -> list[dict]:
    """Crawl agilent.com from seed_urls and return list of {url, title, text}."""
    visited: set[str] = set()
    queue = list(seed_urls)
    docs: list[dict] = []

    while queue and len(docs) < max_pages:
        url = queue.pop(0)
        # Normalise — strip fragment
        url = urlparse(url)._replace(fragment="").geturl()
        if url in visited or not _is_agilent(url) or _skip_url(url):
            continue
        visited.add(url)

        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15, allow_redirects=True)
            if not resp.ok:
                continue
            ct = resp.headers.get("Content-Type", "")
            if "text/html" not in ct:
                continue

            doc = _extract(resp.text, url)
            if doc:
                docs.append(doc)
                logger.info("[Crawler] %d/%d — %s (%d chars)", len(docs), max_pages, url, len(doc["text"]))

            # Queue same-domain links
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"].strip())
                if _is_agilent(href) and not _skip_url(href) and href not in visited:
                    queue.append(href)

            time.sleep(delay)

        except requests.RequestException as exc:
            logger.warning("[Crawler] Skipped %s — %s", url, exc)

    logger.info("[Crawler] Done — %d pages crawled", len(docs))
    return docs
