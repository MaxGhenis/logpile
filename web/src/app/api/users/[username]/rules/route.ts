import { getApiUserRules } from "@/lib/db";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ username: string }> }
) {
  const { username } = await params;
  const payload = getApiUserRules(username);
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
    rules: payload.rules,
  });
}
