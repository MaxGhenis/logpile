import { getApiUserProfile } from "@/lib/db";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ slug: string }> }
) {
  const { slug } = await params;
  const profile = getApiUserProfile(slug);
  if (!profile || !profile.user) {
    return Response.json({ error: "not found" }, { status: 404 });
  }

  return Response.json({
    user: {
      slug: profile.user.slug,
      username: profile.user.username,
      display_name: profile.user.display_name ?? profile.user.username,
      bio: profile.user.bio,
      avatar_url: profile.user.avatar_url,
      profile_visibility: profile.user.profile_visibility,
      default_session_visibility: profile.user.default_session_visibility,
    },
    summary: profile.summary,
  });
}
