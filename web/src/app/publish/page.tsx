import { Topbar } from "@/components/topbar";
import { StatusBadge, VisibilityBadge } from "@/components/status-badge";
import { SourceBadge } from "@/components/badge";
import { getPublishQueueCount } from "@/lib/db";
import { fmtTs, fmtNum, truncate } from "@/lib/format";
import { config } from "@/lib/config";
import type { PublishCandidate } from "@/lib/types";
import { SESSION_STATUSES } from "@/lib/types";
import { getPublishQueueResponse, PublishReviewCommandError } from "@/lib/publish";
import Link from "next/link";
import { IconShieldCheck, IconAlertTriangle } from "@tabler/icons-react";

export const dynamic = "force-dynamic";

const VIS_OPTIONS = ["pending", "private", "unlisted", "public", "all"] as const;

export default async function PublishQueuePage({
  searchParams,
}: {
  searchParams: Promise<{ visibility?: string; status?: string; user?: string; limit?: string }>;
}) {
  if (config.publicMode) {
    return (
      <>
        <Topbar title="Publish" />
        <div className="p-7 max-w-[1400px] text-center py-20 text-lp-text-faint">
          Publish queue is not available in public mode.
        </div>
      </>
    );
  }

  const params = await searchParams;
  const visibility = params.visibility || "pending";
  const status = params.status || "";
  const user = params.user || "";
  const limit = Math.min(Math.max(parseInt(params.limit || "50", 10) || 50, 1), 200);

  let queueError: string | null = null;
  let candidates: PublishCandidate[] = [];
  try {
    const payload = await getPublishQueueResponse({
      visibility,
      status: status || undefined,
      user: user || undefined,
      limit,
      reviews: true,
    });
    candidates = payload.candidates;
  } catch (error) {
    if (error instanceof PublishReviewCommandError || error instanceof RangeError) {
      queueError = error.message;
    } else {
      throw error;
    }
  }
  const pendingCount = getPublishQueueCount({ visibility: "pending" });

  return (
    <>
      <Topbar title="Publish queue" />
      <div className="p-7 max-w-[1400px] animate-fade-up">
        {/* Header */}
        <div className="flex items-center justify-between mb-5">
          <div>
            <h2 className="font-brand text-xl font-bold text-lp-text m-0">
              Publish queue
            </h2>
            <div className="text-sm text-lp-text-faint mt-1">
              {pendingCount} session{pendingCount !== 1 ? "s" : ""} pending review
            </div>
          </div>
          <div className="flex items-center gap-2 text-xs text-lp-text-faint">
            <IconShieldCheck size={14} stroke={1.5} />
            private — local only
          </div>
        </div>

        {/* Filters */}
        <form action="/publish" method="get" className="flex gap-2 flex-wrap items-center mb-5">
          <select
            name="visibility"
            defaultValue={visibility}
            className="bg-lp-surface border border-lp-border-dim text-lp-text rounded-lg px-3 py-2 text-sm font-body"
          >
            {VIS_OPTIONS.map((v) => (
              <option key={v} value={v}>{v === "pending" ? "Pending (private+unlisted)" : v}</option>
            ))}
          </select>
          <select
            name="status"
            defaultValue={status}
            className="bg-lp-surface border border-lp-border-dim text-lp-text rounded-lg px-3 py-2 text-sm font-body"
          >
            <option value="">All statuses</option>
            {SESSION_STATUSES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <button
            type="submit"
            className="bg-lp-amber text-lp-bg rounded-lg px-5 py-2 text-sm font-semibold font-body hover:bg-lp-amber-hot hover:shadow-[0_2px_12px_rgba(245,158,11,0.3)] transition-all cursor-pointer"
          >
            Filter
          </button>
          {(visibility !== "pending" || status) && (
            <Link href="/publish" className="text-xs text-lp-text-faint hover:text-lp-amber no-underline">
              Reset
            </Link>
          )}
        </form>

        {/* Queue */}
        <div className="flex flex-col gap-3">
          {queueError && (
            <div className="bg-lp-surface border border-lp-red/20 rounded-lg p-5 text-center">
              <div className="inline-flex items-center gap-1.5 text-sm font-medium text-lp-red mb-1">
                <IconAlertTriangle size={16} stroke={1.8} />
                Queue unavailable
              </div>
              <div className="text-xs text-lp-text-faint max-w-md mx-auto leading-relaxed">
                {queueError}
              </div>
            </div>
          )}

          {candidates.map((c) => (
            <Link
              key={c.session_id}
              href={`/publish/review/${c.session_id}`}
              className="bg-lp-surface border border-lp-border-dim rounded-lg p-5 no-underline text-inherit hover-glow group block"
            >
              {/* Top row: badges + timestamp */}
              <div className="flex items-center gap-2 mb-2.5">
                <StatusBadge status={c.session_status} />
                <VisibilityBadge visibility={c.visibility} />
                <SourceBadge source={c.source} />
                {c.repo_name && (
                  <span className="font-mono text-xs text-lp-text-dim">{c.repo_name}</span>
                )}
                <span className="ml-auto text-xs text-lp-text-faint font-mono">
                  {fmtTs(c.first_timestamp)}
                </span>
              </div>

              {/* Goal */}
              {c.session_goal && (
                <div className="text-sm font-medium text-lp-text mb-1.5">
                  {truncate(c.session_goal, 120)}
                </div>
              )}

              {/* Summary */}
              {c.session_summary && (
                <div className="text-sm text-lp-text-dim leading-relaxed mb-1.5">
                  {truncate(c.session_summary, 200)}
                </div>
              )}

              {/* Outcome */}
              {c.session_outcome && (
                <div className="text-xs text-lp-text-faint italic">
                  Outcome: {truncate(c.session_outcome, 120)}
                </div>
              )}

              {/* Bottom row: findings */}
              {(c.finding_count > 0 || c.review_recommendation) && (
                <div className="flex items-center gap-3 mt-3 pt-3 border-t border-lp-border-dim">
                  {c.review_recommendation && (
                    <span className={`text-xs font-semibold ${
                      c.review_recommendation === "public" ? "text-lp-green" :
                      c.review_recommendation === "unlisted" ? "text-lp-amber" :
                      "text-lp-red"
                    }`}>
                      rec: {c.review_recommendation}
                    </span>
                  )}
                  {c.high_findings > 0 && (
                    <span className="inline-flex items-center gap-1 text-xs text-lp-red">
                      <IconAlertTriangle size={12} stroke={2} />
                      {c.high_findings} high
                    </span>
                  )}
                  {c.medium_findings > 0 && (
                    <span className="text-xs text-lp-amber">
                      {c.medium_findings} medium
                    </span>
                  )}
                </div>
              )}

              {/* Fallback: no goal/summary — show first message */}
              {!c.session_goal && !c.session_summary && (
                <div className="text-sm text-lp-text-faint italic">
                  No summary available — view transcript to review
                </div>
              )}
            </Link>
          ))}

          {candidates.length === 0 && !queueError && (
            <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-10 text-center">
              <div className="text-lp-green mb-2">
                <IconShieldCheck size={32} stroke={1.5} className="mx-auto" />
              </div>
              <div className="font-brand text-lg font-bold text-lp-text mb-2">
                Queue clear
              </div>
              <div className="text-sm text-lp-text-faint max-w-sm mx-auto leading-relaxed">
                {visibility === "pending"
                  ? "No sessions pending review. All sessions are either published or explicitly private."
                  : `No sessions matching visibility="${visibility}"${status ? ` and status="${status}"` : ""}.`}
              </div>
            </div>
          )}
        </div>

        <div className="mt-4 text-xs text-lp-text-faint">
          {fmtNum(candidates.length)} candidate{candidates.length !== 1 ? "s" : ""} shown
        </div>
      </div>
    </>
  );
}
