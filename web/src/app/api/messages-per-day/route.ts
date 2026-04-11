import { getMessagesPerDay } from "@/lib/db";

const PALETTE = [
  "#f59e0b", "#60a5fa", "#84cc16", "#ef4444",
  "#a78bfa", "#fb923c", "#22d3ee", "#f472b6",
];

export async function GET() {
  const rows = getMessagesPerDay(30);
  const daysSet = [...new Set(rows.map((r) => r.day))].sort();
  const displayCounts = new Map<string, number>();
  for (const row of rows) {
    const display = row.user_display_name || row.username || row.user_key;
    displayCounts.set(display, (displayCounts.get(display) ?? 0) + 1);
  }

  const labelMap = new Map<string, string>();
  for (const row of rows) {
    const display = row.user_display_name || row.username || row.user_key;
    labelMap.set(
      row.user_key,
      (displayCounts.get(display) ?? 0) > 1 ? `${display} (@${row.user_key})` : display
    );
  }

  const users = [...new Set(rows.map((r) => r.user_key))].sort((a, b) =>
    (labelMap.get(a) ?? a).localeCompare(labelMap.get(b) ?? b)
  );

  const pivot = new Map<string, Map<string, number>>();
  for (const r of rows) {
    if (!pivot.has(r.day)) pivot.set(r.day, new Map());
    pivot.get(r.day)!.set(r.user_key, r.msgs);
  }

  const datasets = users.map((user, i) => ({
    label: labelMap.get(user) ?? user,
    data: daysSet.map((d) => pivot.get(d)?.get(user) ?? 0),
    borderColor: PALETTE[i % PALETTE.length],
    backgroundColor: PALETTE[i % PALETTE.length] + "18",
    borderWidth: 2,
    pointRadius: 0,
    tension: 0.35,
    fill: false,
  }));

  return Response.json({ labels: daysSet, datasets });
}
