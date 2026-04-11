import { Topbar } from "@/components/topbar";
import { StatCard } from "@/components/stat-card";
import { ActivityChart } from "@/components/charts/activity-chart";
import { BarChartCard } from "@/components/charts/bar-chart";
import { getDashboardStats, getRecentSessions } from "@/lib/db";
import { fmtNum, fmtTokens, fmtTs, displayProject, truncate } from "@/lib/format";
import Link from "next/link";

export const dynamic = "force-dynamic";

export default function DashboardPage() {
  const stats = getDashboardStats();
  const recent = getRecentSessions(10);

  const totalMsgs = (stats.total_user_msgs ?? 0) + (stats.total_assistant_msgs ?? 0);
  const totalTokens = (stats.total_input_tokens ?? 0) + (stats.total_output_tokens ?? 0);

  return (
    <>
      <Topbar title="Dashboard" />
      <div className="p-7 max-w-[1400px] animate-fade-up">
        {/* Stat cards */}
        <div className="grid grid-cols-4 gap-4 mb-4">
          <StatCard value={fmtNum(stats.total_sessions)} label="sessions" />
          <StatCard value={fmtNum(totalMsgs)} label="messages" />
          <StatCard value={fmtTokens(totalTokens)} label="tokens" />
          <StatCard value={fmtNum(stats.active_users)} label="operators" />
        </div>

        {/* Charts */}
        <div className="grid grid-cols-[2fr_1fr] gap-4 mb-4">
          <ActivityChart url="/api/messages-per-day" title="Activity — last 30 days" />
          <ActivityChart url="/api/messages-per-tool" title="CC vs Codex" />
        </div>
        <div className="grid grid-cols-[2fr_1fr] gap-4 mb-6">
          <BarChartCard url="/api/top-tools" title="Most-used tools" />
          <BarChartCard url="/api/error-rate" title="Errors by operator" />
        </div>

        {/* Recent sessions */}
        <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5">
          <h2 className="text-xs font-bold text-lp-text-dim uppercase tracking-widest mb-4">
            Recent sessions
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="text-lp-text-faint text-[0.68rem] uppercase tracking-wider">
                  <th className="text-left py-2.5 px-3.5 border-b border-lp-border font-semibold">When</th>
                  <th className="text-left py-2.5 px-3.5 border-b border-lp-border font-semibold">Operator</th>
                  <th className="text-left py-2.5 px-3.5 border-b border-lp-border font-semibold">Tool</th>
                  <th className="text-left py-2.5 px-3.5 border-b border-lp-border font-semibold">Project</th>
                  <th className="text-left py-2.5 px-3.5 border-b border-lp-border font-semibold">Msgs</th>
                  <th className="text-left py-2.5 px-3.5 border-b border-lp-border font-semibold">Tokens</th>
                  <th className="text-left py-2.5 px-3.5 border-b border-lp-border font-semibold">First message</th>
                </tr>
              </thead>
              <tbody>
                {recent.map((r) => (
                  <tr
                    key={r.session_id}
                    className="border-b border-lp-border-dim last:border-b-0 hover:bg-lp-amber-glow transition-colors"
                  >
                    <td className="py-2.5 px-3.5 text-lp-text-faint font-mono text-xs whitespace-nowrap">
                      {fmtTs(r.first_timestamp)}
                    </td>
                    <td className="py-2.5 px-3.5">
                      <Link href={`/u/${r.user_slug || r.username}`} className="no-underline">
                        <span className="bg-lp-raised border border-lp-border-dim rounded px-2 py-0.5 text-xs font-medium text-lp-text hover:border-lp-amber hover:text-lp-amber transition-colors">
                          {r.user_display_name}
                        </span>
                      </Link>
                    </td>
                    <td className="py-2.5 px-3.5">
                      <span
                        className={`rounded px-2 py-0.5 text-[0.7rem] font-bold font-mono tracking-wide border ${
                          r.source === "claudecode"
                            ? "bg-lp-amber/10 text-lp-amber border-lp-amber/25"
                            : "bg-lp-blue/10 text-lp-blue border-lp-blue/25"
                        }`}
                      >
                        {r.source === "claudecode" ? "CC" : "Codex"}
                      </span>
                    </td>
                    <td className="py-2.5 px-3.5 text-lp-text-dim max-w-[150px] truncate">
                      {displayProject(r.project)}
                    </td>
                    <td className="py-2.5 px-3.5">
                      {(r.user_message_count ?? 0) + (r.assistant_message_count ?? 0)}
                    </td>
                    <td className="py-2.5 px-3.5 font-mono text-xs">
                      {fmtNum(r.tokens)}
                    </td>
                    <td className="py-2.5 px-3.5 max-w-[400px] truncate">
                      <Link
                        href={`/sessions/${r.session_id}`}
                        className="text-lp-text hover:text-lp-amber no-underline transition-colors"
                      >
                        {truncate(r.first_user_message || "\u2014", 80)}
                      </Link>
                    </td>
                  </tr>
                ))}
                {recent.length === 0 && (
                  <tr>
                    <td colSpan={7} className="text-center text-lp-text-faint py-10 italic">
                      No sessions yet. Run <code className="text-lp-amber">logpile sync</code> to populate.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="mt-4 pt-3 border-t border-lp-border-dim text-right">
            <Link href="/sessions" className="text-sm text-lp-amber hover:text-lp-amber-hot no-underline">
              View all sessions &rarr;
            </Link>
          </div>
        </div>
      </div>
    </>
  );
}
