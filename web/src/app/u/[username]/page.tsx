import { notFound } from "next/navigation";
import Link from "next/link";
import { Topbar } from "@/components/topbar";
import { StatCard } from "@/components/stat-card";
import { SourceBadge } from "@/components/badge";
import { StatusBadge } from "@/components/status-badge";
import { UserActivityChart, UserSourceChart } from "@/components/charts/profile-charts";
import {
  getUserByUsername,
  getUserSummary,
  getUserActivity,
  getUserSourceBreakdown,
  getUserTopTools,
  getUserModels,
  getUserRecentSessions,
  getUserTopRepos,
  getUserGithubActivity,
  getUserGithubTotals,
  isProfileDirectlyVisible,
} from "@/lib/db";
import { fmtNum, fmtTokens, fmtTs, fmtDuration, displayProject } from "@/lib/format";
import { config } from "@/lib/config";
import {
  normalizeAnalyticsOrigin,
  originQueryValue,
  withOriginQuery,
} from "@/lib/origin-lens";
import { WorkflowLensBar } from "@/components/workflow-lens-bar";
import {
  IconPencil,
  IconTestPipe,
  IconHammer,
  IconGitCommit,
  IconGitBranch,
} from "@tabler/icons-react";

export const dynamic = "force-dynamic";

