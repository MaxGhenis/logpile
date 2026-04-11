import { getMessagesByTool } from "@/lib/db";

const SOURCE_COLORS: Record<string, { border: string; bg: string }> = {
  claudecode: { border: "#f59e0b", bg: "#f59e0b18" },
  codex: { border: "#60a5fa", bg: "#60a5fa18" },
};

export async function GET() {
  const rows = getMessagesByTool(30);
  const daysSet = [...new Set(rows.map((r) => r.day))].sort();
  const sources = [...new Set(rows.map((r) => r.source))].sort();

  const pivot = new Map<string, Map<string, number>>();
  for (const r of rows) {
    if (!pivot.has(r.day)) pivot.set(r.day, new Map());
    pivot.get(r.day)!.set(r.source, r.msgs);
  }

  const datasets = sources.map((src) => {
    const c = SOURCE_COLORS[src] ?? { border: "#aaa", bg: "#aaa33" };
    return {
      label: src === "claudecode" ? "CC" : "Codex",
      data: daysSet.map((d) => pivot.get(d)?.get(src) ?? 0),
      borderColor: c.border,
      backgroundColor: c.bg,
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.35,
      fill: true,
    };
  });

  return Response.json({ labels: daysSet, datasets });
}
