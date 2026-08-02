import { API_BASE, authHeaders, passthrough, unreachable } from "@/lib/server-api";

export const runtime = "nodejs";

export async function GET(
  _request: Request,
  { params }: { params: { jobId: string } },
): Promise<Response> {
  // Guard the path segment: the job id is opaque to us and must not be able
  // to walk out of the jobs collection.
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(params.jobId)) {
    return Response.json({ detail: "That job reference is not valid." }, { status: 400 });
  }

  try {
    const response = await fetch(`${API_BASE}/api/v1/audit/jobs/${params.jobId}`, {
      headers: authHeaders(),
      cache: "no-store",
    });
    return passthrough(response);
  } catch {
    return unreachable();
  }
}
