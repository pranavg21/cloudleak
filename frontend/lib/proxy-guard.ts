import "server-only";

// The Next.js proxy uses one backend key shared by every browser session.
// Until it has real user identity and quotas, it is for local development only.
export function proxyGuard(): Response | null {
  if (process.env.NODE_ENV === "production") {
    return Response.json(
      { detail: "Public uploads are disabled. Run CloudLeak locally or add user authentication before deployment." },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
  return null;
}
