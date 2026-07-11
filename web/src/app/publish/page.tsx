import { Topbar } from "@/components/topbar";
import { PublishCandidateCard } from "@/components/publish-candidate-card";
import { fmtNum } from "@/lib/format";
import { config } from "@/lib/config";
import type { PublishCandidate } from "@/lib/types";
import { SESSION_ORIGINS, SESSION_STATUSES } from "@/lib/types";
import { getPublishQueueResponse, PublishReviewCommandError } from "@/lib/publish";
import {
  normalizeAnalyticsOrigin,
  originQueryValue,
  withOriginQuery,
} from "@/lib/origin-lens";
import { WorkflowLensBar } from "@/components/workflow-lens-bar";
import Link from "next/link";
import { IconShieldCheck, IconAlertTriangle } from "@tabler/icons-react";

export const dynamic = "force-dynamic";

const VIS_OPTIONS = ["pending", "needs_changes", "private", "unlisted", "public", "all"] as const;

export default async function PublishQueuePage({
  searchParams,
}: {
  searchParams: Promise<{ visibility?: string; status?: string; origin?: string; user?: string; limit?: string }>;
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
  const originLens = normalizeAnalyticsOrigin(params.origin);
  const origin = originQueryValue(originLens);
  const user = params.user || "";
  const limit = Math.min(Math.max(parseInt(params.limit || "50", 10) || 50, 1), 200);

  let pendingCount = 0;
  try {
    const pendingPayload = await getPublishQueueResponse({
      visibility: "pending",
      origin,
      limit: 1,
      reviews: false,
    });
    pendingCount = pendingPayload.total;
  } catch (error) {
    if (!(error instanceof PublishReviewCommandError || error instanceof RangeError)) {
      throw error;
    }
  }

  let queueError: string | null = null;
  let candidates: PublishCandidate[] = [];
  try {
    const payload = await getPublishQueueResponse({
      visibility,
      status: status || undefined,
      origin,
      user: user || undefined,
      limit,
      reviews: false,
    });
    candidates = payload.candidates;
  } catch (error) {
    if (error instanceof PublishReviewCommandError || error instanceof RangeError) {
      queueError = error.message;
    } else {
      throw error;
    }
  }

  return (
    <>
      <Topbar title="Publish queue" />
      <div className="p-7 max-w-[1400px] animate-fade-up">
        <WorkflowLensBar
          basePath="/publish"
          originLens={originLens}
          extraParams={{
            visibility: visibility !== "pending" ? visibility : undefined,
            status: status || undefined,
            user: user || undefined,
            limit: String(limit),
          }}
        />

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
              <option key={v} value={v}>
                {v === "pending"
                  ? "Pending (private+unlisted)"
                  : v === "needs_changes"
                    ? "Needs changes (review wants tighter visibility)"
                    : v}
              </option>
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
          <select
            name="origin"
            defaultValue={origin || ""}
            className="bg-lp-surface border border-lp-border-dim text-lp-text rounded-lg px-3 py-2 text-sm font-body"
          >
            <option value="">Current lens</option>
            {SESSION_ORIGINS.map((o) => (
              <option key={o} value={o}>{o}</option>
            ))}
          </select>
          <button
            type="submit"
            className="bg-lp-amber text-lp-bg rounded-lg px-5 py-2 text-sm font-semibold font-body hover:bg-lp-amber-hot hover:shadow-[0_2px_12px_rgba(245,158,11,0.3)] transition-all cursor-pointer"
          >
            Filter
          </button>
          {(visibility !== "pending" || status || origin) && (
            <Link
              href={withOriginQuery("/publish", originLens)}
              className="text-xs text-lp-text-faint hover:text-lp-amber no-underline"
            >
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
            <PublishCandidateCard key={c.session_id} candidate={c} />
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
                  : visibility === "needs_changes"
                    ? "No sessions currently look over-shared under the active filters."
                    : `No sessions matching visibility="${visibility}"${status ? ` and status="${status}"` : ""}.`}
                {visibility === "pending" && (
                  <>
                    {" "}Try <Link href={withOriginQuery("/publish", originLens, { visibility: "needs_changes", status: status || undefined, user: user || undefined, limit: String(limit) })} className="text-lp-amber hover:text-lp-amber-hot">Needs changes</Link> to review already-public sessions the scanner would tighten.
                  </>
                )}
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
