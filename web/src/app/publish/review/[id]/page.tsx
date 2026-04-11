import { notFound } from "next/navigation";
import Link from "next/link";
import { Topbar } from "@/components/topbar";
import { StatusBadge, VisibilityBadge, RecommendationBadge } from "@/components/status-badge";
import { SourceBadge } from "@/components/badge";
import { getSession } from "@/lib/db";
import { config } from "@/lib/config";
import { fmtTs, truncate, displayProject } from "@/lib/format";
import type { PublishReview } from "@/lib/types";
import {
  IconShieldCheck,
  IconAlertTriangle,
  IconLock,
  IconEye,
  IconArrowLeft,
} from "@tabler/icons-react";

export const dynamic = "force-dynamic";

async function fetchReview(sessionId: string): Promise<PublishReview | null> {
  // Call the Flask backend's review endpoint (runs Python publish.py logic)
  try {
    const res = await fetch(
      `http://127.0.0.1:5001/api/publish/review/${encodeURIComponent(sessionId)}`,
      { cache: "no-store" }
    );
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

export default async function PublishReviewPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  if (config.publicMode) {
    return (
      <>
        <Topbar title="Review" />
        <div className="p-7 text-center py-20 text-lp-text-faint">
          Publish review is not available in public mode.
        </div>
      </>
    );
  }

  const { id } = await params;
  const session = getSession(id);
  if (!session) notFound();

  const review = await fetchReview(id);

  const highFindings = review?.findings.filter((f) => f.severity === "high") ?? [];
  const mediumFindings = review?.findings.filter((f) => f.severity === "medium") ?? [];
  const totalFindings = (review?.findings ?? []).length;

  return (
    <>
      <Topbar title="Publish review" />
      <div className="p-7 max-w-[1000px] animate-fade-up">
        {/* Back link */}
        <Link href="/publish" className="inline-flex items-center gap-1 text-xs text-lp-text-faint hover:text-lp-amber no-underline mb-5">
          <IconArrowLeft size={12} stroke={2} />
          Back to queue
        </Link>

        {/* Session header */}
        <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5 mb-5">
          <div className="flex items-center gap-2 mb-3">
            <StatusBadge status={session.session_status} />
            <VisibilityBadge visibility={session.visibility} />
            <SourceBadge source={session.source} />
            {session.repo_name ? (
              <span className="font-mono text-xs text-lp-text-dim">
                {String(session.repo_name)}
              </span>
            ) : null}
            <span className="ml-auto text-xs text-lp-text-faint font-mono">
              {fmtTs(session.first_timestamp)}
            </span>
          </div>

          {session.session_goal && (
            <div className="text-sm font-medium text-lp-text mb-2">
              {session.session_goal}
            </div>
          )}
          {session.session_summary && (
            <div className="text-sm text-lp-text-dim leading-relaxed mb-2">
              {session.session_summary}
            </div>
          )}
          {session.session_outcome && (
            <div className="text-xs text-lp-text-faint italic">
              Outcome: {session.session_outcome}
            </div>
          )}

          <div className="flex items-center gap-4 mt-3 pt-3 border-t border-lp-border-dim text-xs text-lp-text-faint">
            <span className="font-mono">{session.session_id.slice(0, 16)}...</span>
            <span>{displayProject(session.project)}</span>
            <Link
              href={`/sessions/${session.session_id}`}
              className="ml-auto text-lp-amber hover:text-lp-amber-hot no-underline inline-flex items-center gap-1"
            >
              <IconEye size={12} stroke={2} />
              View transcript
            </Link>
          </div>
        </div>

        {/* Review result */}
        {review ? (
          <div className="space-y-5">
            {/* Recommendation card */}
            <div className={`rounded-lg p-5 border ${
              review.recommendation === "public"
                ? "bg-lp-green/[0.04] border-lp-green/20"
                : review.recommendation === "unlisted"
                ? "bg-lp-amber/[0.04] border-lp-amber/20"
                : "bg-lp-red/[0.04] border-lp-red/20"
            }`}>
              <div className="flex items-center gap-3 mb-3">
                <RecommendationBadge recommendation={review.recommendation} />
                <span className="text-xs font-bold text-lp-text-dim uppercase tracking-widest">
                  Recommendation
                </span>
              </div>
              <div className="text-sm text-lp-text leading-relaxed">
                {review.rationale}
              </div>
              {review.inspected_path && (
                <div className="mt-3 text-xs text-lp-text-faint font-mono">
                  Inspected: {review.inspected_path}
                </div>
              )}
            </div>

            {/* Findings */}
            {totalFindings > 0 ? (
              <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5">
                <div className="text-xs font-bold text-lp-text-dim uppercase tracking-widest mb-4">
                  Findings ({totalFindings})
                </div>

                {highFindings.length > 0 && (
                  <div className="mb-4">
                    <div className="flex items-center gap-1.5 text-xs font-semibold text-lp-red mb-2">
                      <IconAlertTriangle size={13} stroke={2} />
                      High severity ({highFindings.length})
                    </div>
                    <div className="space-y-2">
                      {highFindings.map((f, i) => (
                        <FindingCard key={i} finding={f} />
                      ))}
                    </div>
                  </div>
                )}

                {mediumFindings.length > 0 && (
                  <div>
                    <div className="text-xs font-semibold text-lp-amber mb-2">
                      Medium severity ({mediumFindings.length})
                    </div>
                    <div className="space-y-2">
                      {mediumFindings.map((f, i) => (
                        <FindingCard key={i} finding={f} />
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-8 text-center">
                <IconShieldCheck size={28} stroke={1.5} className="mx-auto text-lp-green mb-2" />
                <div className="text-sm font-medium text-lp-text mb-1">No findings</div>
                <div className="text-xs text-lp-text-faint">
                  No secrets, PII, or structural issues detected.
                </div>
              </div>
            )}

            {/* Metadata */}
            {review.metadata && Object.keys(review.metadata).length > 0 && (
              <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5">
                <div className="text-xs font-bold text-lp-text-dim uppercase tracking-widest mb-3">
                  Review metadata
                </div>
                <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-2">
                  {Object.entries(review.metadata).map(([k, v]) => (
                    <div key={k} className="flex flex-col gap-0.5">
                      <span className="text-[0.62rem] uppercase tracking-wider text-lp-text-faint font-semibold">
                        {k.replace(/_/g, " ")}
                      </span>
                      <span className="text-xs font-mono text-lp-text">
                        {v ?? "\u2014"}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : (
          <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-8 text-center">
            <IconLock size={28} stroke={1.5} className="mx-auto text-lp-text-faint mb-2" />
            <div className="text-sm font-medium text-lp-text mb-1">
              Review not available
            </div>
            <div className="text-xs text-lp-text-faint max-w-sm mx-auto leading-relaxed">
              The Flask backend must be running on port 5001 for publish reviews.
              Start it with <code className="text-lp-amber">logpile serve --flask --port 5001</code>
            </div>
          </div>
        )}
      </div>
    </>
  );
}

function FindingCard({ finding }: { finding: import("@/lib/types").PublishFinding }) {
  return (
    <div className={`rounded-lg px-4 py-3 border-l-[3px] ${
      finding.severity === "high"
        ? "border-l-lp-red bg-lp-red/[0.03]"
        : "border-l-lp-amber bg-lp-amber/[0.03]"
    }`}>
      <div className="flex items-center gap-2 mb-1">
        <span className={`text-xs font-semibold uppercase tracking-wide ${
          finding.severity === "high" ? "text-lp-red" : "text-lp-amber"
        }`}>
          {finding.category}
        </span>
        <span className="text-xs font-medium text-lp-text">
          {finding.title}
        </span>
      </div>
      <div className="text-xs text-lp-text-dim font-mono leading-relaxed">
        {truncate(finding.evidence, 200)}
      </div>
      <div className="flex gap-3 mt-1.5 text-[0.62rem] text-lp-text-faint">
        {finding.source && <span>source: {finding.source}</span>}
        {finding.line_number && <span>line {finding.line_number}</span>}
      </div>
    </div>
  );
}
