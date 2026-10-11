"""End-to-end verification of token-metered chat against a stub Groq.

The stub stands in for Groq Cloud so the suite needs no API key and no network:
it rate-limits the first model in the chain, serves the second, and can be
re-scripted per model. The backend under test is a real uvicorn process on its
own SQLite database, so every check goes over HTTP exactly like the frontend.

Covers:
  1.  cost math and message normalization (unit)
  2.  happy path: AI SDK UI-message stream frames, text, and [DONE]
  3.  fallback: the rate-limited model is skipped, the next one serves, and the
      ledger records which model answered
  4.  metering: credits charged equal the reported usage, not the reservation
  5.  partial settle: the unused part of the reservation returns to available,
      with release + commit ledger rows
  6.  no usage reported -> charge derived from an estimate
  7.  overspend (402), expired credits (403), bad bearer (401), bad body (422)
  8.  whole chain rate-limited -> 5xx, reservation released
  9.  client hangs up mid-stream -> delivered text is still charged
 10.  chat disabled without a provider key -> 503

Run from the repo root:  .venv/bin/python scripts/verify_chat.py
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
sys.path.insert(0, str(REPO_ROOT))

from miniLLMBE import llm  # noqa: E402

BASE = "http://127.0.0.1:8123"  # backend under test
DISABLED_BASE = "http://127.0.0.1:8124"  # second backend, no provider key
FAILURES: list[str] = []
CHECKS = 0

MODEL_A = "qwen/qwen3.8-27b"
MODEL_B = "openai/gpt-oss-120b"
MODEL_C = "openai/gpt-oss-20b"
USAGE_840_400 = {"prompt_tokens": 840, "completion_tokens": 400, "total_tokens": 1240}


# --- stub upstream ---------------------------------------------------------

# model -> {"status", "deltas", "usage", "delay"}; an unscripted model 429s,
# which is the Groq rate-limit behaviour the fallback chain exists for.
STUB_SCRIPT: dict[str, dict] = {}
STUB_CALLS: list[dict] = []
STUB_LOCK = threading.Lock()


def script_model(model: str, **script) -> None:
    with STUB_LOCK:
        STUB_SCRIPT[model] = script


def calls_for(model: str) -> int:
    with STUB_LOCK:
        return sum(1 for call in STUB_CALLS if call.get("model") == model)


def clear_calls() -> None:
    with STUB_LOCK:
        STUB_CALLS.clear()


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # silent
        pass

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}

        with STUB_LOCK:
            STUB_CALLS.append(
                {
                    "model": body.get("model"),
                    "stream_options": body.get("stream_options"),
                    "max_tokens": body.get("max_tokens"),
                }
            )
            script = STUB_SCRIPT.get(body.get("model", ""), {"status": 429})

        if script.get("status", 200) != 200:
            payload = json.dumps(
                {"error": {"message": "rate limit reached", "type": "rate_limit_exceeded"}}
            ).encode()
            self.send_response(script["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        for piece in script.get("deltas", ["Hello", " world"]):
            frame = {
                "choices": [{"index": 0, "delta": {"content": piece}}]
            }
            self._write_chunk(f"data: {json.dumps(frame)}\n\n".encode())
            if script.get("delay"):
                time.sleep(script["delay"])

        if script.get("usage") is not None:
            frame = {"choices": [], "usage": script["usage"]}
            self._write_chunk(f"data: {json.dumps(frame)}\n\n".encode())

        self._write_chunk(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _write_chunk(self, payload: bytes) -> None:
        self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
        self.wfile.flush()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --- helpers ---------------------------------------------------------------


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f" -> {detail}" if detail else ""))
        FAILURES.append(label)


def section(title: str) -> None:
    print(f"\n{title}")


def create_key(base: str, name: str, credits: int | None = None, ttl_days: int | None = None):
    body: dict = {"name": name}
    if credits is not None:
        body["credits"] = credits
    if ttl_days is not None:
        body["ttl_days"] = ttl_days
    response = httpx.post(f"{base}/keys", json=body, timeout=30)
    if response.status_code != 201:
        raise SystemExit(f"key creation failed: {response.status_code} {response.text}")
    return response.json()


def balance(base: str, key_id: int) -> dict:
    return httpx.get(f"{base}/keys/{key_id}/credits", timeout=30).json()


def ledger(base: str, key_id: int) -> list[dict]:
    return httpx.get(f"{base}/keys/{key_id}/credits/ledger", timeout=30).json()


def chat_raw(key: str, messages: list[dict], timeout: float = 30) -> httpx.Response:
    return httpx.post(
        f"{BASE}/chat",
        json={"messages": messages},
        headers={"Authorization": f"Bearer {key}"},
        timeout=timeout,
    )


def parse_sse(text: str) -> list[dict | str]:
    """UI-message frames in order; the terminal [DONE] as the string."""
    frames: list[dict | str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        frames.append("[DONE]" if data == "[DONE]" else json.loads(data))
    return frames


def text_from(frames: list[dict | str]) -> str:
    return "".join(
        frame["delta"]
        for frame in frames
        if isinstance(frame, dict) and frame.get("type") == "text-delta"
    )


# --- checks ----------------------------------------------------------------


def check_cost_math() -> None:
    section("1. cost math and normalization")

    check("estimate_tokens counts ~4 chars/token", llm.estimate_tokens("abcd") == 1)
    check("estimate_tokens handles empty", llm.estimate_tokens("") == 0)
    check(
        "credits_for_tokens rounds up",
        llm.credits_for_tokens(1) == 1
        and llm.credits_for_tokens(1000) == 1
        and llm.credits_for_tokens(1001) == 2
        and llm.credits_for_tokens(2500) == 3,
        str([llm.credits_for_tokens(n) for n in (1, 1000, 1001, 2500)]),
    )
    check(
        "estimate_charge bounds the completion by max_tokens",
        llm.estimate_charge([{"role": "user", "content": "abcd"}], max_tokens=4096) == 5,
        str(llm.estimate_charge([{"role": "user", "content": "abcd"}], max_tokens=4096)),
    )
    check(
        "estimate_charge scales with credits_per_1k_tokens",
        llm.estimate_charge([{"role": "user", "content": "abcd"}], max_tokens=1000, per_1k=3)
        == 4,  # 1 + 1000 tokens -> ceil(1001/1000 * 3) = 4
        str(llm.estimate_charge([{"role": "user", "content": "abcd"}], max_tokens=1000, per_1k=3)),
    )

    normalized = llm.normalize_messages(
        [
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "extra keys ignored", "id": "x"},
            {"role": "user", "parts": [{"type": "text", "text": "from parts"}]},
            {"role": "user", "content": [{"type": "image", "url": "x"}, {"text": "kept"}]},
            {"role": "user", "content": [{"type": "image", "url": "x"}]},
            {"role": "tool", "content": "dropped role"},
        ]
    )
    check(
        "normalize keeps text, drops non-text parts and unknown roles",
        [m["content"] for m in normalized]
        == ["hi", "extra keys ignored", "from parts", "kept"],
        str(normalized),
    )
    check(
        "normalize prepends the system prompt",
        llm.normalize_messages([{"role": "user", "content": "hi"}], system_prompt="SYS")[0]
        == {"role": "system", "content": "SYS"},
    )
    try:
        llm.normalize_messages([{"role": "user", "content": "   "}])
        check("normalize rejects an empty conversation", False, "no error raised")
    except ValueError:
        check("normalize rejects an empty conversation", True)


def check_stream_shape() -> None:
    section("2. happy path: stream shape")

    script_model(MODEL_A, status=429)
    script_model(
        MODEL_B,
        deltas=["Hel", "lo ", "there"],
        usage={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    )
    clear_calls()

    key = create_key(BASE, "chat-shape", credits=50, ttl_days=1)
    response = chat_raw(key["plaintext_key"], [{"role": "user", "content": "hi"}])
    check("chat returns 200", response.status_code == 200, response.text[:200])
    check(
        "content type is SSE",
        response.headers.get("content-type", "").startswith("text/event-stream"),
        response.headers.get("content-type", ""),
    )

    frames = parse_sse(response.text)
    types = [f.get("type") if isinstance(f, dict) else f for f in frames]
    check("stream ends with [DONE]", types[-1] == "[DONE]", str(types))
    check(
        "frame order matches the UI-message protocol",
        types[0] == "start"
        and types[1] == "start-step"
        and types[2] == "text-start"
        and "text-delta" in types
        and types[-4:] == ["text-end", "finish-step", "finish", "[DONE]"],
        str(types),
    )
    check("answer text assembled from deltas", text_from(frames) == "Hello there", text_from(frames))
    check(
        "the first frame identifies the message and its text part",
        isinstance(frames[0], dict)
        and bool(frames[0].get("messageId"))
        and isinstance(frames[2], dict)
        and frames[2].get("id") == frames[0].get("messageId"),
        str(frames[:3]),
    )
    check("the upstream was asked for usage", calls_for(MODEL_B) == 1, str(STUB_CALLS))
    with STUB_LOCK:
        options = next(c["stream_options"] for c in STUB_CALLS if c["model"] == MODEL_B)
    check("stream_options include_usage is set", options == {"include_usage": True}, str(options))


def check_fallback() -> None:
    section("3. fallback across the model chain")

    script_model(MODEL_A, status=429)
    script_model(MODEL_B, deltas=["fallback", " answer"], usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    script_model(MODEL_C, deltas=["never", " used"])
    clear_calls()

    key = create_key(BASE, "chat-fallback", credits=50, ttl_days=1)
    response = chat_raw(key["plaintext_key"], [{"role": "user", "content": "hi"}])
    check("request succeeded despite the rate limit", response.status_code == 200, response.text[:200])
    check("answer came from the second model", text_from(parse_sse(response.text)) == "fallback answer")
    check("the rate-limited model was tried once, not retried", calls_for(MODEL_A) == 1, str(calls_for(MODEL_A)))
    check("the third model was never reached", calls_for(MODEL_C) == 0)
    references = [entry.get("reference") for entry in ledger(BASE, key["key"]["id"])]
    check(
        "the ledger records the model that served the request",
        f"chat:{MODEL_B}" in references,
        str(references),
    )


def check_metering() -> None:
    section("4. metering: charge equals reported usage")

    script_model(MODEL_A, status=429)
    script_model(MODEL_B, deltas=["metered"], usage=USAGE_840_400)

    key = create_key(BASE, "chat-metered", credits=10, ttl_days=1)
    before = balance(BASE, key["key"]["id"])
    response = chat_raw(key["plaintext_key"], [{"role": "user", "content": "hi"}])
    check("metered chat returns 200", response.status_code == 200, response.text[:200])

    after = balance(BASE, key["key"]["id"])
    check(
        "exactly 2 credits spent (1240 tokens, reserved 5)",
        after["spent"] - before["spent"] == 2,
        f"spent {before['spent']} -> {after['spent']}",
    )
    check(
        "available dropped by exactly 2",
        before["available"] - after["available"] == 2,
        f"available {before['available']} -> {after['available']}",
    )
    check("no credits left reserved", after["reserved"] == 0, str(after))

    kinds = [(e["kind"], e["amount"]) for e in ledger(BASE, key["key"]["id"])]
    check("ledger has a commit of 2", ("commit", 2) in kinds, str(kinds))
    check("ledger releases the unused reservation (5 - 2 = 3)", ("release", 3) in kinds, str(kinds))
    check(
        "no reservation left pending to reclaim",
        httpx.post(f"{BASE}/credits/reclaim-expired", timeout=30).json()["reclaimed"] == 0,
    )


def check_estimated_usage() -> None:
    section("5. provider reports no usage -> charge is estimated")

    script_model(MODEL_A, status=200, deltas=["x" * 400])  # no usage key
    script_model(MODEL_B, deltas=["unused"])

    key = create_key(BASE, "chat-estimated", credits=50, ttl_days=1)
    response = chat_raw(key["plaintext_key"], [{"role": "user", "content": "hi"}])
    check("chat without usage still returns 200", response.status_code == 200, response.text[:200])

    after = balance(BASE, key["key"]["id"])
    # prompt estimate 1 token + 400 chars -> 100 tokens -> 101 -> 1 credit.
    check("estimated charge is 1 credit", after["spent"] == 1, str(after))
    check("reservation released", after["reserved"] == 0, str(after))

    script_model(MODEL_A, status=429)  # restore the chain


def check_rejections() -> None:
    section("6. rejections charge nothing")

    script_model(MODEL_A, status=429)
    script_model(MODEL_B, deltas=["never"], usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    clear_calls()

    broke = create_key(BASE, "chat-broke")  # no grant: a 0-credit key
    response = chat_raw(broke["plaintext_key"], [{"role": "user", "content": "hi"}])
    check("no credits -> 402", response.status_code == 402, str(response.status_code))
    check("nothing was charged", balance(BASE, broke["key"]["id"])["spent"] == 0)
    check("the model was never called", len(STUB_CALLS) == 0, str(STUB_CALLS))

    check("invalid key -> 401", chat_raw("mnl_not-a-key", [{"role": "user", "content": "hi"}]).status_code == 401)
    check(
        "missing Authorization -> 401",
        httpx.post(f"{BASE}/chat", json={"messages": [{"role": "user", "content": "hi"}]}, timeout=30).status_code
        == 401,
    )
    check("empty message -> 422", chat_raw(broke["plaintext_key"], [{"role": "user", "content": "   "}]).status_code == 422)
    check("oversized message -> 422", chat_raw(broke["plaintext_key"], [{"role": "user", "content": "x" * 20000}]).status_code == 422)

    # Expiry cannot be set through the API (a past expiry is rejected), so
    # backdate the stored expiry the way a real TTL elapse would.
    expired = create_key(BASE, "chat-expired", credits=50, ttl_days=1)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "update api_keys set credits_expire_at = datetime('now', '-1 hour') where id = ?",
            (expired["key"]["id"],),
        )
    response = chat_raw(expired["plaintext_key"], [{"role": "user", "content": "hi"}])
    check("expired credits -> 403", response.status_code == 403, str(response.status_code))
    check("expired key was not charged", balance(BASE, expired["key"]["id"])["spent"] == 0)


def check_chain_exhausted() -> None:
    section("7. whole chain rate-limited")

    script_model(MODEL_A, status=429)
    script_model(MODEL_B, status=429)
    script_model(MODEL_C, status=429)
    clear_calls()

    key = create_key(BASE, "chat-chain", credits=50, ttl_days=1)
    before = balance(BASE, key["key"]["id"])
    response = chat_raw(key["plaintext_key"], [{"role": "user", "content": "hi"}], timeout=60)
    check("exhausted chain returns a server error", response.status_code in (429, 502, 503), str(response.status_code))

    after = balance(BASE, key["key"]["id"])
    check("reservation released after the chain failed", after["reserved"] == 0, str(after))
    check("nothing charged", after["spent"] == before["spent"], f"{before} -> {after}")
    check(
        "every model in the chain was tried exactly once",
        calls_for(MODEL_A) == 1 and calls_for(MODEL_B) == 1 and calls_for(MODEL_C) == 1,
        str(STUB_CALLS),
    )


def check_disconnect() -> None:
    section("8. client hangs up mid-stream")

    script_model(
        MODEL_A,
        status=200,
        deltas=list("ABCDEFGH"),
        usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        delay=0.05,
    )
    key = create_key(BASE, "chat-disconnect", credits=50, ttl_days=1)
    before = balance(BASE, key["key"]["id"])

    with httpx.Client(timeout=30) as client:
        with client.stream(
            "POST",
            f"{BASE}/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {key['plaintext_key']}"},
        ) as response:
            seen = 0
            for _ in response.iter_lines():
                seen += 1
                if seen >= 3:  # bail after the opening frames
                    break

    time.sleep(0.8)  # let the server notice the disconnect and settle
    after = balance(BASE, key["key"]["id"])
    check("delivered text was charged despite the hang-up", after["spent"] >= 1, str(after))
    check("reservation released", after["reserved"] == 0, str(after))
    script_model(MODEL_A, status=429)


def check_disabled(process: subprocess.Popen) -> None:
    section("9. chat disabled without a provider key")

    key = create_key(DISABLED_BASE, "chat-disabled", credits=50, ttl_days=1)
    response = httpx.post(
        f"{DISABLED_BASE}/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {key['plaintext_key']}"},
        timeout=30,
    )
    check("no provider key -> 503", response.status_code == 503, str(response.status_code))
    check("nothing charged", balance(DISABLED_BASE, key["key"]["id"])["spent"] == 0)


# --- harness ---------------------------------------------------------------


def wait_for_backend(base: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise SystemExit(f"backend on {base} did not come up in time")


def spawn_backend(port: int, database_path: str, groq_api_key: str, groq_base_url: str) -> subprocess.Popen:
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{database_path}",
        "GROQ_API_KEY": groq_api_key,
        "GROQ_BASE_URL": groq_base_url,
        "LLM_MODEL_CHAIN": f"{MODEL_A},{MODEL_B},{MODEL_C}",
        "LLM_MAX_TOKENS": "4096",
        "CREDITS_PER_1K_TOKENS": "1",
        "LLM_MAX_MESSAGE_CHARS": "8000",
    }
    return subprocess.Popen(
        [str(PYTHON), "-m", "uvicorn", "miniLLMBE.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def main() -> int:
    global DB_PATH

    stub_port = free_port()
    stub = ThreadingHTTPServer(("127.0.0.1", stub_port), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    stub_url = f"http://127.0.0.1:{stub_port}/openai/v1"

    with tempfile.TemporaryDirectory() as tmp:
        DB_PATH = str(Path(tmp) / "verify_chat.db")
        disabled_path = str(Path(tmp) / "verify_chat_disabled.db")

        backend = spawn_backend(8123, DB_PATH, "stub-key", stub_url)
        disabled = spawn_backend(8124, disabled_path, "", stub_url)
        try:
            wait_for_backend(BASE)
            wait_for_backend(DISABLED_BASE)
            check_cost_math()
            check_stream_shape()
            check_fallback()
            check_metering()
            check_estimated_usage()
            check_rejections()
            check_chain_exhausted()
            check_disconnect()
            check_disabled(disabled)
        finally:
            for process in (backend, disabled):
                process.terminate()
                try:
                    _, stderr = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _, stderr = process.communicate()
                stderr = (stderr or b"").decode("utf-8", "replace")
                if "Traceback" in stderr:
                    print("\n  FAIL backend logged a traceback:")
                    print("\n".join(stderr.strip().splitlines()[-12:]))
                    FAILURES.append(f"backend traceback (port {process.args[process.args.index('--port') + 1]})")
            stub.shutdown()

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("metering under test: 1 credit per 1K tokens, 5-credit reservation bound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
