import { proxyJson } from "@/lib/backend";

export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ id: string }> };

export async function GET(_: Request, { params }: Ctx) {
  const { id } = await params;
  return proxyJson(`/keys/${id}/credits/ledger`, {}, []);
}
