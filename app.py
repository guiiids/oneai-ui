"""
OneAI RAG Chatbot — Flask Backend
Connected to SAGE Completions API (OpenAI-compatible)
Key resolved from Azure Key Vault (Managed Identity) in production,
or SAGE_COMPLETIONS_API_KEY env var for local development.
"""
from __future__ import annotations

import os
import uuid
import json
import time
import threading
import logging
import logging.handlers
import functools
import requests
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session, Response, stream_with_context, redirect, url_for, flash
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

try:
    from rag import pipeline as rag_pipeline
    RAG_AVAILABLE = True
except ImportError:
    RAG_AVAILABLE = False

load_dotenv()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fmt = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(log_level)

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(log_level)
    ch.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(ch)

    # Rotating file handler — oneai.log, max 5 MB, keep 3 backups
    fh = logging.handlers.RotatingFileHandler(
        "oneai.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(log_level)
    fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(fh)

    return logging.getLogger(__name__)


logger = _setup_logging()


# ---------------------------------------------------------------------------
# Azure Key Vault — resolve SAGE API key at startup
# ---------------------------------------------------------------------------

def _resolve_api_key() -> str:
    """
    Fetch SAGE_COMPLETIONS_API_KEY from Azure Key Vault (Managed Identity)
    when AZURE_KEYVAULT_URL is configured, with a fallback to the local env var.

    - Production (App Service): set AZURE_KEYVAULT_URL + SAGE_KEY_SECRET_NAME
      in App Service config. Managed Identity is used automatically.
    - Local dev: set SAGE_COMPLETIONS_API_KEY in .env. Key Vault is skipped.
    """
    vault_url = os.environ.get("AZURE_KEYVAULT_URL", "").strip().rstrip("/")
    if vault_url:
        secret_name = os.environ.get("SAGE_KEY_SECRET_NAME", "sage-completions-api-key")
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            credential = DefaultAzureCredential()
            client = SecretClient(vault_url=vault_url, credential=credential)
            secret = client.get_secret(secret_name)
            logger.info("[Key Vault] Loaded SAGE_COMPLETIONS_API_KEY from Key Vault ✓")
            return secret.value or ""
        except ImportError:
            logger.warning(
                "[Key Vault] azure-identity / azure-keyvault-secrets not installed. "
                "Falling back to env var."
            )
        except Exception as e:
            logger.error(
                f"[Key Vault] Failed to fetch secret '{secret_name}' from {vault_url}: {e}. "
                "Falling back to env var."
            )

    # Local dev fallback
    key = os.environ.get("SAGE_COMPLETIONS_API_KEY", "")
    if key:
        logger.info("[Key Vault] Using SAGE_COMPLETIONS_API_KEY from environment (local dev).")
    else:
        logger.warning(
            "[Key Vault] No API key found. Set AZURE_KEYVAULT_URL (production) "
            "or SAGE_COMPLETIONS_API_KEY (local dev)."
        )
    return key


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-in-production")

# ---------------------------------------------------------------------------
# Auth configuration
# ---------------------------------------------------------------------------
# Set AUTH_REQUIRED=0 in .env to disable login requirement (open access)
AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "0") == "1"

if AUTH_REQUIRED:
    logger.info("[Auth] Login required — users must sign in (AUTH_REQUIRED=1)")
else:
    logger.info("[Auth] Login DISABLED — open access mode (AUTH_REQUIRED=0)")

# ---------------------------------------------------------------------------
# User store (in-memory — swap for a database in production)
# ---------------------------------------------------------------------------
# Structure: { "email": { "password_hash": "...", "created_at": "..." } }
users: dict[str, dict] = {}


def login_required(f):
    """Decorator that redirects unauthenticated users to the login page.
    Becomes a no-op when AUTH_REQUIRED=0."""
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if AUTH_REQUIRED and not session.get("user_email"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated_function

# ---------------------------------------------------------------------------
# SAGE Configuration
# ---------------------------------------------------------------------------

SAGE_BASE_URL = os.environ.get("SAGE_BASE_URL", "http://localhost:5001/v1")
SAGE_API_KEY = _resolve_api_key()
SAGE_MODEL = os.environ.get("SAGE_MODEL", "sage")
SAGE_VERIFY_SSL = os.environ.get("SAGE_VERIFY_SSL", "0") == "1"

# Auth header format: "bearer" (default) or "api-key" (Azure OpenAI style)
# Set SAGE_AUTH_HEADER_FORMAT=api-key in App Service if the SAGE endpoint
# expects an `api-key:` header instead of `Authorization: Bearer`.
SAGE_AUTH_HEADER_FORMAT = os.environ.get("SAGE_AUTH_HEADER_FORMAT", "bearer").lower().strip()

# ---------------------------------------------------------------------------
# Entra ID (Azure AD) — OAuth2 Client Credentials for production SAGE
# ---------------------------------------------------------------------------
AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID", "").strip()
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "").strip()
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "").strip()
SAGE_RESOURCE_ID = os.environ.get("SAGE_RESOURCE_ID", "").strip()

