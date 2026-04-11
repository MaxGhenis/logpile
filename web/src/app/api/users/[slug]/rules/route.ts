import { getApiUserRules } from "@/lib/db";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ slug: string }> }
) {
  const { slug } = await params;
  const payload = getApiUserRules(slug);
  if (!payload || !payload.user) {
    return Response.json({ error: "not found" }, { status: 404 });
  }

  return Response.json({
    user: {
      slug: payload.user.slug,
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
