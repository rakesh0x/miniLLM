export const dynamic = "force-dynamic";

const BACKEND = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

/**
 * Passthrough to the backend's metered chat endpoint.
 *
 * The browser sends the AI SDK chat body plus the selected key's bearer token;
 * the backend streams the AI SDK v7 UI-message stream straight back, so this
 * route only carries bytes, status and headers — nothing is parsed or
 * re-shaped here.
 */
export async function POST(req: Request) {
  const authorization = req.headers.get("authorization");

  const body = await req.text();

  if (!authorization) {
    console.log("Auth headers missing")
  } else {
    console.log("Auth headers in present")
  }

  const res = await fetch(`${BACKEND}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...(authorization ? { authorization } : {}),
    },
    body,
    cache: "no-store",
  });

  if (!res.ok || !res.body) {
    // Failures before the first token (402/403/429/503) arrive as JSON with a
    // real status, which is what surfaces the error in the chat UI.
    const detail = await res.text().catch(() => "");
    return new Response(detail || JSON.stringify({ detail: "upstream error" }), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  }

  return new Response(res.body, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
      "X-Vercel-AI-UI-Message-Stream": res.headers.get("X-Vercel-AI-UI-Message-Stream") ?? "v1",
    },
  });
}
