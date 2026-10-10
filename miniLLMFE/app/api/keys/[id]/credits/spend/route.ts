const BACKEND = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

type Ctx = { params: Promise<{ id: string }> };

export async function POST(req: Request, { params }: Ctx) {
  const { id } = await params;
  const body = await req.json().catch(() => ({}));
  const res = await fetch(`${BACKEND}/keys/${id}/credits/spend`, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return Response.json(await res.json().catch(() => ({})), { status: res.status });
}
