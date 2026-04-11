import { getApiUsers } from "@/lib/db";

export async function GET() {
  return Response.json(getApiUsers());
}
