import { proxyJson } from "@/lib/backend";

export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ id: string }> };

export async function POST(req: Request, { params }: Ctx) {
  const { id } = await params;
  const body = await req.json().catch(() => ({}));
  return proxyJson(`/keys/${id}/credits/spend`, { method: "POST", body: JSON.stringify(body) }, {});
}
