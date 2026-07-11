import {
  getUserByUsername,
  getUserSummary,
  getUserActivity,
  getUserSourceBreakdown,
  getUserTopTools,
  getUserModels,
  getUserRecentSessions,
  isProfileDirectlyVisible,
} from "@/lib/db";

export async function GET(
  req: Request,
  { params }: { params: Promise<{ username: string }> }
) {
  const { username } = await params;
  const { searchParams } = new URL(req.url);
  const origin = searchParams.get("origin") || "human_direct";
  const user = getUserByUsername(username);
  if (!user || !isProfileDirectlyVisible(user)) {
    return Response.json({ error: "not found" }, { status: 404 });
  }

  let summary;
  let activity;
  let sources;
  let topTools;
  let models;
  let recentSessions;
  try {
    summary = getUserSummary(user.username, origin);
    activity = getUserActivity(user.username, 60, origin);
    sources = getUserSourceBreakdown(user.username, origin);
    topTools = getUserTopTools(user.username, 12, origin);
    models = getUserModels(user.username, 8, origin);
    recentSessions = getUserRecentSessions(user.username, 12, origin);
  } catch (error) {
    if (error instanceof RangeError) {
      return Response.json({ error: error.message }, { status: 400 });
    }
    throw error;
  }

  return Response.json({
    user: {
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
