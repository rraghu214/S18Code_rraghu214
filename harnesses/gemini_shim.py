"""Local shim: OpenAI-shaped /chat/completions -> GLC /v1/chat, forced provider=gemini.

Why this file exists (docs/s18_assignment.md §10b/10c): Qwen Code and DeepSeek
Harness each hold exactly one API key (no rotation), so pointing them straight
at Gemini gives them 1 key against our own harness's 5-key GLC pool -- a 5x
budget asymmetry that breaks §4's "same budget for all harnesses" claim.

Neither can call GLC's own /v1/chat directly either: both unconditionally
append `/chat/completions` to their base URL (confirmed in both repos'
source) and expect an OpenAI-shaped response, and a request with no
`provider` field would fall through to GLC's default LLM_ORDER routing --
given the live models map (nvidia/groq/cerebras/openrouter/ollama all serve a
different model entirely) that is a guarantee of silent model drift, not a
risk. Forcing "provider":"gemini" into every forwarded body is the
non-negotiable line in this file -- see docs/s18_assignment.md's "Stop and
ask me before... shipping the shim without the forced provider=gemini".

Deliberately not editing glc/routes/chat.py itself: CODEOWNERS-restricted,
needs instructor review.

Stdlib only, matching the rest of this repo's style (make_glc_llm in loop.py
also uses urllib rather than adding an httpx dependency just for S18Code).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GLC_URL = os.getenv("GLC_URL", "http://127.0.0.1:8111/v1/chat")
FORCE_PROVIDER = os.getenv("GLC_PROVIDER", "gemini")
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
PORT = int(os.getenv("S18_SHIM_PORT", "8877"))


def _extract_text(content) -> str:
    """OpenAI content is sometimes a list of blocks; GLC/Gemini want a string."""
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content or ""


def _flatten_tool(t: dict) -> dict:
    """OpenAI wire format nests each tool as {"type":"function","function":{name,
    description, parameters}} -- confirmed from a live GLC 422 ("tools.0.name:
    Field required") when the nested shape was forwarded as-is. GLC wants the
    inner {name, description, parameters} flat, matching what the DeepSeek
    Harness session log prints (its own internal shape), not what it puts on
    the wire."""
    fn = t.get("function", t)
    return {"name": fn.get("name"), "description": fn.get("description", ""), "parameters": fn.get("parameters", {})}


def _to_glc_body(openai_body: dict) -> dict:
    messages = openai_body.get("messages", [])
    system_text = "\n".join(
        _extract_text(m.get("content")) for m in messages if m.get("role") == "system"
    )
    non_system = [
        {"role": m.get("role"), "content": _extract_text(m.get("content"))}
        for m in messages
        if m.get("role") != "system"
    ]
    body = {
        "provider": FORCE_PROVIDER,  # non-negotiable, see module docstring
        "model": openai_body.get("model") or DEFAULT_MODEL,
        "messages": non_system,
        "system": system_text or None,
        "max_tokens": openai_body.get("max_tokens", 1024),
    }
    # Without this, GLC/Gemini is never told bash/str_replace_editor exist --
    # confirmed live: a request with no `tools` gets a plain-text reply asking
    # the user to paste error logs, since there's nothing to call. GLC accepts
    # OpenAI's flat {name, description, parameters} shape directly (checked
    # against a live GLC response returning a correct tool_calls entry).
    if openai_body.get("tools"):
        body["tools"] = [_flatten_tool(t) for t in openai_body["tools"]]
    return body


def _to_openai_stream_chunks(glc_body: dict, resp: dict) -> list[dict]:
    """DeepSeek Harness always sends stream=true and errors with
    'SSE stream ended without [DONE]' on a plain JSON response -- confirmed
    against a live request/response pair, not just docs. GLC itself isn't
    streaming, so this is a single-chunk fake-stream bridge: the full delta
    in one chunk, then a finish-reason-only chunk, matching OpenAI's
    chat.completion.chunk shape."""
    choice = resp["choices"][0]
    base = {"id": resp["id"], "object": "chat.completion.chunk", "created": resp["created"], "model": resp["model"]}
    delta = {"role": "assistant", "content": choice["message"]["content"]}
    if "tool_calls" in choice["message"]:
        delta["tool_calls"] = choice["message"]["tool_calls"]
    return [
        {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]},
    ]


def _to_openai_response(glc_body: dict) -> dict:
    """GLC's own shape (text/tool_calls/stop_reason/input_tokens/...) is not
    OpenAI's chat.completion shape -- checked directly against a live
    response, confirmed a reshape is required, not just a pass-through."""
    finish = "tool_calls" if glc_body.get("tool_calls") else "stop"
    message = {"role": "assistant", "content": glc_body.get("text", "")}
    if glc_body.get("tool_calls"):
        message["tool_calls"] = [
            {
                "id": tc.get("id", ""),
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": json.dumps(tc.get("arguments") or {}),
                },
            }
            for tc in glc_body["tool_calls"]
        ]
    return {
        "id": f"shim-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": glc_body.get("model", ""),
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": glc_body.get("input_tokens", 0),
            "completion_tokens": glc_body.get("output_tokens", 0),
            "total_tokens": glc_body.get("input_tokens", 0) + glc_body.get("output_tokens", 0),
        },
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # one line per request, printed explicitly below instead

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        openai_body = json.loads(self.rfile.read(length) or b"{}")
        streaming = bool(openai_body.get("stream"))
        glc_body = _to_glc_body(openai_body)

        req = urllib.request.Request(
            GLC_URL,
            data=json.dumps(glc_body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                glc_resp = json.loads(r.read())
            status, out = 200, _to_openai_response(glc_resp)
        except urllib.error.HTTPError as e:
            status, out = e.code, {"error": {"message": e.read().decode()[:500], "code": e.code}}
        except urllib.error.URLError as e:
            status, out = 502, {"error": {"message": f"shim: GLC unreachable: {e}", "code": 502}}

        if streaming and status == 200:
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for chunk in _to_openai_stream_chunks(glc_body, out):
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            body = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        print(f"[shim] {self.path} -> provider={glc_body['provider']} status={status}", flush=True)


def main() -> None:
    # 0.0.0.0, not just localhost: WSL2 has its own network namespace, so
    # DeepSeek Harness running inside it needs to reach this on the Windows
    # host's real IP, not 127.0.0.1 (docs/s18_assignment.md §10b/10c).
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[shim] listening on 0.0.0.0:{PORT}, forwarding to {GLC_URL}, provider forced to {FORCE_PROVIDER!r}")
    server.serve_forever()


if __name__ == "__main__":
    main()
