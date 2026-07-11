import { getApiUserProfile } from "@/lib/db";

export async function GET(
  req: Request,
  { params }: { params: Promise<{ username: string }> }
) {
  const { username } = await params;
  const { searchParams } = new URL(req.url);
  let profile;
  try {
    profile = getApiUserProfile(username, searchParams.get("origin") || "human_direct");
  } catch (error) {
    if (error instanceof RangeError) {
      return Response.json({ error: error.message }, { status: 400 });
    }
    throw error;
  }
  if (!profile || !profile.user) {
    return Response.json({ error: "not found" }, { status: 404 });
  }

  return Response.json({
    user: {
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
