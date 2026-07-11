import { getApiUserSessions } from "@/lib/db";

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

export async function GET(
  req: Request,
  { params }: { params: Promise<{ username: string }> }
) {
  const { username } = await params;
  const url = new URL(req.url);
  let payload;
  try {
    const limit = parsePositiveInt(url.searchParams.get("limit"), 50, "limit");
    const offset = parsePositiveInt(url.searchParams.get("offset"), 0, "offset");
    payload = getApiUserSessions(username, {
      limit,
      offset,
      project: url.searchParams.get("project") || undefined,
      repo: url.searchParams.get("repo") || undefined,
      repoRoot: url.searchParams.get("repoRoot") || undefined,
      branch: url.searchParams.get("branch") || undefined,
      activity: url.searchParams.get("activity") || undefined,
      status: url.searchParams.get("status") || undefined,
      origin: url.searchParams.get("origin") || "human_direct",
      objective: url.searchParams.get("objective") || undefined,
      path: url.searchParams.get("path") || undefined,
    });
  } catch (error) {
    if (error instanceof RangeError) {
      return Response.json({ error: error.message }, { status: 400 });
    }
    throw error;
  }
  if (!payload || !payload.user) {
    return Response.json({ error: "not found" }, { status: 404 });
  }

  return Response.json({
    user: {
      username: payload.user.username,
      display_name: payload.user.display_name ?? payload.user.username,
      bio: payload.user.bio,
      avatar_url: payload.user.avatar_url,
      profile_visibility: payload.user.profile_visibility,
      default_session_visibility: payload.user.default_session_visibility,
    },
    total: payload.total,
    limit: payload.limit,
    offset: payload.offset,
    sessions: payload.sessions,
  });
}
