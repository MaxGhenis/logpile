import { getErrorRate } from "@/lib/db";

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  let rows;
  try {
    rows = getErrorRate(15, searchParams.get("origin") ?? undefined);
  } catch (error) {
    if (error instanceof RangeError) {
      return Response.json({ error: error.message }, { status: 400 });
    }
    throw error;
  }
  const displayCounts = new Map<string, number>();
  for (const row of rows) {
    const display = row.user_display_name || row.username || row.user_key;
    displayCounts.set(display, (displayCounts.get(display) ?? 0) + 1);
  }

  const labels = rows.map((row) => {
    const display = row.user_display_name || row.username || row.user_key;
    return (displayCounts.get(display) ?? 0) > 1
      ? `${display} (@${row.user_key})`
      : display;
  });

  return Response.json({
    labels,
    datasets: [
      {
        label: "Errors",
        data: rows.map((r) => r.errors ?? 0),
        backgroundColor: "#f59e0b99",
        borderColor: "#f59e0b",
        borderWidth: 1,
        borderRadius: 3,
      },
    ],
  });
}
