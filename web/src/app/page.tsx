import { Topbar } from "@/components/topbar";
import { ActivityChart } from "@/components/charts/activity-chart";
import { PublicIntro } from "@/components/public-intro";
import { SourceBadge, UserBadge } from "@/components/badge";
import { StatusBadge } from "@/components/status-badge";
import { ActivityBadges } from "@/components/activity-badges";
import { getDashboardStats, getDayCounts, getRecentSessions } from "@/lib/db";
import { fmtNum, fmtTokens, displayProject, sessionTitle, truncate } from "@/lib/format";
import { normalizeAnalyticsOrigin, originQueryValue, withOriginQuery } from "@/lib/origin-lens";
import { WorkflowLensBar } from "@/components/workflow-lens-bar";
import { config } from "@/lib/config";
import type { SessionRow } from "@/lib/types";
import Link from "next/link";

export const dynamic = "force-dynamic";

const RECORD_SESSION_LIMIT = 400;
const RECORD_DAYS = 7;
const ROWS_PER_DAY = 15;

function dayLabel(day: string): string {
  const d = new Date(`${day}T00:00:00Z`);
  if (isNaN(d.getTime())) return day;
  return d.toLocaleDateString("en-US", {
    weekday: "long",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

/** Group sessions by UTC day, newest day first (input is already sorted desc). */
function groupByDay(rows: SessionRow[]): Array<{ day: string; rows: SessionRow[] }> {
  const days = new Map<string, SessionRow[]>();
  for (const r of rows) {
    const day = (r.first_timestamp ?? "").slice(0, 10) || "undated";
    const bucket = days.get(day);
    if (bucket) bucket.push(r);
    else days.set(day, [r]);
  }
  return [...days.entries()].map(([day, dayRows]) => ({ day, rows: dayRows }));
}

export default async function RecordPage({
  searchParams,
}: {
  searchParams?: Promise<{ origin?: string }>;
}) {
  const params = searchParams ? await searchParams : undefined;
  const originLens = normalizeAnalyticsOrigin(params?.origin);
  const origin = originQueryValue(originLens);
  const stats = getDashboardStats(origin);
  const recent = getRecentSessions(RECORD_SESSION_LIMIT, origin);
  const dayCounts = new Map(getDayCounts(RECORD_DAYS, origin).map((d) => [d.day, d.sessions]));
  const days = groupByDay(recent)
    .filter(({ day }) => dayCounts.has(day))
    .slice(0, RECORD_DAYS);

  const totalMsgs = (stats.total_user_msgs ?? 0) + (stats.total_assistant_msgs ?? 0);
  const totalTokens = (stats.total_input_tokens ?? 0) + (stats.total_output_tokens ?? 0);
  const multiOperator = (stats.active_users ?? 0) > 1;

  const ledgerStrip: Array<[string, string]> = [
    [fmtNum(stats.total_sessions), "sessions"],
    [fmtNum(totalMsgs), "messages"],
    [fmtTokens(totalTokens), "tokens"],
    [fmtNum(stats.total_projects), "repos"],
    [fmtNum(stats.active_users), "operators"],
  ];

  return (
    <>
      <Topbar title="Record" />
      <div className="p-7 max-w-[1400px] animate-fade-up">
        {config.publicMode && <PublicIntro />}
        <WorkflowLensBar basePath="/" originLens={originLens} />

        {/* Ledger strip — the record's running totals */}
        <div className="flex flex-wrap items-baseline gap-x-7 gap-y-2 bg-lp-surface border border-lp-border-dim rounded-lg px-5 py-3.5 mb-7 font-mono">
          {ledgerStrip.map(([value, label]) => (
            <span key={label} className="whitespace-nowrap">
              <span className="text-[0.95rem] text-lp-text font-medium">{value}</span>{" "}
              <span className="text-xs text-lp-text-faint">{label}</span>
            </span>
          ))}
          <Link
            href={withOriginQuery("/analysis", originLens)}
            className="ml-auto text-xs text-lp-amber hover:text-lp-amber-hot no-underline whitespace-nowrap"
          >
            Open the ledger &rarr;
          </Link>
        </div>

        {/* The record */}
        {days.map(({ day, rows }) => (
          <section key={day} className="mb-6">
            <div className="flex items-baseline gap-3 mb-2">
              <h2 className="text-xs font-bold text-lp-text-dim uppercase tracking-widest whitespace-nowrap">
                {dayLabel(day)}
              </h2>
              <span className="text-[0.68rem] text-lp-text-faint font-mono whitespace-nowrap">
                {fmtNum(dayCounts.get(day) ?? rows.length)} session
                {(dayCounts.get(day) ?? rows.length) === 1 ? "" : "s"}
              </span>
              <div className="flex-1 border-t border-lp-border-dim self-center" />
            </div>
            <div className="bg-lp-surface border border-lp-border-dim rounded-lg divide-y divide-lp-border-dim overflow-hidden">
              {rows.slice(0, ROWS_PER_DAY).map((r) => (
                <Link
                  key={r.session_id}
                  href={`/sessions/${r.session_id}`}
                  className="flex items-center gap-3 px-4 py-2.5 no-underline hover:bg-lp-amber-glow transition-colors group"
                >
                  <span className="font-mono text-xs text-lp-text-faint w-[40px] shrink-0">
                    {(r.first_timestamp ?? "").slice(11, 16) || "—"}
                  </span>
                  <SourceBadge source={r.source} />
                  {multiOperator && (
                    <UserBadge username={r.username} displayName={r.user_display_name} />
                  )}
                  <span className="hidden md:block text-xs text-lp-text-dim w-[140px] shrink-0 truncate">
                    {displayProject(r.project)}
                  </span>
                  <span className="flex-1 min-w-0 truncate text-sm text-lp-text group-hover:text-lp-amber transition-colors">
                    {truncate(
                      sessionTitle(r.session_goal, r.session_summary, r.first_user_message),
                      110
                    )}
                  </span>
                  <span className="hidden lg:flex shrink-0">
                    <ActivityBadges {...r} />
                  </span>
                  <StatusBadge status={r.session_status} />
                  <span className="font-mono text-xs text-lp-text-faint w-[64px] text-right shrink-0">
                    {fmtTokens(r.tokens)}
                  </span>
                </Link>
              ))}
              {(dayCounts.get(day) ?? 0) > Math.min(rows.length, ROWS_PER_DAY) && (
                <Link
                  href={withOriginQuery("/sessions", originLens)}
                  className="block px-4 py-2 text-xs text-lp-text-faint hover:text-lp-amber no-underline font-mono"
                >
                  {`+ ${fmtNum((dayCounts.get(day) ?? 0) - Math.min(rows.length, ROWS_PER_DAY))} more in the archive →`}
                </Link>
              )}
            </div>
          </section>
        ))}

        {days.length === 0 && (
          <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-10 text-center mb-6">
            <p className="text-lp-text-dim mb-1.5">No sessions in the record yet.</p>
            <p className="text-sm text-lp-text-faint">
              Run <code className="text-lp-amber font-mono">logpile sync</code> to index your
              Claude Code and Codex sessions.
            </p>
          </div>
        )}

        {days.length > 0 && (
          <div className="mb-8 text-right">
            <Link
              href={withOriginQuery("/sessions", originLens)}
              className="text-sm text-lp-amber hover:text-lp-amber-hot no-underline"
            >
              View the full archive &rarr;
            </Link>
          </div>
        )}

        {/* Pulse — supporting lens, below the record */}
        <div className="grid grid-cols-1 md:grid-cols-[2fr_1fr] gap-4">
          <ActivityChart
            url={withOriginQuery("/api/messages-per-day", originLens)}
            title="Activity — last 30 days"
          />
          <ActivityChart
            url={withOriginQuery("/api/messages-per-tool", originLens)}
            title="CC vs Codex"
          />
        </div>
      </div>
    </>
  );
}
