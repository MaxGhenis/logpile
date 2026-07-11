"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { IconAlertTriangle, IconShieldCheck } from "@tabler/icons-react";

import { StatusBadge, VisibilityBadge } from "@/components/status-badge";
import { SourceBadge } from "@/components/badge";
import { fmtTs, truncate } from "@/lib/format";
import type { PublishCandidate, PublishReview } from "@/lib/types";

const VISIBILITY_ORDER: Record<string, number> = {
  private: 0,
  unlisted: 1,
  public: 2,
};

type ReviewSummary = {
  loading: boolean;
  finding_count: number;
  high_findings: number;
  medium_findings: number;
  recommendation: PublishReview["recommendation"] | null;
  needs_visibility_change: boolean;
  error: string | null;
};

const INITIAL_SUMMARY: ReviewSummary = {
  loading: true,
  finding_count: 0,
  high_findings: 0,
  medium_findings: 0,
  recommendation: null,
  needs_visibility_change: false,
  error: null,
};

export function PublishCandidateCard({ candidate }: { candidate: PublishCandidate }) {
  const [summary, setSummary] = useState<ReviewSummary>(INITIAL_SUMMARY);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    fetch(`/api/publish/review/${encodeURIComponent(candidate.session_id)}`, {
      signal: controller.signal,
    })
      .then(async (res) => {
        if (!res.ok) {
          const payload = (await res.json().catch(() => ({}))) as { error?: string };
          throw new Error(payload.error || `status ${res.status}`);
        }
        return res.json() as Promise<PublishReview>;
      })
      .then((review) => {
        if (cancelled) return;
        const high = review.findings.filter((f) => f.severity === "high").length;
        const medium = review.findings.filter((f) => f.severity === "medium").length;
        const currentRank = VISIBILITY_ORDER[candidate.visibility] ?? 2;
        const recRank = VISIBILITY_ORDER[review.recommendation] ?? currentRank;
        setSummary({
          loading: false,
          finding_count: review.findings.length,
          high_findings: high,
          medium_findings: medium,
          recommendation: review.recommendation,
          needs_visibility_change: recRank < currentRank,
          error: null,
        });
      })
      .catch((err: Error) => {
        if (cancelled || err.name === "AbortError") return;
        setSummary({ ...INITIAL_SUMMARY, loading: false, error: err.message });
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [candidate.session_id, candidate.visibility]);

  const showReviewFooter =
    summary.loading ||
    summary.error ||
    summary.finding_count > 0 ||
    summary.recommendation;

  return (
    <Link
      href={`/publish/review/${candidate.session_id}`}
      className="bg-lp-surface border border-lp-border-dim rounded-lg p-5 no-underline text-inherit hover-glow group block"
    >
      {/* Top row: badges + timestamp */}
      <div className="flex items-center gap-2 mb-2.5 flex-wrap">
        <StatusBadge status={candidate.session_status} />
        <VisibilityBadge visibility={candidate.visibility} />
        <SourceBadge source={candidate.source} />
        {candidate.repo_name && (
          <span className="font-mono text-xs text-lp-text-dim">{candidate.repo_name}</span>
        )}
        <span className="ml-auto text-xs text-lp-text-faint font-mono">
          {fmtTs(candidate.first_timestamp)}
        </span>
      </div>

      {candidate.session_goal && (
        <div className="text-sm font-medium text-lp-text mb-1.5">
          {truncate(candidate.session_goal, 120)}
        </div>
      )}

      {candidate.session_summary && (
        <div className="text-sm text-lp-text-dim leading-relaxed mb-1.5">
          {truncate(candidate.session_summary, 200)}
        </div>
      )}

      {candidate.session_outcome && (
        <div className="text-xs text-lp-text-faint italic">
          Outcome: {truncate(candidate.session_outcome, 120)}
        </div>
      )}

      {!candidate.session_goal && !candidate.session_summary && (
        <div className="text-sm text-lp-text-faint italic">
          No summary available — view transcript to review
        </div>
      )}

      {/* Review findings — lazy loaded */}
      {showReviewFooter && (
        <div className="flex items-center gap-3 mt-3 pt-3 border-t border-lp-border-dim min-h-[18px]">
          {summary.loading && (
            <span className="text-xs text-lp-text-faint italic">scanning…</span>
          )}
          {summary.error && (
            <span className="text-xs text-lp-text-faint">
              review unavailable
            </span>
          )}
          {!summary.loading && !summary.error && summary.recommendation && (
            <span
              className={`text-xs font-semibold ${
                summary.recommendation === "public"
                  ? "text-lp-green"
                  : summary.recommendation === "unlisted"
                  ? "text-lp-amber"
                  : "text-lp-red"
              }`}
            >
              {summary.needs_visibility_change ? "tighten to" : "rec:"}{" "}
              {summary.recommendation}
            </span>
          )}
          {!summary.loading && summary.high_findings > 0 && (
            <span className="inline-flex items-center gap-1 text-xs text-lp-red">
              <IconAlertTriangle size={12} stroke={2} />
              {summary.high_findings} high
            </span>
          )}
          {!summary.loading && summary.medium_findings > 0 && (
            <span className="text-xs text-lp-amber">{summary.medium_findings} medium</span>
          )}
          {!summary.loading &&
            !summary.error &&
            summary.finding_count === 0 &&
            summary.recommendation === candidate.visibility && (
              <span className="inline-flex items-center gap-1 text-xs text-lp-green">
                <IconShieldCheck size={12} stroke={2} />
                clean
              </span>
            )}
        </div>
      )}
    </Link>
  );
}
