import {
  getUserBySlug,
  getUserSummary,
  getUserActivity,
  getUserSourceBreakdown,
  getUserTopTools,
  getUserModels,
  getUserRecentSessions,
  isProfileDirectlyVisible,
} from "@/lib/db";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ slug: string }> }
) {
  const { slug } = await params;
  const user = getUserBySlug(slug);
  if (!user || !isProfileDirectlyVisible(user)) {
    return Response.json({ error: "not found" }, { status: 404 });
  }

  const summary = getUserSummary(user.slug);
  const activity = getUserActivity(user.slug, 60);
  const sources = getUserSourceBreakdown(user.slug);
  const topTools = getUserTopTools(user.slug, 12);
  const models = getUserModels(user.slug, 8);
  const recentSessions = getUserRecentSessions(user.slug, 12);

  return Response.json({
    user: {
      slug: user.slug,
      username: user.username,
      display_name: user.display_name ?? user.username,
      bio: user.bio,
    },
    summary,
    activity: {
      labels: activity.map((r) => r.day),
      messages: activity.map((r) => r.messages),
      tool_calls: activity.map((r) => r.tool_calls),
    },
    sources,
    top_tools: topTools,
    models,
    recent_sessions: recentSessions,
  });
}
