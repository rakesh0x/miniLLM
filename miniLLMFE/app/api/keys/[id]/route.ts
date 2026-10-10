export async function DELETE(_: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const res = await fetch(`${process.env.BACKEND_URL ?? "http://127.0.0.1:8000"}/keys/${id}`, {
    method: "DELETE",
    cache: "no-store",
  });
  return new Response(null, { status: res.ok ? 204 : res.status });
}
