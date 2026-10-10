export const dynamic = "force-dynamic";

const BACKEND = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

async function proxy(path: string, init?: RequestInit) {
  const res = await fetch(`${BACKEND}${path}`, {
    ...init,
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (res.status === 204) return new Response(null, { status: 204 });
  const data = await res.json().catch(() => null);
  return Response.json(data, { status: res.status });
}

export async function GET() {
  return proxy("/keys");
}

export async function POST(req: Request) {
  const { name } = await req.json();
  return proxy("/keys", { method: "POST", body: JSON.stringify({ name }) });
}
