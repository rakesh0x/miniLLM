import { proxyJson } from "@/lib/backend";

export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ id: string }> };

export async function GET(_: Request, { params }: Ctx) {
  const { id } = await params;
  return proxyJson(`/keys/${id}/credits`, {}, null);
}

export async function POST(req: Request, { params }: Ctx) {
  const { id } = await params;
  const body = await req.json();
  return proxyJson(`/keys/${id}/credits`, { method: "POST", body: JSON.stringify(body) }, null);
}
