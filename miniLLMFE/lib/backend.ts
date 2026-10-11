/**
 * Single point of contact between the frontend and the FastAPI backend.
 *
 * Every /api/* route proxies to BACKEND_URL. Two rules live here so no route
 * has to remember them:
 *
 * 1. A missing or unreachable backend produces a 503 with a readable message.
 *    Without this, an unset BACKEND_URL falls back to 127.0.0.1:8000, the
 *    fetch throws, and Next.js answers with an opaque 500 that looks like an
 *    application bug rather than missing configuration.
 *
 * 2. Upstream error text is not echoed verbatim to the browser. The backend
 *    already maps its own errors to client-safe messages, but a proxy should
 *    not be the thing that leaks an internal hostname or stack trace.
 */

/** Resolved backend origin, or null when it is not configured. */
export function backendOrigin(): string | null {
  const url = process.env.BACKEND_URL?.trim();
  return url ? url.replace(/\/+$/, "") : null;
}

/** Body returned when the backend cannot be reached at all. */
export function backendUnavailable(cause?: unknown): Response {
  const configured = backendOrigin() !== null;
  if (!configured) {
    console.error("[api] BACKEND_URL is not set — cannot proxy to the backend.");
  } else {
    // Logged, not returned: the origin can be an internal hostname.
    console.error("[api] backend unreachable:", cause);
  }
  return Response.json(
    {
      detail: configured
        ? "The miniLLM backend is unreachable. Please try again shortly."
        : "The server is not configured with a BACKEND_URL.",
    },
    { status: 503 },
  );
}

/**
 * Marks a request as coming from this proxy.
 *
 * `cache: "no-store"` below is deliberate — it stops Vercel and nginx from
 * buffering or storing responses. But it makes the fetch implementation send
 * `Cache-Control: no-cache` upstream, which the backend reads as a *client*
 * opting out of its response cache. That silently disabled caching for every
 * request arriving through the deployed frontend, while direct API calls still
 * hit it — a bug visible only in the deployed topology.
 *
 * The backend ignores cache opt-outs from requests carrying this header.
 */
const PROXY_TAG = "x-minillm-proxy";

/**
 * Call the backend, translating a transport failure into a 503.
 *
 * Returns the raw `Response` so callers can stream it (chat) or read it as
 * JSON (everything else).
 */
export async function callBackend(path: string, init: RequestInit = {}): Promise<Response> {
  const origin = backendOrigin();
  if (!origin) return backendUnavailable();

  try {
    return await fetch(`${origin}${path}`, {
      ...init,
      cache: "no-store",
      headers: { [PROXY_TAG]: "1", ...init.headers },
    });
  } catch (cause) {
    return backendUnavailable(cause);
  }
}

/**
 * Proxy a JSON request to the backend, preserving its status code.
 *
 * A 204 is passed through as an empty body. Any other response is parsed as
 * JSON; a body that will not parse degrades to `fallback` rather than throwing,
 * so a proxying bug cannot turn a backend error into a frontend crash.
 */
export async function proxyJson(
  path: string,
  init: RequestInit = {},
  fallback: unknown = null,
): Promise<Response> {
  const res = await callBackend(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init.headers },
  });

  if (res.status === 204) return new Response(null, { status: 204 });

  const data = await res.json().catch(() => fallback);
  return Response.json(data, { status: res.status });
}