export default async function UserProfilePage({
  params,
  searchParams,
}: {
  params: Promise<{ username: string }>;
  searchParams?: Promise<{ origin?: string }>;
}) {
  const { username } = await params;
  const resolvedSearchParams = searchParams ? await searchParams : undefined;
  const originLens = normalizeAnalyticsOrigin(resolvedSearchParams?.origin);
  const origin = originQueryValue(originLens);
  const user = getUserByUsername(username);
  if (!user || !isProfileDirectlyVisible(user)) notFound();

  const summary = getUserSummary(user.username, origin);
  if (!summary) notFound();

  const activityRows = getUserActivity(user.username, 60, origin);
  const sourceRows = getUserSourceBreakdown(user.username, origin);
  const toolRows = getUserTopTools(user.username, 12, origin);
  const modelRows = getUserModels(user.username, 8, origin);
  const recentRows = getUserRecentSessions(user.username, 12, origin);
  const topRepos = getUserTopRepos(user.username, 8, origin);
  const githubDaily = getUserGithubActivity(user.username, 60);
  const githubTotals = getUserGithubTotals(user.username, 180);

  const activity = {
    labels: activityRows.map((r) => r.day),
    messages: activityRows.map((r) => r.messages ?? 0),
    tool_calls: activityRows.map((r) => r.tool_calls ?? 0),
    github_contributions: activityRows.map((r) => githubDaily[r.day]?.contributions ?? 0),
  };
  const displayName = user.display_name ?? user.username;

  const sourceBreakdown = {
    labels: sourceRows.map((r) =>
      r.source === "claudecode" ? "CC" : r.source === "codex" ? "Codex" : r.source
    ),
    sessions: sourceRows.map((r) => r.sessions ?? 0),
    colors: sourceRows.map((r) =>
      r.source === "claudecode" ? "#f59e0b" : "#60a5fa"
    ),
  };

  return (
    <>
      <Topbar title="Profile" />
      <div className="p-7 max-w-[1400px] animate-fade-up">
        <WorkflowLensBar basePath={`/u/${user.username}`} originLens={originLens} />

        {/* Hero */}
        <div className="flex items-end justify-between gap-5 p-8 border border-lp-border-dim rounded-xl mb-5 bg-[radial-gradient(ellipse_at_top_left,rgba(245,158,11,0.08),transparent_50%),radial-gradient(ellipse_at_bottom_right,rgba(96,165,250,0.05),transparent_50%),var(--color-lp-surface)]">
          <div className="min-w-0">
            <div className="text-[0.68rem] text-lp-amber-dim uppercase tracking-[1.2px] font-bold mb-2.5 flex flex-wrap items-center gap-2">
              <span>operator profile</span>
              {config.publicMode && (
                <>
                  <span className="text-lp-text-faint">·</span>
                  <Link
                    href="/"
                    className="text-lp-text-faint hover:text-lp-amber no-underline normal-case tracking-normal font-medium"
                  >
                    on <span className="font-brand font-bold tracking-tight">Logpile</span>
                  </Link>
                </>
              )}
            </div>
            <h1 className="font-brand text-[clamp(2.2rem,5vw,3.5rem)] font-black leading-[0.95] tracking-tight text-lp-text m-0">
              @{user.username}
            </h1>
            <div className="text-sm text-lp-text-faint mt-3">
              {displayName}
              {user.bio && ` \u00b7 ${user.bio}`}
              <br />
              active since {fmtTs(summary.first_seen)} &middot; {summary.active_days} active
              days &middot; {summary.known_repos} repos &middot; {summary.known_projects} projects
              {githubTotals && (
                <>
                  <br />
                  <span className="text-lp-green">
                    {fmtNum(githubTotals.contributions)} GitHub contributions
                    {githubTotals.prs_opened > 0 && ` · ${fmtNum(githubTotals.prs_opened)} PRs`}
                  </span>
                  <span className="text-lp-text-faint"> (last 180 days)</span>
                </>
              )}
            </div>
          </div>
          <div className="flex gap-2.5 flex-wrap justify-end">
            {(!config.publicMode || user.profile_visibility === "public") && (
              <Link
                href={withOriginQuery("/sessions", originLens, { user: user.username })}
                className="inline-flex items-center justify-center min-w-[108px] px-3.5 py-2 rounded-full border border-lp-border text-sm font-medium text-lp-text-dim bg-lp-bg/60 no-underline hover:border-lp-amber hover:text-lp-amber hover:bg-lp-amber-glow transition-all"
              >
                View sessions
              </Link>
            )}
            {config.publicMode && (
              <a
                href="https://github.com/MaxGhenis/logpile#quick-start"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center justify-center px-3.5 py-2 rounded-full border border-lp-amber bg-lp-amber-glow text-sm font-semibold text-lp-amber no-underline hover:bg-lp-amber hover:text-lp-bg transition-all"
              >
                Get your own
              </a>
            )}
          </div>
        </div>

        {/* Stat cards — primary */}
        <div className="grid grid-cols-4 gap-4 mb-3">
          <StatCard value={fmtNum(summary.total_sessions)} label="sessions" />
          <StatCard value={fmtNum(summary.total_messages)} label="messages" />
          <StatCard value={fmtNum(summary.total_tool_calls)} label="tool calls" />
          <StatCard value={fmtTokens(summary.total_tokens)} label="tokens" />
        </div>

        {/* Stat cards — activity + status summary */}
        <div className="grid grid-cols-5 gap-4 mb-3">
          <MiniStat icon={<IconPencil size={14} stroke={2} />} value={fmtNum(summary.write_paths)} label="files written" />
          <MiniStat icon={<IconTestPipe size={14} stroke={2} />} value={fmtNum(summary.test_runs)} label="test runs" alert={summary.test_failures > 0 ? `${fmtNum(summary.test_failures)} failed` : undefined} />
          <MiniStat icon={<IconHammer size={14} stroke={2} />} value={fmtNum(summary.build_runs)} label="builds" alert={summary.build_failures > 0 ? `${fmtNum(summary.build_failures)} failed` : undefined} />
          <MiniStat icon={<IconGitCommit size={14} stroke={2} />} value={fmtNum(summary.git_commits)} label="git commits" />
          <MiniStat icon={<IconGitBranch size={14} stroke={2} />} value={fmtNum(summary.known_repos)} label="repos" />
        </div>

        {/* Session status mix */}
        {(summary.success_sessions > 0 || summary.partial_sessions > 0 || summary.failed_sessions > 0) && (
          <div className="bg-lp-surface border border-lp-border-dim rounded-lg px-5 py-3 mb-5 flex items-center gap-5">
            <span className="text-[0.68rem] text-lp-text-faint uppercase tracking-wider font-semibold mr-1">Outcomes</span>
            {summary.success_sessions > 0 && (
              <span className="inline-flex items-center gap-1.5 text-xs">
                <span className="w-2 h-2 rounded-full bg-lp-green" />
                <span className="font-mono font-medium text-lp-text">{fmtNum(summary.success_sessions)}</span>
                <span className="text-lp-text-faint">success</span>
              </span>
            )}
            {summary.partial_sessions > 0 && (
              <span className="inline-flex items-center gap-1.5 text-xs">
                <span className="w-2 h-2 rounded-full bg-lp-amber" />
                <span className="font-mono font-medium text-lp-text">{fmtNum(summary.partial_sessions)}</span>
                <span className="text-lp-text-faint">partial</span>
              </span>
            )}
            {summary.failed_sessions > 0 && (
              <span className="inline-flex items-center gap-1.5 text-xs">
                <span className="w-2 h-2 rounded-full bg-lp-red" />
                <span className="font-mono font-medium text-lp-text">{fmtNum(summary.failed_sessions)}</span>
                <span className="text-lp-text-faint">failed</span>
              </span>
            )}
            {summary.exploration_sessions > 0 && (
              <span className="inline-flex items-center gap-1.5 text-xs">
                <span className="w-2 h-2 rounded-full bg-lp-blue" />
                <span className="font-mono font-medium text-lp-text">{fmtNum(summary.exploration_sessions)}</span>
                <span className="text-lp-text-faint">exploration</span>
              </span>
            )}
          </div>
        )}

        {/* Charts */}
        <div className="grid grid-cols-[2fr_1fr] gap-4 mb-5">
          <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5">
            <div className="text-[0.72rem] text-lp-text-faint uppercase tracking-widest font-semibold mb-4 flex items-center justify-between">
              <span>Activity — last 60 days</span>
              {githubTotals && (
                <span className="text-[0.65rem] text-lp-text-faint normal-case tracking-normal font-normal italic">
                  with GitHub overlay
                </span>
              )}
            </div>
            <UserActivityChart data={activity} />
          </div>
          <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5">
            <div className="text-[0.72rem] text-lp-text-faint uppercase tracking-widest font-semibold mb-4">
              Session mix
            </div>
            <UserSourceChart data={sourceBreakdown} />
          </div>
        </div>

        {/* Top repos + Top tools */}
        <div className="grid grid-cols-2 gap-4 mb-5">
          {topRepos.length > 0 && (
            <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5">
              <div className="text-xs font-bold text-lp-text-dim uppercase tracking-widest mb-4">
                Top repos
              </div>
              <table className="w-full text-sm border-collapse">
                <thead>
                  <tr className="text-lp-text-faint text-[0.68rem] uppercase tracking-wider">
                    <th className="text-left py-2 px-3 border-b border-lp-border font-semibold">Repo</th>
                    <th className="text-left py-2 px-3 border-b border-lp-border font-semibold">Sessions</th>
                    <th className="text-left py-2 px-3 border-b border-lp-border font-semibold">Branches</th>
                    <th className="py-2 px-3 border-b border-lp-border"></th>
                  </tr>
                </thead>
                <tbody>
                  {topRepos.map((r) => (
                    <tr key={`${r.repo_name}:${r.repo_root ?? "public"}`} className="border-b border-lp-border-dim last:border-b-0 hover:bg-lp-amber-glow transition-colors">
                      <td className="py-2 px-3">
                        <Link
                          href={withOriginQuery("/sessions", originLens, {
                            user: user.username,
                            repo: r.repo_name,
                            repoRoot: r.repo_root ?? undefined,
                          })}
                          className="font-mono text-xs text-lp-text hover:text-lp-amber no-underline"
                        >
                          {r.repo_name}
                        </Link>
                      </td>
                      <td className="py-2 px-3">{fmtNum(r.sessions)}</td>
                      <td className="py-2 px-3">{fmtNum(r.branches)}</td>
                      <td className="py-2 px-3">
                        <div className="mini-bar" style={{ width: `${Math.round((r.sessions / (topRepos[0]?.sessions || 1)) * 100)}%` }} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5">
            <div className="text-xs font-bold text-lp-text-dim uppercase tracking-widest mb-4">
              Top tools
            </div>
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
                      <div className="mini-bar" style={{ width: `${Math.round((r.cnt / (toolRows[0]?.cnt || 1)) * 100)}%` }} />
                    </td>
                  </tr>
                ))}
                {toolRows.length === 0 && (
                  <tr><td colSpan={3} className="text-center text-lp-text-faint py-6 italic">No tool calls.</td></tr>
                )}
              </tbody>
            </table>
          </div>

          {topRepos.length === 0 && (
            <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5">
              <div className="text-xs font-bold text-lp-text-dim uppercase tracking-widest mb-4">
                Model mix
              </div>
              <table className="w-full text-sm border-collapse">
                <thead>
                  <tr className="text-lp-text-faint text-[0.68rem] uppercase tracking-wider">
                    <th className="text-left py-2 px-3 border-b border-lp-border font-semibold">Model</th>
                    <th className="text-left py-2 px-3 border-b border-lp-border font-semibold">Sessions</th>
                    <th className="text-left py-2 px-3 border-b border-lp-border font-semibold">Messages</th>
                  </tr>
                </thead>
                <tbody>
                  {modelRows.map((r) => (
                    <tr key={r.model_name} className="border-b border-lp-border-dim last:border-b-0">
                      <td className="py-2 px-3 font-mono text-xs">{r.model_name}</td>
                      <td className="py-2 px-3">{fmtNum(r.sessions)}</td>
                      <td className="py-2 px-3">{fmtNum(r.messages)}</td>
                    </tr>
                  ))}
                  {modelRows.length === 0 && (
                    <tr><td colSpan={3} className="text-center text-lp-text-faint py-6 italic">No model data.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Recent sessions */}
        <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5">
          <div className="text-xs font-bold text-lp-text-dim uppercase tracking-widest mb-4">
            Recent sessions
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm border-collapse min-w-[800px]">
              <thead>
                <tr className="text-lp-text-faint text-[0.68rem] uppercase tracking-wider">
                  {["When", "Tool", "Repo", "Status", "Msgs", "Summary"].map((h) => (
                    <th key={h} className="text-left py-2 px-3 border-b border-lp-border font-semibold">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {recentRows.map((r) => (
                  <tr key={r.session_id} className="border-b border-lp-border-dim last:border-b-0 hover:bg-lp-amber-glow transition-colors">
                    <td className="py-2 px-3 font-mono text-xs text-lp-text-faint whitespace-nowrap">
                      <Link href={`/sessions/${r.session_id}`} className="text-lp-text hover:text-lp-amber no-underline">
                        {fmtTs(r.first_timestamp)}
                      </Link>
                    </td>
                    <td className="py-2 px-3"><SourceBadge source={r.source} /></td>
                    <td className="py-2 px-3 text-lp-text-dim max-w-[150px] truncate font-mono text-xs">
                      {r.repo_name || displayProject(r.project)}
                    </td>
                    <td className="py-2 px-3"><StatusBadge status={r.session_status} /></td>
                    <td className="py-2 px-3">{(r.user_message_count ?? 0) + (r.assistant_message_count ?? 0)}</td>
                    <td className="py-2 px-3 max-w-[400px]">
                      <Link href={`/sessions/${r.session_id}`} className="text-lp-text hover:text-lp-amber no-underline block">
                        {r.session_summary ? (
                          <span className="text-sm line-clamp-2">{r.session_summary}</span>
                        ) : r.session_goal ? (
                          <span className="text-sm line-clamp-2 text-lp-text-dim">{r.session_goal}</span>
                        ) : (
                          <span className="text-xs text-lp-text-faint italic">
                            {r.tool_call_count ?? 0} tool calls · {fmtDuration(r.duration_seconds)}
                          </span>
                        )}
                      </Link>
                    </td>
                  </tr>
                ))}
                {recentRows.length === 0 && (
                  <tr><td colSpan={6} className="text-center text-lp-text-faint py-10 italic">No public sessions yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </>
  );
}

function MiniStat({ icon, value, label, alert }: {
  icon: React.ReactNode;
  value: string;
  label: string;
  alert?: string;
}) {
  return (
    <div className="bg-lp-surface border border-lp-border-dim rounded-lg px-4 py-3 flex items-center gap-3">
      <div className="text-lp-text-faint">{icon}</div>
      <div>
        <div className="font-mono text-sm font-medium text-lp-text">{value}</div>
        <div className="text-[0.62rem] uppercase tracking-wider text-lp-text-faint font-semibold">
          {label}
        </div>
        {alert && (
          <div className="text-[0.6rem] text-lp-red mt-0.5">{alert}</div>
        )}
      </div>
    </div>
  );
}
