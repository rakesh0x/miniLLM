import { callBackend } from "@/lib/backend";

export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ id: string }> };

export async function DELETE(_: Request, { params }: Ctx) {
  const { id } = await params;
  const res = await callBackend(`/keys/${id}`, { method: "DELETE" });
  return new Response(null, { status: res.ok ? 204 : res.status });
}
