import { getApiSessionsFiltered } from "@/lib/db";

function parsePositiveInt(raw: string | null, fallback: number, name: string) {
  if (raw === null || raw === "") {
    return fallback;
  }
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed)) {
    throw new RangeError(`Invalid integer for '${name}'`);
  }
  return parsed;
}

export async function GET(req: Request) {
  const url = new URL(req.url);
  try {
    return Response.json(
      getApiSessionsFiltered({
        source: url.searchParams.get("source") || undefined,
        project: url.searchParams.get("project") || undefined,
        repo: url.searchParams.get("repo") || undefined,
        repoRoot: url.searchParams.get("repoRoot") || undefined,
        branch: url.searchParams.get("branch") || undefined,
        activity: url.searchParams.get("activity") || undefined,
        status: url.searchParams.get("status") || undefined,
        origin: url.searchParams.get("origin") || undefined,
        objective: url.searchParams.get("objective") || undefined,
        user: url.searchParams.get("user") || undefined,
        path: url.searchParams.get("path") || undefined,
        limit: parsePositiveInt(url.searchParams.get("limit"), 500, "limit"),
      })
    );
  } catch (error) {
    if (error instanceof RangeError) {
      return Response.json({ error: error.message }, { status: 400 });
    }
    throw error;
  }
}
