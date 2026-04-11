import { config } from "@/lib/config";
import { getPublishQueueResponse, PublishReviewCommandError } from "@/lib/publish";

function parsePositiveInt(raw: string | null, fallback: number, name: string) {
  if (raw === null || raw === "") {
    return fallback;
  }
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed)) {
    throw new RangeError(`Invalid integer for '${name}'`);
  }
  return parsed;
}

function parseReviews(raw: string | null): boolean {
  if (raw === null || raw === "") {
    return false;
  }
  return ["1", "true", "yes"].includes(raw.toLowerCase());
}

export async function GET(req: Request) {
  if (config.publicMode) {
    return Response.json({ error: "not found" }, { status: 404 });
  }

  const url = new URL(req.url);
  try {
    const payload = await getPublishQueueResponse({
      visibility: url.searchParams.get("visibility") || undefined,
      status: url.searchParams.get("status") || undefined,
      user: url.searchParams.get("user") || undefined,
      limit: parsePositiveInt(url.searchParams.get("limit"), 25, "limit"),
      reviews: parseReviews(url.searchParams.get("reviews")),
    });
    return Response.json(payload);
  } catch (error) {
    if (error instanceof RangeError) {
      return Response.json({ error: error.message }, { status: 400 });
    }
    if (error instanceof PublishReviewCommandError) {
      return Response.json({ error: error.message }, { status: error.status });
    }
    throw error;
  }
}
