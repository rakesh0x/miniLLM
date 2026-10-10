export const dynamic = "force-dynamic";

async function sendQuery(query: string, messages: unknown[]) {
  const res = await fetch(`${process.env.BACKEND_URL ?? "http://127.0.0.1:8000"}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ query, messages }),
  });

  if (!res.ok || !res.body) {
    return new Response("upstream error", { status: 502 });
  }

  return new Response(res.body, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}

type MessagePart = { type: string; text?: string };
type ChatMessage = { role: string; content: string | MessagePart[] };

function extractQuery(messages: ChatMessage[]): string {
  const lastUser = [...messages].reverse().find((m) => m.role === "user");
  if (!lastUser) return "";
  if (typeof lastUser.content === "string") return lastUser.content;
  return lastUser.content
    .filter((p): p is MessagePart & { text: string } => p.type === "text" && !!p.text)
    .map((p) => p.text)
    .join("");
}

export async function POST(req: Request) {
  const { messages } = (await req.json()) as { messages: ChatMessage[] };
  return sendQuery(extractQuery(messages), messages);
}
