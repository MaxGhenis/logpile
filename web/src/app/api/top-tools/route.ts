import { getTopTools } from "@/lib/db";

export async function GET() {
  const rows = getTopTools(20);
  return Response.json({
    labels: rows.map((r) => r.tool_name),
    datasets: [
      {
        label: "Calls",
        data: rows.map((r) => r.cnt),
        backgroundColor: "#f59e0b99",
        borderColor: "#f59e0b",
        borderWidth: 1,
        borderRadius: 3,
      },
    ],
  });
}
