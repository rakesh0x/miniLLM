import { proxyJson } from "@/lib/backend";

export const dynamic = "force-dynamic";

export async function GET() {
  return proxyJson("/keys", {}, null);
}

export async function POST(req: Request) {
  const body = await req.json();
  return proxyJson("/keys", { method: "POST", body: JSON.stringify(body) }, null);
}
