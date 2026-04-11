import { config } from "@/lib/config";
import { getPublishReview, PublishReviewCommandError } from "@/lib/publish";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  if (config.publicMode) {
    return Response.json({ error: "not found" }, { status: 404 });
  }

  const { id } = await params;
  try {
    const review = await getPublishReview(id);
    if (!review) {
      return Response.json({ error: "not found" }, { status: 404 });
    }
    return Response.json(review);
  } catch (error) {
    if (error instanceof PublishReviewCommandError) {
      return Response.json({ error: error.message }, { status: error.status });
    }
    throw error;
  }
}
