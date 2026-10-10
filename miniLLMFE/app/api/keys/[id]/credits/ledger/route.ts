const BACKEND = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

type Ctx = { params: Promise<{ id: string }> };

export async function GET(_: Request, { params }: Ctx) {
  const { id } = await params;
  const res = await fetch(`${BACKEND}/keys/${id}/credits/ledger`, { cache: "no-store" });
  return Response.json(await res.json().catch(() => []), { status: res.status });
}
