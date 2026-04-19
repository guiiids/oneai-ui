# SAGE Completions API — Integration Guide

SAGE exposes an **OpenAI-compatible Chat Completions endpoint** so any tool that can talk to OpenAI (LangChain, Open WebUI, LibreChat, n8n, custom scripts, etc.) can treat SAGE as a drop-in LLM backend — no custom client needed.

---

## Base URL

```
http://<your-sage-host>:<port>/v1
```

Examples:
- Local dev: `http://localhost:5001/v1`
- Production: `https://sage.yourdomain.com/v1`

---

## Authentication

If the `COMPLETIONS_API_KEY` environment variable is set on the server, every request must include:

```
Authorization: Bearer <COMPLETIONS_API_KEY>
```

If `COMPLETIONS_API_KEY` is **not** set, the endpoint is open (protection is delegated to the network/proxy layer).

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/models` | Model discovery — returns the `sage` model entry |
| `POST` | `/v1/chat/completions` | Main query endpoint (streaming + non-streaming) |

---

## POST /v1/chat/completions

### Request body

```json
{
  "model": "sage",
  "messages": [
    { "role": "user", "content": "What is the rotor seal part number for the 1260 Infinity II pump?" }
  ],
  "stream": false,
  "temperature": 0.7,
  "max_tokens": 1024,
  "user": "my-app-session-abc123"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `messages` | array | Yes | Standard OpenAI messages. SAGE reads the last `user` message as the query. |
| `model` | string | No | Any value works; SAGE always responds as `"sage"`. |
| `stream` | boolean | No | `false` (default) = wait for full response. `true` = SSE stream. |
| `temperature` | float | No | Passed through to the underlying Azure OpenAI call. |
| `max_tokens` | integer | No | Passed through to the underlying Azure OpenAI call. |
| `user` | string | No | **Session ID for multi-turn continuity.** Same value across calls = shared conversation history. Different/omitted value = fresh session. |

### Multi-part (multimodal) content

If `content` is an array instead of a string, SAGE concatenates all `text` parts:

```json
{
  "role": "user",
  "content": [
    { "type": "text", "text": "What does" },
    { "type": "text", "text": " part G1312-60010 do?" }
  ]
}
```

---

## Non-streaming response

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1713200000,
  "model": "sage",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "The rotor seal for the 1260 Infinity II pump is part number G1310-67300. [1]\n\n..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  }
}
```

> **Note:** Token counts are returned as 0. SAGE logs usage internally but does not expose it through this endpoint.

---

## Streaming response (SSE)

Set `"stream": true`. The response is a standard OpenAI SSE stream:

```
data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1713200000,"model":"sage","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1713200000,"model":"sage","choices":[{"index":0,"delta":{"content":"The rotor seal"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1713200000,"model":"sage","choices":[{"index":0,"delta":{"content":" for the 1260"},"finish_reason":null}]}

...

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1713200000,"model":"sage","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

Content-Type header: `text/event-stream`

---

## Examples

### curl — non-streaming

```bash
curl -s http://localhost:5001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "sage",
    "messages": [
      {"role": "user", "content": "What is the part number for the rotor seal on a 1260 Infinity II?"}
    ]
  }' | jq '.choices[0].message.content'
```

### curl — streaming

```bash
curl -s http://localhost:5001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "sage",
    "messages": [
      {"role": "user", "content": "Explain the HPLC pump priming procedure."}
    ],
    "stream": true
  }'
```

### Python (openai SDK)

The OpenAI Python SDK works without modification — just point `base_url` at SAGE:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:5001/v1",
    api_key="YOUR_API_KEY",   # or any non-empty string if no key is set
)

# Non-streaming
response = client.chat.completions.create(
    model="sage",
    messages=[
        {"role": "user", "content": "What is the rotor seal PN for the 1260 Infinity II pump?"}
    ],
)
print(response.choices[0].message.content)

# Streaming
stream = client.chat.completions.create(
    model="sage",
    messages=[
        {"role": "user", "content": "Walk me through the GC column installation procedure."}
    ],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta.content or ""
    print(delta, end="", flush=True)
```

### Python — multi-turn conversation

Use the same `user` value across calls to maintain context:

```python
SESSION_ID = "my-chatbot-user-42"

messages = []

def ask(question: str) -> str:
    messages.append({"role": "user", "content": question})
    response = client.chat.completions.create(
        model="sage",
        messages=messages,
        user=SESSION_ID,
    )
    answer = response.choices[0].message.content
    messages.append({"role": "assistant", "content": answer})
    return answer

print(ask("What pump is in the 1260 Infinity II?"))
print(ask("And what is the rotor seal part number for it?"))  # SAGE has context from turn 1
```

### LangChain

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="http://localhost:5001/v1",
    api_key="YOUR_API_KEY",
    model="sage",
    streaming=True,
)

response = llm.invoke("What is the recommended maintenance interval for the 1260 pump?")
print(response.content)
```

### n8n

In an **HTTP Request** node or **OpenAI Chat Model** node:
- **Base URL:** `http://<sage-host>/v1`
- **API Key:** your `COMPLETIONS_API_KEY`
- **Model:** `sage`

---

## Open WebUI / LibreChat configuration

In Open WebUI, add a custom OpenAI-compatible provider:

| Setting | Value |
|---|---|
| API Base URL | `http://<sage-host>/v1` |
| API Key | your `COMPLETIONS_API_KEY` (or any string if open) |
| Model | `sage` |

SAGE will appear as a selectable model in the UI.

---

## GET /v1/models

```bash
curl http://localhost:5001/v1/models \
  -H "Authorization: Bearer YOUR_API_KEY"
```

Response:

```json
{
  "object": "list",
  "data": [
    {
      "id": "sage",
      "object": "model",
      "created": 1700000000,
      "owned_by": "agilent"
    }
  ]
}
```

---

## Limitations

| Feature | Status |
|---|---|
| General RAG queries (Pipeline A) | Supported |
| Part-number lookup (Pipeline B / PNMaester) | **Not available** — use the SAGE web UI for PN queries |
| Token usage in response | Always 0 (logged server-side only) |
| Citations / source references | Included inline in `content` as `[N]` markers; not structured separately |
| Image / file input | Not supported |
| Function calling / tool use | Not supported |
| Embeddings endpoint | Not supported |

---

## Error responses

All errors follow the OpenAI error envelope:

```json
{
  "error": {
    "message": "Unauthorized — invalid API key",
    "type": "auth_error"
  }
}
```

| HTTP status | Scenario |
|---|---|
| `401` | Missing or invalid API key |
| `400` | Missing `messages` array or no `user` message found |
| `500` | SAGE internal error (check server logs) |
