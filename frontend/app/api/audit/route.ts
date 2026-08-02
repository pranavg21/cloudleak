import { API_BASE, authHeaders, passthrough, unreachable } from "@/lib/server-api";

// Streaming the body straight through avoids buffering a large export twice.
export const runtime = "nodejs";
export const maxDuration = 60;

export async function POST(request: Request): Promise<Response> {
  const form = await request.formData();
  const file = form.get("file");

  if (!(file instanceof File)) {
    return Response.json({ detail: "No file was included in the upload." }, { status: 400 });
  }

  const upstream = new FormData();
  upstream.append("file", file, file.name);

  try {
    const response = await fetch(`${API_BASE}/api/v1/audit/upload`, {
      method: "POST",
      headers: authHeaders(),
      body: upstream,
      cache: "no-store",
    });
    return passthrough(response);
  } catch {
    return unreachable();
  }
}