USE_ENTRA_AUTH = bool(AZURE_CLIENT_ID and AZURE_CLIENT_SECRET and AZURE_TENANT_ID)

if USE_ENTRA_AUTH:
    logger.info("[Auth] Entra ID mode enabled — will acquire OAuth2 tokens for SAGE")
elif SAGE_API_KEY:
    logger.info(
        "[Auth] API key mode — header format: %s | key ends: ...%s",
        SAGE_AUTH_HEADER_FORMAT,
        SAGE_API_KEY[-6:],
    )
else:
    logger.warning("[Auth] No SAGE credentials found — requests will be unauthenticated!")

_token_cache = {"access_token": "", "expires_at": 0}
_token_lock = threading.Lock()


def _get_sage_oauth_token() -> str:
    """
    Acquire an OAuth2 access token via Entra ID client credentials flow.
    Tokens are cached in-memory and refreshed 60s before expiry.
    """
    with _token_lock:
        if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
            return _token_cache["access_token"]

    try:
        import msal
    except ImportError:
        raise RuntimeError(
            "msal is required for Entra ID auth. Run: pip install msal"
        )

    authority = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}"
    app = msal.ConfidentialClientApplication(
        AZURE_CLIENT_ID,
        authority=authority,
        client_credential=AZURE_CLIENT_SECRET,
    )
    scopes = [f"{SAGE_RESOURCE_ID}/.default"] if SAGE_RESOURCE_ID else []
    result = app.acquire_token_for_client(scopes=scopes)

    if "access_token" in result:
        with _token_lock:
            _token_cache["access_token"] = result["access_token"]
            _token_cache["expires_at"] = time.time() + result.get("expires_in", 3600)
        logger.info("[Entra ID] Token acquired ✓ (expires in %ds)", result.get("expires_in", 0))
        return result["access_token"]

    error_desc = result.get("error_description", result.get("error", "unknown"))
    logger.error("[Entra ID] Token acquisition failed: %s", error_desc)
    raise RuntimeError(f"Entra ID token error: {error_desc}")

# ---------------------------------------------------------------------------
# OB-4 / Onboard API Configuration
# ---------------------------------------------------------------------------

OB4_URL = os.environ.get("OB4_URL", "")
OB4_TOKEN = os.environ.get("OB4_TOKEN", "")
OB4_EMAIL = os.environ.get("OB4_EMAIL", "")

# SSL verification for outbound HTTPS — Agilent's corporate proxy re-signs
# TLS certs with an internal CA whose full chain is not always in the
# local CA bundle.  For internal Azure endpoints we skip verification.
# Set OB4_VERIFY_SSL=1 to re-enable once the CA bundle is fixed.
OB4_VERIFY_SSL = os.environ.get("OB4_VERIFY_SSL", "0") == "1"


# ---------------------------------------------------------------------------
# RAG ingest state
# ---------------------------------------------------------------------------
_ingest_state: dict = {"status": "idle", "pages": 0, "chunks": 0, "error": ""}
_ingest_lock = threading.Lock()


