import "server-only";

/**
 * Server-only helpers for talking to the CloudLeak API.
 *
 * The API key lives here and never crosses to the browser. The client calls
 * this app's /api/audit routes; those routes attach the key and forward.
 */

export const API_BASE = process.env.CLOUDLEAK_API_BASE_URL ?? "http://localhost:8000";

export function authHeaders(): HeadersInit {
  const key = process.env.CLOUDLEAK_API_KEY;
  return key ? { Authorization: `Bearer ${key}` } : {};
}

/** Turn any upstream failure into a shape the client can render. */
export async function passthrough(response: Response): Promise<Response> {
  const body = await response.text();
  const headers = new Headers({ "Content-Type": "application/json" });

  const retryAfter = response.headers.get("Retry-After");
  if (retryAfter) headers.set("Retry-After", retryAfter);

  return new Response(body || JSON.stringify({ detail: "Empty response from the audit service." }), {
    status: response.status,
    headers,
  });
}

export function unreachable(): Response {
  return Response.json(
    {
      detail:
        "The audit service is not responding. Start the backend, then upload again.",
    },
    { status: 502 },
  );
}
