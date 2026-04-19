# OneAI — RAG Chatbot

Enterprise-grade chat UI built on Flask that unifies three AI backends under a single interface. The flagship **OneAI** mode is a Retrieval-Augmented Generation (RAG) pipeline grounded in [agilent.com](https://www.agilent.com) content.

---

## Features

| Feature | Details |
|---|---|
| **RAG Knowledge Base** | Crawls agilent.com, chunks and embeds pages with `all-MiniLM-L6-v2`, stores vectors in ChromaDB, retrieves top-5 chunks per query |
| **Multi-bot UI** | Switch between OneAI (RAG), SAGE, and OB-4 without leaving the chat |
| **Streaming responses** | Server-Sent Events relay — tokens render in real time |
| **Conversation history** | In-memory session store with sidebar browser |
| **Dark / light mode** | Persisted in `localStorage`, no flash on load |
| **Azure-ready** | Azure Key Vault for secrets, Entra ID OAuth2 for SAGE auth, Gunicorn for WSGI |

---

## Architecture

```
Browser  ──►  Flask (app.py)
                │
                ├─ /api/chat/stream  ──►  rag/pipeline.py  ──►  ChromaDB (rag_data/)
                │                               │                    ↑
                │                         SAGE API (LLM)      rag/store.py
                │                                             rag/crawler.py
                │
                ├─ /api/rag/ingest   ──►  background thread → crawl → embed → store
                ├─ /api/rag/status   ──►  ingest progress + chunk count
                ├─ /api/ob4/chat     ──►  OB-4 / Onboard API
                └─ /api/conversations/*  ──►  session history
```

---

## Quick Start

### 1. Clone and create virtualenv

```bash
git clone <repo-url>
cd oneai-ui
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

> **Note:** `chromadb` pulls in `onnxruntime` to run the embedding model locally — first install takes ~2 min and ~500 MB. Subsequent starts are fast.

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env with your values (see Configuration below)
```

### 4. Run

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000).

---

## Configuration

All settings are environment variables (loaded from `.env` in development).

### SAGE API (LLM backend)

| Variable | Default | Description |
|---|---|---|
| `SAGE_BASE_URL` | `http://localhost:5001/v1` | OpenAI-compatible completions base URL |
| `SAGE_MODEL` | `sage` | Model name sent in every request |
| `SAGE_COMPLETIONS_API_KEY` | — | API key for local dev (skip in production — use Key Vault) |
| `SAGE_VERIFY_SSL` | `0` | Set `1` to enforce TLS certificate verification |

### RAG

| Variable | Default | Description |
|---|---|---|
| `RAG_MAX_PAGES` | `40` | Max agilent.com pages to crawl per ingest run |
| `RAG_TOP_K` | `5` | Chunks injected as context per query |
| `RAG_CHUNK_SIZE` | `400` | Words per chunk |
| `RAG_CHUNK_OVERLAP` | `40` | Overlap words between consecutive chunks |
| `RAG_CHROMA_PATH` | `./rag_data` | ChromaDB persistence directory |

### OB-4 / Onboard

| Variable | Description |
|---|---|
| `OB4_URL` | Onboard completion endpoint |
| `OB4_TOKEN` | API key (`api-key` header) |
| `OB4_EMAIL` | Email sent with each request |
| `OB4_VERIFY_SSL` | `0` by default (Agilent corporate proxy re-signs TLS) |

### Azure (production)

| Variable | Description |
|---|---|
| `AZURE_KEYVAULT_URL` | Key Vault URL — enables Managed Identity secret resolution |
| `SAGE_KEY_SECRET_NAME` | Secret name in Key Vault (default: `sage-completions-api-key`) |
| `AZURE_TENANT_ID` | Entra ID tenant for OAuth2 client credentials flow |
| `AZURE_CLIENT_ID` | App registration client ID |
| `AZURE_CLIENT_SECRET` | App registration client secret |
| `SAGE_RESOURCE_ID` | SAGE resource ID for token scope |
| `FLASK_SECRET_KEY` | Session signing key — **always set in production** |

---

## Using the RAG Knowledge Base

1. Open the app and ensure **OneAI** is selected in the bot panel (left sidebar).
2. Click **Index Agilent KB** — a background thread crawls up to `RAG_MAX_PAGES` pages from agilent.com, chunks and embeds them.
3. The badge on the OneAI card turns green and shows the chunk count when ready.
4. All subsequent OneAI messages are automatically grounded in the indexed content.
5. Click **Re-index Agilent KB** at any time to refresh the knowledge base.

The crawler starts from these seed URLs and follows same-domain links:

- `agilent.com/`
- `agilent.com/en/products`
- `agilent.com/en/solutions`
- `agilent.com/en/about-agilent`
- `agilent.com/en/services`
- `agilent.com/en/promotions`
- `agilent.com/en/industries`

---

## Production Deployment

```bash
gunicorn -w 2 -b 0.0.0.0:8000 app:app
```

Set all secrets via environment variables or Azure Key Vault — never commit `.env` to version control. For persistent conversation history and horizontal scaling, replace the in-memory `conversations` dict with Redis or a database.

---

## Project Structure

```
oneai-ui/
├── app.py                  # Flask application + all routes
├── requirements.txt
├── .env.example
├── rag/
│   ├── __init__.py
│   ├── crawler.py          # agilent.com web crawler
│   ├── store.py            # ChromaDB vector store wrapper
│   └── pipeline.py         # ingest + retrieval + message augmentation
├── templates/
│   └── index.html          # Single-page chat UI (Tailwind CSS)
└── rag_data/               # ChromaDB persistence (created on first ingest)
```