def _run_ingest(max_pages: int) -> None:
    global _ingest_state
    with _ingest_lock:
        _ingest_state = {"status": "running", "pages": 0, "chunks": 0, "error": ""}
    try:
        result = rag_pipeline.ingest(max_pages=max_pages)
        with _ingest_lock:
            _ingest_state = {"status": "done", **result, "error": ""}
        logger.info("[RAG] Ingest complete: %s", result)
    except Exception as exc:
        logger.exception("[RAG] Ingest failed")
        with _ingest_lock:
            _ingest_state = {"status": "error", "pages": 0, "chunks": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# In-memory store (swap for Redis / DB in production)
# ---------------------------------------------------------------------------
conversations: dict[str, list[dict]] = {}


def get_or_create_conversation(conv_id: str | None = None) -> tuple[str, list[dict]]:
    """Return (conversation_id, messages) — creates a new one when needed."""
    if conv_id and conv_id in conversations:
        return conv_id, conversations[conv_id]
    new_id = uuid.uuid4().hex[:12]
    conversations[new_id] = []
    return new_id, conversations[new_id]


def _build_sage_messages(history: list[dict]) -> list[dict]:
    """Convert our history format to OpenAI-compatible messages array."""
    return [
        {"role": m["role"], "content": m["content"]}
        for m in history
        if m["role"] in ("user", "assistant")
    ]


def _sage_headers() -> dict:
    """Build request headers for SAGE API calls.

    Production: Entra ID OAuth2 token (when AZURE_CLIENT_ID is configured).
    Local dev:  Static API key via Bearer or api-key header (SAGE_AUTH_HEADER_FORMAT).
    """
    headers = {"Content-Type": "application/json"}
    if USE_ENTRA_AUTH:
        try:
            token = _get_sage_oauth_token()
            headers["Authorization"] = f"Bearer {token}"
        except RuntimeError as e:
            logger.error("[SAGE] Entra ID auth failed, falling back to API key: %s", e)
            if SAGE_API_KEY:
                _apply_api_key_header(headers, SAGE_API_KEY)
    elif SAGE_API_KEY:
        _apply_api_key_header(headers, SAGE_API_KEY)
    return headers


def _apply_api_key_header(headers: dict, key: str) -> None:
    """Apply the API key to headers using the configured format.

    SAGE_AUTH_HEADER_FORMAT=bearer  → Authorization: Bearer <key>  (default)
    SAGE_AUTH_HEADER_FORMAT=api-key → api-key: <key>  (Azure OpenAI style)
    """
    if SAGE_AUTH_HEADER_FORMAT == "api-key":
        headers["api-key"] = key
    else:
        headers["Authorization"] = f"Bearer {key}"


# ---------------------------------------------------------------------------
# SAGE API — Non-streaming call
# ---------------------------------------------------------------------------
def query_sage(messages: list[dict], session_id: str = "") -> str:
    """
    Call SAGE Completions API (non-streaming) and return the response text.
    """
    payload = {
        "model": SAGE_MODEL,
        "messages": messages,
        "stream": False,
    }
    if session_id:
        payload["user"] = session_id

    logger.info("[SAGE] Non-streaming request | session=%s | messages=%d", session_id, len(messages))
    try:
        resp = requests.post(
            f"{SAGE_BASE_URL}/chat/completions",
            headers=_sage_headers(),
            json=payload,
            timeout=120,
            verify=SAGE_VERIFY_SSL,
        )
        resp.raise_for_status()
        data = resp.json()
        reply = data["choices"][0]["message"]["content"]
        logger.info("[SAGE] Response OK | session=%s | chars=%d", session_id, len(reply))
        return reply
    except requests.exceptions.ConnectionError:
        logger.error("[SAGE] ConnectionError — is SAGE_BASE_URL=%s reachable?", SAGE_BASE_URL)
        return "⚠ Could not connect to SAGE. Please verify the server is running and `SAGE_BASE_URL` is correct."
    except requests.exceptions.Timeout:
        logger.error("[SAGE] Timeout after 120s | session=%s", session_id)
        return "⚠ SAGE request timed out. The server may be overloaded — try again shortly."
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        logger.error("[SAGE] HTTP %s | session=%s", status, session_id)
        return f"⚠ SAGE returned HTTP {status}. Check your `SAGE_COMPLETIONS_API_KEY` and server logs."
    except Exception as e:
        logger.exception("[SAGE] Unexpected error | session=%s", session_id)
        return f"⚠ Unexpected error calling SAGE: {e}"


# ---------------------------------------------------------------------------
# SAGE API — Streaming SSE relay
# ---------------------------------------------------------------------------
def stream_sage(messages: list[dict], session_id: str = ""):
    """
    Generator that yields SSE events from SAGE streaming response.
    Each yield is a `data: ...` line ready for the browser EventSource.
    """
    payload = {
        "model": SAGE_MODEL,
        "messages": messages,
        "stream": True,
    }
    if session_id:
        payload["user"] = session_id

    logger.info("[SAGE] Streaming request | session=%s | messages=%d", session_id, len(messages))
    try:
        with requests.post(
            f"{SAGE_BASE_URL}/chat/completions",
            headers=_sage_headers(),
            json=payload,
            timeout=120,
            stream=True,
            verify=SAGE_VERIFY_SSL,
        ) as resp:
            resp.raise_for_status()
            logger.info("[SAGE] Stream opened | session=%s | status=%s", session_id, resp.status_code)
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                # Forward the SSE line as-is (SAGE sends `data: {...}`)
                if line.startswith("data: "):
                    yield f"{line}\n\n"
                elif line.startswith("data:"):
                    yield f"{line}\n\n"
            logger.info("[SAGE] Stream closed | session=%s", session_id)
    except requests.exceptions.ConnectionError:
        logger.error("[SAGE] Stream ConnectionError — is SAGE_BASE_URL=%s reachable?", SAGE_BASE_URL)
        error_payload = json.dumps({
            "id": "error",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": SAGE_MODEL,
            "choices": [{"index": 0, "delta": {"content": "⚠ Could not connect to SAGE."}, "finish_reason": "stop"}],
        })
        yield f"data: {error_payload}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        logger.exception("[SAGE] Stream error | session=%s", session_id)
        error_payload = json.dumps({
            "id": "error",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": SAGE_MODEL,
            "choices": [{"index": 0, "delta": {"content": f"⚠ Error: {e}"}, "finish_reason": "stop"}],
        })
        yield f"data: {error_payload}\n\n"
        yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Auth Routes
# ---------------------------------------------------------------------------
@app.route("/auth/login", methods=["GET"])
def login_page():
    """Show the login / register page."""
    if session.get("user_email"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/auth/login", methods=["POST"])
def login_submit():
    """Validate credentials and log the user in."""
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not email or not password:
        flash("Email and password are required.", "error")
        return redirect(url_for("login_page"))

    user = users.get(email)
    if not user or not check_password_hash(user["password_hash"], password):
        flash("Invalid email or password.", "error")
        return redirect(url_for("login_page"))

    session["user_email"] = email
    logger.info("[Auth] Login OK | user=%s", email)
    return redirect(url_for("index"))


@app.route("/auth/register", methods=["POST"])
def register_submit():
    """Create a new user account."""
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")

    if not email or not password:
        flash("Email and password are required.", "error")
        return redirect(url_for("login_page") + "?tab=register")

    if len(password) < 6:
        flash("Password must be at least 6 characters.", "error")
        return redirect(url_for("login_page") + "?tab=register")

    if password != confirm:
        flash("Passwords do not match.", "error")
        return redirect(url_for("login_page") + "?tab=register")

    if email in users:
        flash("An account with this email already exists.", "error")
        return redirect(url_for("login_page") + "?tab=register")

    users[email] = {
        "password_hash": generate_password_hash(password),
        "created_at": datetime.utcnow().isoformat(),
    }
    logger.info("[Auth] Registered new user | email=%s", email)
    flash("Account created! Please sign in.", "success")
    return redirect(url_for("login_page"))


@app.route("/auth/logout")
def logout():
    """Clear the session and redirect to login."""
    email = session.pop("user_email", None)
    session.clear()
    logger.info("[Auth] Logout | user=%s", email)
    flash("You have been signed out.", "info")
    return redirect(url_for("login_page"))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    """Render the main chat interface."""
    conv_id = session.get("conversation_id")
    if not conv_id or conv_id not in conversations:
        conv_id, _ = get_or_create_conversation()
        session["conversation_id"] = conv_id
    return render_template(
        "index.html",
        conversation_id=conv_id,
        conversations=conversations,
        user_email=session.get("user_email", ""),
    )


@app.route("/api/chat", methods=["POST"])
def chat():
    """Handle a chat message — non-streaming (returns full response)."""
    data = request.get_json(force=True)
    user_message = data.get("message", "").strip()
    conv_id = data.get("conversation_id") or session.get("conversation_id")

    if not user_message:
        logger.warning("[/api/chat] Empty message received")
        return jsonify({"error": "Empty message"}), 400

    conv_id, history = get_or_create_conversation(conv_id)
    session["conversation_id"] = conv_id
    logger.info("[/api/chat] Message received | conv=%s | len=%d", conv_id, len(user_message))

    # Append user message
    history.append({
        "role": "user",
        "content": user_message,
        "timestamp": datetime.utcnow().isoformat(),
    })

    # Query SAGE
    sage_messages = _build_sage_messages(history)
    answer = query_sage(sage_messages, session_id=conv_id)

    # Append assistant message
    history.append({
        "role": "assistant",
        "content": answer,
        "timestamp": datetime.utcnow().isoformat(),
    })

    logger.info("[/api/chat] Response sent | conv=%s", conv_id)
    return jsonify({
        "conversation_id": conv_id,
        "message": answer,
    })


@app.route("/api/chat/stream", methods=["POST"])
def chat_stream():
    """Handle a chat message — streaming SSE (tokens arrive in real-time)."""
    data = request.get_json(force=True)
    user_message = data.get("message", "").strip()
    conv_id = data.get("conversation_id") or session.get("conversation_id")
    bot_type = data.get("bot_type", "")

    if not user_message:
        logger.warning("[/api/chat/stream] Empty message received")
        return jsonify({"error": "Empty message"}), 400

    conv_id, history = get_or_create_conversation(conv_id)
    session["conversation_id"] = conv_id
    logger.info("[/api/chat/stream] Stream request | conv=%s | bot=%s | len=%d", conv_id, bot_type, len(user_message))

    # Append user message
    history.append({
        "role": "user",
        "content": user_message,
        "timestamp": datetime.utcnow().isoformat(),
    })

    base_messages = _build_sage_messages(history)

    # RAG augmentation for the OneAI bot + strict grounding system prompt
    if bot_type == "oneai-default" and RAG_AVAILABLE:
        indexed_url = os.environ.get("RAG_START_URL", "the indexed website")
        strict_system = {
            "role": "system",
            "content": (
                f"You are OneAI, a knowledge assistant. "
                f"You have been given access ONLY to content crawled and indexed from: {indexed_url}. "
                "Your ONLY job is to answer questions using that indexed content. "
                "If a question cannot be answered from the indexed content, reply: "
                "'I can only answer questions about the indexed content. "
                "That information is not available in the knowledge base.' "
                "Do NOT answer from general knowledge. Do NOT speculate beyond what the documents say."
            ),
        }
        sage_messages = rag_pipeline.augment_messages(base_messages, user_message)
        # Prepend strict system prompt as first message
        sage_messages = [strict_system] + [m for m in sage_messages if m.get("role") != "system"]
        logger.info("[RAG] Augmented messages with KB context | conv=%s", conv_id)
    else:
        sage_messages = base_messages

    def generate():
        full_response = []
        for event in stream_sage(sage_messages, session_id=conv_id):
            yield event
            # Collect content for history
            if event.startswith("data: ") and not event.strip().endswith("[DONE]"):
                try:
                    chunk = json.loads(event[6:])
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        full_response.append(content)
                except (json.JSONDecodeError, IndexError, KeyError):
                    pass

        # After stream completes, save full response to history
        assembled = "".join(full_response)
        if assembled:
            history.append({
                "role": "assistant",
                "content": assembled,
                "timestamp": datetime.utcnow().isoformat(),
            })

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Conversation-Id": conv_id,
        },
    )


