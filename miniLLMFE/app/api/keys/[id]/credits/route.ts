const BACKEND = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

type Ctx = { params: Promise<{ id: string }> };

async function readJson(res: Response) {
  return res.json().catch(() => ({}));
}

export async function GET(_: Request, { params }: Ctx) {
  const { id } = await params;
  const res = await fetch(`${BACKEND}/keys/${id}/credits`, { cache: "no-store" });
  return Response.json(await readJson(res), { status: res.status });
}

export async function POST(req: Request, { params }: Ctx) {
  const { id } = await params;
  const body = await req.json();
  const res = await fetch(`${BACKEND}/keys/${id}/credits`, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return Response.json(await readJson(res), { status: res.status });
}
