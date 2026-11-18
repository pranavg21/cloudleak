import { API_BASE, authHeaders, passthrough, unreachable } from "@/lib/server-api";
import { proxyGuard } from "@/lib/proxy-guard";

export const runtime = "nodejs";
export const maxDuration = 60;

// A raw multipart request is buffered by formData(), so enforce the byte limit
// on the incoming stream before invoking the multipart parser.
const MAX_REQUEST_BYTES = 21 * 1024 * 1024;

export async function POST(request: Request): Promise<Response> {
  const blocked = proxyGuard();
  if (blocked) return blocked;

  const contentType = request.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().startsWith("multipart/form-data;")) {
    return Response.json({ detail: "Expected a multipart CSV upload." }, { status: 415 });
  }
  const length = Number(request.headers.get("content-length") ?? "0");
  if (Number.isFinite(length) && length > MAX_REQUEST_BYTES) {
    return Response.json({ detail: "Upload exceeds the 20 MB limit." }, { status: 413 });
  }
  if (!request.body) {
    return Response.json({ detail: "No file was included in the upload." }, { status: 400 });
  }

  const reader = request.body.getReader();
  const chunks: BlobPart[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_REQUEST_BYTES) {
        await reader.cancel();
        return Response.json({ detail: "Upload exceeds the 20 MB limit." }, { status: 413 });
      }
      chunks.push(new Uint8Array(value).buffer as ArrayBuffer);
    }
  } catch {
    return Response.json({ detail: "Could not read the uploaded file." }, { status: 400 });
  }

  let file: FormDataEntryValue | null;
  try {
    const form = await new Response(new Blob(chunks), {
      headers: { "content-type": contentType },
    }).formData();
    file = form.get("file");
  } catch {
    return Response.json({ detail: "Invalid multipart upload." }, { status: 400 });
  }
  if (!(file instanceof File)) {
    return Response.json({ detail: "No file was included in the upload." }, { status: 400 });
  }
  if (!/\.(csv|txt)$/i.test(file.name) || file.size === 0) {
    return Response.json({ detail: "Choose a non-empty CSV billing export." }, { status: 400 });
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