@app.route("/api/conversations", methods=["GET"])
def list_conversations():
    """Return all conversation summaries."""
    summaries = []
    for cid, msgs in conversations.items():
        title = msgs[0]["content"][:60] if msgs else "New conversation"
        summaries.append({
            "id": cid,
            "title": title,
            "message_count": len(msgs),
            "last_activity": msgs[-1]["timestamp"] if msgs else None,
        })
    # Most recent first
    summaries.sort(key=lambda s: s["last_activity"] or "", reverse=True)
    return jsonify(summaries)


@app.route("/api/conversations/<conv_id>", methods=["GET"])
def get_conversation(conv_id: str):
    """Retrieve full message history for a conversation."""
    if conv_id not in conversations:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "id": conv_id,
        "messages": conversations[conv_id],
    })


@app.route("/api/conversations", methods=["POST"])
def new_conversation():
    """Create a fresh conversation and return its id."""
    conv_id, _ = get_or_create_conversation()
    session["conversation_id"] = conv_id
    return jsonify({"conversation_id": conv_id})


@app.route("/api/ob4/chat", methods=["POST"])
def ob4_chat():
    """Proxy a chat message to the OB-4 / Onboard completion API."""
    if not OB4_URL or not OB4_TOKEN:
        logger.warning("[OB-4] Not configured — OB4_URL or OB4_TOKEN missing")
        return jsonify({"error": "OB-4 is not configured on this server."}), 503

    data = request.get_json(force=True)
    user_message = data.get("message", "").strip()
    conv_id = data.get("conversation_id") or session.get("conversation_id", "")

    if not conv_id or conv_id not in conversations:
        conv_id, _ = get_or_create_conversation(conv_id)

    if not user_message:
        return jsonify({"error": "Empty message"}), 400

    logger.info("[OB-4] Request | conv=%s | len=%d", conv_id, len(user_message))

    # 1. Append user message to the shared conversation memory
    conversations[conv_id].append({
        "role": "user",
        "content": user_message,
        "timestamp": datetime.utcnow().isoformat()
    })

    # 2. Extract conversation history for OB-4 (strip out OneAI's system prompt which is for SAGE)
    ob4_messages = [msg for msg in conversations[conv_id] if msg["role"] != "system"]

    try:
        resp = requests.post(
            OB4_URL,
            headers={
                "Content-Type": "application/json",
                "api-key": OB4_TOKEN,
            },
            json={
                "messages": ob4_messages,
                "email": OB4_EMAIL,
                "conversation_id": conv_id,
            },
            timeout=120,
            verify=OB4_VERIFY_SSL,
        )
        resp.raise_for_status()

        # 3. Parse the OB-4 response so we can save it to history
        resp_data = resp.json()
        reply_content = ""

        if "choices" in resp_data and len(resp_data["choices"]) > 0:
            choice = resp_data["choices"][0]
            if "messages" in choice:
                assistant_msg = next((m for m in choice["messages"] if m.get("role") == "assistant"), None)
                if assistant_msg:
                    reply_content = assistant_msg.get("content", "")
            elif "message" in choice:
                reply_content = choice["message"].get("content", "")

        if not reply_content:
            reply_content = str(resp_data)

        conversations[conv_id].append({
            "role": "assistant",
            "content": reply_content,
            "timestamp": datetime.utcnow().isoformat()
        })

        logger.info("[OB-4] Response OK | conv=%s | status=%s", conv_id, resp.status_code)
        return jsonify({"message": reply_content, "conversation_id": conv_id}), 200
    except requests.exceptions.ConnectionError as e:
        logger.error("[OB-4] ConnectionError | url=%s | %s", OB4_URL, e)
        return jsonify({"error": "Could not connect to OB-4 API."}), 502
    except requests.exceptions.Timeout:
        logger.error("[OB-4] Timeout after 120s | conv=%s", conv_id)
        return jsonify({"error": "OB-4 API timed out."}), 504
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 500
        body = {}
        try:
            body = e.response.json()
        except Exception:
            pass
        logger.error("[OB-4] HTTP %s | conv=%s | body=%s", status, conv_id, body)
        return jsonify({"error": f"OB-4 returned HTTP {status}", "detail": body}), status
    except Exception as e:
        logger.exception("[OB-4] Unexpected error | conv=%s", conv_id)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# RAG Routes
