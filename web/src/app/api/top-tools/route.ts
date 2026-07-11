import { getTopTools } from "@/lib/db";

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  let rows;
  try {
    rows = getTopTools(20, searchParams.get("origin") ?? undefined);
  } catch (error) {
    if (error instanceof RangeError) {
      return Response.json({ error: error.message }, { status: 400 });
    }
    throw error;
  }
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
