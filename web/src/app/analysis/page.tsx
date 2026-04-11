import { Topbar } from "@/components/topbar";
import {
  getTopBashCommands,
  getTopTools,
  getUserStats,
  getSharedUtilities,
} from "@/lib/db";
import { fmtNum, fmtTokens, fmtTs, truncate } from "@/lib/format";
import Link from "next/link";

export const dynamic = "force-dynamic";

export default function AnalysisPage() {
  const userRows = getUserStats();
  const toolRows = getTopTools(25);
  const bashCmds = getTopBashCommands(30).map((r) => ({
    cmd: r.command.trim().replace(/\s+/g, " ").slice(0, 80),
    cnt: r.cnt,
  }));
  const sharedRows = getSharedUtilities(20);
  const maxToolCnt = toolRows[0]?.cnt || 1;

  return (
    <>
      <Topbar title="Analysis" />
      <div className="p-7 max-w-[1400px] animate-fade-up">
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
                    key={r.slug}
                    className="border-b border-lp-border-dim last:border-b-0 hover:bg-lp-amber-glow transition-colors"
                  >
                    <td className="py-2.5 px-3.5">
                      <Link href={`/u/${r.slug}`} className="no-underline group">
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