# ---------------------------------------------------------------------------
@app.route("/api/rag/status", methods=["GET"])
def rag_status():
    """Return current ingest state and KB chunk count."""
    if not RAG_AVAILABLE:
        return jsonify({"available": False, "status": "unavailable", "chunks": 0})
    with _ingest_lock:
        state = dict(_ingest_state)
    state["available"] = True
    state["chunks"] = rag_pipeline.kb_count()
    return jsonify(state)


@app.route("/api/rag/ingest", methods=["POST"])
def rag_ingest():
    """Start a background crawl-and-index job for a user-supplied URL."""
    if not RAG_AVAILABLE:
        return jsonify({"error": "RAG dependencies not installed."}), 503

    with _ingest_lock:
        if _ingest_state.get("status") == "running":
            return jsonify({"error": "Ingest already running."}), 409

    req_data = request.get_json(force=True, silent=True) or {}
    max_pages = int(req_data.get("max_pages", os.environ.get("RAG_MAX_PAGES", 40)))
    max_pages = min(max_pages, 50)  # hard cap at 50

    # Allow user-supplied URL; fall back to env default
    site_url = req_data.get("url", "").strip()
    if site_url:
        # Persist for the current process so the pipeline uses it
        os.environ["RAG_START_URL"] = site_url
        logger.info("[RAG] User-supplied URL: %s | max_pages=%d", site_url, max_pages)
    else:
        logger.info("[RAG] Using default RAG_START_URL | max_pages=%d", max_pages)

    thread = threading.Thread(target=_run_ingest, args=(max_pages,), daemon=True)
    thread.start()
    logger.info("[RAG] Ingest thread started | max_pages=%d", max_pages)
    return jsonify({"status": "started", "max_pages": max_pages, "url": site_url or os.environ.get("RAG_START_URL", "")}), 202


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, port=5000)
