import { Topbar } from "@/components/topbar";
import { SourceBadge } from "@/components/badge";
import { StatusBadge } from "@/components/status-badge";
import {
  getContextExplosionWorkstreams,
  getRepeatedObjectiveRelaunches,
  getTopBashCommands,
  getRunawaySessions,
  getTopTools,
  getUserStats,
  getSharedUtilities,
} from "@/lib/db";
import { displayProject, fmtDuration, fmtNum, fmtTokens, fmtTs, truncate } from "@/lib/format";
import {
  normalizeAnalyticsOrigin,
  originLensLabel,
  originQueryValue,
  withOriginQuery,
} from "@/lib/origin-lens";
import { WorkflowLensBar } from "@/components/workflow-lens-bar";
import Link from "next/link";

export const dynamic = "force-dynamic";

export default async function AnalysisPage({
  searchParams,
}: {
  searchParams?: Promise<{ origin?: string }>;
}) {
  const params = searchParams ? await searchParams : undefined;
  const originLens = normalizeAnalyticsOrigin(params?.origin);
  const origin = originQueryValue(originLens);
  const userRows = getUserStats(origin);
  const toolRows = getTopTools(25, origin);
  const bashCmds = getTopBashCommands(30, origin).map((r) => ({
    cmd: r.command.trim().replace(/\s+/g, " ").slice(0, 80),
    cnt: r.cnt,
  }));
  const sharedRows = getSharedUtilities(20, origin);
  const contextRows = getContextExplosionWorkstreams(6, origin);
  const runawayRows = getRunawaySessions(8, origin);
  const objectiveRows = getRepeatedObjectiveRelaunches(8, origin);
  const maxToolCnt = toolRows[0]?.cnt || 1;

  const fmtPct = (value: number) => `${(value * 100).toFixed(1)}%`;

  return (
    <>
      <Topbar title="Analysis" />
      <div className="p-7 max-w-[1400px] animate-fade-up">
        <WorkflowLensBar basePath="/analysis" originLens={originLens} />

        {/* Operator stats */}
        <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5 mb-5">
          <h2 className="text-xs font-bold text-lp-text-dim uppercase tracking-widest mb-4">
            Operator stats
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm border-collapse min-w-[800px]">
              <thead>
                <tr className="text-lp-text-faint text-[0.68rem] uppercase tracking-wider">
                  {["Operator", "Sessions", "Msgs", "Tool calls", "Errors", "Tokens", "First seen", "Last seen"].map(
                    (h) => (
                      <th key={h} className="text-left py-2.5 px-3.5 border-b border-lp-border font-semibold">
                        {h}
                      </th>
                    )
                  )}
                </tr>
              </thead>
              <tbody>
                {userRows.map((r) => (
                  <tr
                    key={r.username}
                    className="border-b border-lp-border-dim last:border-b-0 hover:bg-lp-amber-glow transition-colors"
                  >
                    <td className="py-2.5 px-3.5">
                      <Link href={withOriginQuery(`/u/${r.username}`, originLens)} className="no-underline group">
                        <span className="bg-lp-raised border border-lp-border-dim rounded px-2 py-0.5 text-xs font-medium text-lp-text group-hover:border-lp-amber group-hover:text-lp-amber transition-colors">
                          {r.display_name}
                        </span>
                      </Link>
                    </td>
                    <td className="py-2.5 px-3.5">{fmtNum(r.sessions)}</td>
                    <td className="py-2.5 px-3.5">{fmtNum(r.user_msgs)}</td>
                    <td className="py-2.5 px-3.5">{fmtNum(r.tool_calls)}</td>
                    <td className="py-2.5 px-3.5">
                      {r.errors ? (
                        <span className="text-lp-red">{fmtNum(r.errors)}</span>
                      ) : (
                        "0"
                      )}
                    </td>
                    <td className="py-2.5 px-3.5 font-mono text-xs">{fmtTokens(r.tokens)}</td>
                    <td className="py-2.5 px-3.5 font-mono text-xs text-lp-text-faint whitespace-nowrap">
                      {fmtTs(r.first_seen)}
                    </td>
                    <td className="py-2.5 px-3.5 font-mono text-xs text-lp-text-faint whitespace-nowrap">
                      {fmtTs(r.last_seen)}
                    </td>
                  </tr>
                ))}
                {userRows.length === 0 && (
                  <tr>
                    <td colSpan={8} className="text-center text-lp-text-faint py-10 italic">
                      No data yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* Tools + Bash side by side */}
        <div className="grid grid-cols-2 gap-4 mb-5">
          <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5">
            <h2 className="text-xs font-bold text-lp-text-dim uppercase tracking-widest mb-4">
              Top tool calls
            </h2>
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="text-lp-text-faint text-[0.68rem] uppercase tracking-wider">
                  <th className="text-left py-2 px-3 border-b border-lp-border font-semibold">Tool</th>
                  <th className="text-left py-2 px-3 border-b border-lp-border font-semibold">Calls</th>
                  <th className="py-2 px-3 border-b border-lp-border"></th>
                </tr>
              </thead>
              <tbody>
                {toolRows.map((r) => (
                  <tr key={r.tool_name} className="border-b border-lp-border-dim last:border-b-0">
                    <td className="py-2 px-3 font-mono text-xs">{r.tool_name}</td>
                    <td className="py-2 px-3">{fmtNum(r.cnt)}</td>
                    <td className="py-2 px-3">
                      <div
                        className="mini-bar"
                        style={{ width: `${Math.round((r.cnt / maxToolCnt) * 100)}%` }}
                      />
                    </td>
                  </tr>
                ))}
                {toolRows.length === 0 && (
                  <tr>
                    <td colSpan={3} className="text-center text-lp-text-faint py-6 italic">
                      No tool calls recorded.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5">
            <h2 className="text-xs font-bold text-lp-text-dim uppercase tracking-widest mb-4">
              Top bash / shell commands
            </h2>
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="text-lp-text-faint text-[0.68rem] uppercase tracking-wider">
                  <th className="text-left py-2 px-3 border-b border-lp-border font-semibold">Command</th>
                  <th className="text-left py-2 px-3 border-b border-lp-border font-semibold">Count</th>
                </tr>
              </thead>
              <tbody>
                {bashCmds.map((r, i) => (
                  <tr key={i} className="border-b border-lp-border-dim last:border-b-0">
                    <td className="py-2 px-3 font-mono text-xs max-w-[360px] truncate">
                      {r.cmd}
                    </td>
                    <td className="py-2 px-3">{fmtNum(r.cnt)}</td>
                  </tr>
                ))}
                {bashCmds.length === 0 && (
                  <tr>
                    <td colSpan={2} className="text-center text-lp-text-faint py-6 italic">
                      No bash commands recorded.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5 mb-5">
          <div className="flex flex-wrap items-end justify-between gap-3 mb-4">
            <div>
              <h2 className="text-xs font-bold text-lp-text-dim uppercase tracking-widest mb-1">
                Context explosion
              </h2>
              <p className="text-[0.72rem] text-lp-text-faint">
                Codex root workstreams whose forked children are carrying large inherited-context burden.
              </p>
            </div>
            <div className="text-[0.68rem] uppercase tracking-widest text-lp-text-faint">
              Last 7 days · {originLensLabel(originLens)}
            </div>
          </div>
          {contextRows.length > 0 ? (
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
              {contextRows.map((row) => (
                <div
                  key={row.root_session_id}
                  className="rounded-lg border border-lp-border bg-lp-bg/60 px-4 py-3"
                >
                  <div className="flex flex-wrap items-center gap-2 mb-2">
                    <SourceBadge source="codex" />
                    <span className="text-[0.68rem] font-mono text-lp-text-faint">
                      {fmtTs(row.root_first_timestamp)}
                    </span>
                    {row.warnings.map((warning) => (
                      <span
                        key={warning}
                        className="rounded border border-lp-red/25 bg-lp-red/10 px-2 py-1 text-[0.68rem] font-mono font-semibold text-lp-red"
                      >
                        {warning}
                      </span>
                    ))}
                  </div>
                  <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-lp-text-dim">
                    <Link
                      href={withOriginQuery(`/u/${row.username}`, originLens)}
                      className="no-underline"
                    >
                      <span className="bg-lp-raised border border-lp-border-dim rounded px-2 py-0.5 font-medium text-lp-text hover:border-lp-amber hover:text-lp-amber transition-colors">
                        {row.user_display_name}
                      </span>
                    </Link>
                    <span className="font-mono text-[0.68rem] text-lp-text-faint">
                      {row.repo_name || displayProject(row.project)}
                    </span>
                  </div>
                  <div className="text-sm font-medium text-lp-text mb-2">
                    {row.display_label}
                  </div>
                  <div className="text-sm text-lp-text-dim leading-relaxed mb-3">
                    {row.root_summary || "Large Codex fork swarm with no deterministic root summary yet."}
                  </div>
                  <div className="flex flex-wrap gap-2 mb-3">
                    <span className="rounded border border-lp-amber/25 bg-lp-amber-glow px-2 py-1 text-[0.68rem] font-mono font-semibold text-lp-amber">
                      {fmtTokens(row.total_tokens)} total
                    </span>
                    <span className="rounded border border-lp-border-dim bg-lp-raised px-2 py-1 text-[0.68rem] font-mono font-semibold text-lp-text-dim">
                      {fmtPct(row.cached_input_share)} cached
                    </span>
                    <span className="rounded border border-lp-border-dim bg-lp-raised px-2 py-1 text-[0.68rem] font-mono font-semibold text-lp-text-dim">
                      {fmtPct(row.child_token_share)} child burden
                    </span>
                    <span className="rounded border border-lp-border-dim bg-lp-raised px-2 py-1 text-[0.68rem] font-mono font-semibold text-lp-text-dim">
                      {fmtNum(row.child_session_count)} child sessions
                    </span>
                  </div>
                  <div className="rounded-lg border border-lp-border-dim bg-lp-bg/40 px-3 py-3 mb-3">
                    <div className="text-[0.68rem] font-bold uppercase tracking-widest text-lp-text-faint mb-2">
                      Largest child sessions
                    </div>
                    <div className="grid grid-cols-1 gap-2">
                      {row.top_children.map((child, index) => (
                        <div
                          key={child.session_id}
                          className="flex flex-wrap items-center justify-between gap-2 rounded border border-lp-border-dim bg-lp-raised/70 px-2.5 py-2"
                        >
                          <div className="min-w-0">
                            <div className="text-xs font-medium text-lp-text">
                              Child {index + 1} · {truncate(child.session_id, 12)}
                            </div>
                            <div className="text-[0.68rem] font-mono text-lp-text-faint">
                              {fmtTokens(child.total_tokens)} · {fmtPct(
                                child.total_input_tokens > 0
                                  ? child.cached_input_tokens / child.total_input_tokens
                                  : 0
                              )} cached · depth {child.spawn_depth}
                            </div>
                          </div>
                          <Link
                            href={`/sessions/${child.session_id}`}
                            className="text-[0.72rem] font-medium text-lp-amber hover:text-lp-amber-hot no-underline"
                          >
                            Inspect
                          </Link>
                        </div>
                      ))}
                    </div>
                  </div>
                  <Link
                    href={`/sessions/${row.root_session_id}`}
                    className="text-sm font-medium text-lp-amber hover:text-lp-amber-hot no-underline"
                  >
                    Inspect root session
                  </Link>
                </div>
              ))}
            </div>
          ) : (
            <div className="rounded-lg border border-dashed border-lp-border px-4 py-6 text-sm text-lp-text-faint italic">
              No Codex fork swarms crossed the context-explosion threshold under this workflow lens.
            </div>
          )}
        </div>

        <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5 mb-5">
          <div className="flex flex-wrap items-end justify-between gap-3 mb-4">
            <div>
              <h2 className="text-xs font-bold text-lp-text-dim uppercase tracking-widest mb-1">
                Runaway sessions
              </h2>
              <p className="text-[0.72rem] text-lp-text-faint">
                Tool-heavy sessions that are strong candidates for tighter scoping or a forced reset.
              </p>
            </div>
            <div className="text-[0.68rem] uppercase tracking-widest text-lp-text-faint">
              {originLensLabel(originLens)}
            </div>
          </div>
          {runawayRows.length > 0 ? (
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
              {runawayRows.map((row) => (
                <div
                  key={row.session_id}
                  className="rounded-lg border border-lp-border bg-lp-bg/60 px-4 py-3"
                >
                  <div className="flex flex-wrap items-center gap-2 mb-2">
                    <SourceBadge source={row.source} />
                    <StatusBadge status={row.session_status} />
                    <span className="text-[0.68rem] font-mono text-lp-text-faint">
                      {fmtTs(row.first_timestamp)}
                    </span>
                  </div>
                  <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-lp-text-dim">
                    <Link
                      href={withOriginQuery(`/u/${row.username}`, originLens)}
                      className="no-underline"
                    >
                      <span className="bg-lp-raised border border-lp-border-dim rounded px-2 py-0.5 font-medium text-lp-text hover:border-lp-amber hover:text-lp-amber transition-colors">
                        {row.user_display_name}
                      </span>
                    </Link>
                    <span className="font-mono text-[0.68rem] text-lp-text-faint">
                      {row.repo_name || displayProject(row.project)}
                    </span>
                  </div>
                  <div className="text-sm text-lp-text leading-relaxed mb-3">
                    {row.session_summary || "No deterministic summary yet. Review the transcript and tighten the task boundary."}
                  </div>
                  <div className="flex flex-wrap gap-2 mb-3">
                    <span className="rounded border border-lp-amber/25 bg-lp-amber-glow px-2 py-1 text-[0.68rem] font-mono font-semibold text-lp-amber">
                      {fmtNum(row.tool_call_count)} tools
                    </span>
                    <span
                      className={`rounded border px-2 py-1 text-[0.68rem] font-mono font-semibold ${
                        row.error_count > 0
                          ? "border-lp-red/25 bg-lp-red/10 text-lp-red"
                          : "border-lp-border-dim bg-lp-raised text-lp-text-dim"
                      }`}
                    >
                      {fmtNum(row.error_count)} errors
                    </span>
                    <span className="rounded border border-lp-border-dim bg-lp-raised px-2 py-1 text-[0.68rem] font-mono font-semibold text-lp-text-dim">
                      {fmtDuration(row.duration_seconds)}
                    </span>
                  </div>
                  <Link
                    href={`/sessions/${row.session_id}`}
                    className="text-sm font-medium text-lp-amber hover:text-lp-amber-hot no-underline"
                  >
                    Inspect session
                  </Link>
                </div>
              ))}
            </div>
          ) : (
            <div className="rounded-lg border border-dashed border-lp-border px-4 py-6 text-sm text-lp-text-faint italic">
              No runaway sessions under this workflow lens.
            </div>
          )}
        </div>

        <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5 mb-5">
          <div className="flex flex-wrap items-end justify-between gap-3 mb-4">
            <div>
              <h2 className="text-xs font-bold text-lp-text-dim uppercase tracking-widest mb-1">
                Repeated objective relaunches
              </h2>
              <p className="text-[0.72rem] text-lp-text-faint">
                Objective families launched multiple times in the last 30 days under this workflow lens.
              </p>
            </div>
            <div className="text-[0.68rem] uppercase tracking-widest text-lp-text-faint">
              {originLensLabel(originLens)}
            </div>
          </div>
          {objectiveRows.length > 0 ? (
            <div className="grid grid-cols-1 gap-3">
              {objectiveRows.map((row) => (
                <div
                  key={row.objective_key}
                  className="rounded-lg border border-lp-border bg-lp-bg/60 px-4 py-3"
                >
                  <div className="flex flex-wrap items-center gap-2 mb-2">
                    <span className="rounded border border-lp-amber/25 bg-lp-amber-glow px-2 py-1 text-[0.68rem] font-mono font-semibold text-lp-amber">
                      {fmtNum(row.launches)} launches
                    </span>
                    <span className="rounded border border-lp-border-dim bg-lp-raised px-2 py-1 text-[0.68rem] font-mono font-semibold text-lp-text-dim">
                      {fmtNum(row.operator_count)} operators
                    </span>
                    <StatusBadge status={row.latest_status} />
                    <span className="text-[0.68rem] font-mono text-lp-text-faint">
                      {fmtTs(row.latest_timestamp)}
                    </span>
                  </div>
                  <div className="text-sm font-medium text-lp-text mb-2">
                    {row.display_label}
                  </div>
                  <div className="text-sm text-lp-text-dim leading-relaxed mb-3">
                    {row.latest_summary || "No deterministic summary yet. Open the latest session and tighten the task boundary before relaunching."}
                  </div>
                  <div className="flex flex-wrap gap-2 mb-3">
                    <span className="rounded border border-lp-amber/25 bg-lp-amber-glow px-2 py-1 text-[0.68rem] font-mono font-semibold text-lp-amber">
                      {fmtNum(row.total_tool_calls)} total tools
                    </span>
                    <span
                      className={`rounded border px-2 py-1 text-[0.68rem] font-mono font-semibold ${
                        row.total_errors > 0
                          ? "border-lp-red/25 bg-lp-red/10 text-lp-red"
                          : "border-lp-border-dim bg-lp-raised text-lp-text-dim"
                      }`}
                    >
                      {fmtNum(row.total_errors)} total errors
                    </span>
                    <span className="rounded border border-lp-border-dim bg-lp-raised px-2 py-1 text-[0.68rem] font-mono font-semibold text-lp-text-dim">
                      {row.latest_repo_name ? displayProject(row.latest_repo_name) : "unknown repo"}
                    </span>
                  </div>
                  <Link
                    href={withOriginQuery("/sessions", originLens, {
                      objective: row.objective_key,
                      objectiveLabel: row.display_label,
                    })}
                    className="text-sm font-medium text-lp-amber hover:text-lp-amber-hot no-underline"
                  >
                    View matching sessions
                  </Link>
                </div>
              ))}
            </div>
          ) : (
            <div className="rounded-lg border border-dashed border-lp-border px-4 py-6 text-sm text-lp-text-faint italic">
              No repeated objective families under this workflow lens.
            </div>
          )}
        </div>

        {/* Shared utility candidates */}
        <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5">
          <h2 className="text-xs font-bold text-lp-text-dim uppercase tracking-widest mb-1">
            Shared utility candidates
          </h2>
          <p className="text-[0.72rem] text-lp-text-faint mb-4">
            Commands run by 2+ operators &mdash; good refactor targets
          </p>
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="text-lp-text-faint text-[0.68rem] uppercase tracking-wider">
                <th className="text-left py-2.5 px-3.5 border-b border-lp-border font-semibold">Command</th>
                <th className="text-left py-2.5 px-3.5 border-b border-lp-border font-semibold">Operators</th>
                <th className="text-left py-2.5 px-3.5 border-b border-lp-border font-semibold">Total runs</th>
              </tr>
            </thead>
            <tbody>
              {sharedRows.map((r, i) => (
                <tr key={i} className="border-b border-lp-border-dim last:border-b-0">
                  <td className="py-2.5 px-3.5 font-mono text-xs max-w-[400px] truncate">
                    {truncate(r.command, 100)}
                  </td>
                  <td className="py-2.5 px-3.5">
                    <span className="rounded px-2 py-0.5 text-[0.7rem] font-bold font-mono bg-lp-amber-glow text-lp-amber border border-lp-amber/25">
                      {r.users}
                    </span>
                  </td>
                  <td className="py-2.5 px-3.5">{fmtNum(r.total)}</td>
                </tr>
              ))}
              {sharedRows.length === 0 && (
                <tr>
                  <td colSpan={3} className="text-center text-lp-text-faint py-10 italic">
                    Not enough operators yet &mdash; invite teammates and re-sync.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
