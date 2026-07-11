import { Topbar } from "@/components/topbar";
import { SourceBadge, UserBadge } from "@/components/badge";
import { StatusBadge } from "@/components/status-badge";
import { ActivityBadges } from "@/components/activity-badges";
import { getSessions, getUsersForFilter, getReposForFilter } from "@/lib/db";
import { fmtTs, fmtNum, displayProject, truncate } from "@/lib/format";
import { ACTIVITY_FILTERS, SESSION_ORIGINS, SESSION_STATUSES } from "@/lib/types";
import Link from "next/link";

export const dynamic = "force-dynamic";

const ACTIVITY_LABELS: Record<string, string> = {
  write: "Wrote files",
  test: "Ran tests",
  test_failed: "Test failures",
  build: "Ran build",
  build_failed: "Build failures",
  git_commit: "Git commits",
  error: "Errors",
  read: "Read files",
  search: "Searched",
  lint: "Ran lint",
  lint_failed: "Lint failures",
  format: "Ran format",
  format_failed: "Format failures",
  git_status: "Git status",
  git_diff: "Git diff",
};

const ORIGIN_LABELS: Record<string, string> = {
  human_direct: "Human direct",
  human_delegated: "Human delegated",
  pipeline_eval: "Pipeline eval",
  meta_scaffolding: "Meta scaffolding",
  system_generated: "System generated",
};

export default async function SessionsPage({
  searchParams,
}: {
  searchParams: Promise<{
    q?: string; source?: string; user?: string; repo?: string;
    repoRoot?: string; branch?: string; activity?: string; status?: string; origin?: string; objective?: string; objectiveLabel?: string; page?: string;
  }>;
}) {
  const params = await searchParams;
  const q = params.q || "";
  const source = params.source || "";
  const user = params.user || "";
  const repo = params.repo || "";
  const repoRoot = params.repoRoot || "";
  const branch = params.branch || "";
  const activity = params.activity || "";
  const status = params.status || "";
  const origin = params.origin || "";
  const objective = params.objective || "";
  const objectiveLabel = params.objectiveLabel || "";
  const parsedPage = Number.parseInt(params.page || "1", 10);
  const page = Number.isFinite(parsedPage) && parsedPage > 0 ? parsedPage : 1;

  let invalidActivityFilter = false;
  let invalidOriginFilter = false;
  let invalidObjectiveFilter = false;
  let rows = [] as Awaited<ReturnType<typeof getSessions>>["rows"];
  let total = 0;
  let perPage = 50;
  try {
    ({ rows, total, perPage } = getSessions({ q, objective, source, user, repo, repoRoot, branch, activity, status, origin, page }));
  } catch (error) {
    if (error instanceof RangeError) {
      if (error.message.includes("origin")) {
        invalidOriginFilter = true;
      } else if (error.message.includes("objective")) {
        invalidObjectiveFilter = true;
      } else {
        invalidActivityFilter = true;
      }
    } else {
      throw error;
    }
  }
  const users = getUsersForFilter();
  const repos = getReposForFilter();
  const totalPages = Math.ceil(total / perPage);

  function filterUrl(overrides: Record<string, string | number>) {
    const p = { q, source, user, repo, repoRoot, branch, activity, status, origin, objective, objectiveLabel, page: "1", ...overrides };
    const qs = Object.entries(p)
      .filter(([, v]) => v)
      .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
      .join("&");
    return `/sessions${qs ? `?${qs}` : ""}`;
  }

  const hasFilters = q || source || user || repo || repoRoot || branch || activity || status || origin || objective;

  return (
    <>
      <Topbar title="Sessions" />
      <div className="p-7 max-w-[1400px] animate-fade-up">
        {/* Filter bar */}
        <form action="/sessions" method="get" className="flex gap-2 flex-wrap items-center mb-3">
          {objective && <input type="hidden" name="objective" value={objective} />}
          {objectiveLabel && <input type="hidden" name="objectiveLabel" value={objectiveLabel} />}
          <input
            type="text"
            name="q"
            defaultValue={q}
            placeholder="Search first message..."
            className="bg-lp-surface border border-lp-border-dim text-lp-text rounded-lg px-3.5 py-2 text-sm font-body flex-1 min-w-[200px] placeholder:text-lp-text-faint focus:outline-none focus:border-lp-amber focus:ring-2 focus:ring-lp-amber-glow"
          />
          <select name="source" defaultValue={source} className="bg-lp-surface border border-lp-border-dim text-lp-text rounded-lg px-3 py-2 text-sm font-body">
            <option value="">All tools</option>
            <option value="claudecode">CC</option>
            <option value="codex">Codex</option>
          </select>
          <select name="user" defaultValue={user} className="bg-lp-surface border border-lp-border-dim text-lp-text rounded-lg px-3 py-2 text-sm font-body">
            <option value="">All operators</option>
            {users.map((u) => (
              <option key={u.username} value={u.username}>{u.display_name}</option>
            ))}
          </select>
          {repos.length > 0 && (
            <select name="repo" defaultValue={repo} className="bg-lp-surface border border-lp-border-dim text-lp-text rounded-lg px-3 py-2 text-sm font-body">
              <option value="">All repos</option>
              {repos.map((r) => (
                <option key={r.repo_name} value={r.repo_name}>{r.repo_name}</option>
              ))}
            </select>
          )}
          <select name="activity" defaultValue={activity} className="bg-lp-surface border border-lp-border-dim text-lp-text rounded-lg px-3 py-2 text-sm font-body">
            <option value="">All activity</option>
            {ACTIVITY_FILTERS.map((f) => (
              <option key={f} value={f}>{ACTIVITY_LABELS[f] || f}</option>
            ))}
          </select>
          <select name="status" defaultValue={status} className="bg-lp-surface border border-lp-border-dim text-lp-text rounded-lg px-3 py-2 text-sm font-body">
            <option value="">All statuses</option>
            {SESSION_STATUSES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <select name="origin" defaultValue={origin} className="bg-lp-surface border border-lp-border-dim text-lp-text rounded-lg px-3 py-2 text-sm font-body">
            <option value="">All origins</option>
            {SESSION_ORIGINS.map((o) => (
              <option key={o} value={o}>{ORIGIN_LABELS[o] || o}</option>
            ))}
          </select>
          <button
            type="submit"
            className="bg-lp-amber text-lp-bg rounded-lg px-5 py-2 text-sm font-semibold font-body hover:bg-lp-amber-hot hover:shadow-[0_2px_12px_rgba(245,158,11,0.3)] transition-all cursor-pointer"
          >
            Filter
          </button>
          {hasFilters && (
            <Link href="/sessions" className="text-xs text-lp-text-faint hover:text-lp-amber no-underline">
              Clear
            </Link>
          )}
        </form>

        {objective && (
          <div className="mb-4 rounded-lg border border-lp-border-dim bg-lp-surface px-4 py-3 flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="text-[0.68rem] uppercase tracking-widest text-lp-text-faint font-semibold mb-1">
                Objective family
              </div>
              <div className="text-sm text-lp-text">
                {objectiveLabel || objective}
              </div>
            </div>
            <Link
              href={filterUrl({ objective: "", objectiveLabel: "", page: 1 })}
              className="text-sm text-lp-amber hover:text-lp-amber-hot no-underline"
            >
              Clear objective filter
            </Link>
          </div>
        )}

        <div className="text-xs font-mono text-lp-text-faint mb-3">
          {invalidActivityFilter
            ? "Invalid activity filter"
            : invalidOriginFilter
              ? "Invalid origin filter"
              : invalidObjectiveFilter
                ? "Invalid objective filter"
            : `${fmtNum(total)} session${total !== 1 ? "s" : ""}`}
        </div>

        {(invalidActivityFilter || invalidOriginFilter || invalidObjectiveFilter) && (
          <div className="mb-4 rounded-lg border border-lp-red/30 bg-lp-red/8 px-4 py-3 text-sm text-lp-text-dim">
            <div className="font-medium text-lp-text">
              {invalidOriginFilter
                ? `Unknown origin filter: ${origin}`
                : invalidObjectiveFilter
                  ? `Unknown objective filter: ${objective}`
                  : `Unknown activity filter: ${activity}`}
            </div>
            <div className="mt-1 text-lp-text-faint">
              Choose one of the supported filters from the menu or{" "}
              <Link href="/sessions" className="text-lp-amber hover:text-lp-amber-hot no-underline">
                clear the filters
              </Link>
              .
            </div>
          </div>
        )}

        {/* Table */}
        <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5">
          <div className="overflow-x-auto">
            <table className="w-full text-sm border-collapse min-w-[1000px]">
              <thead>
                <tr className="text-lp-text-faint text-[0.68rem] uppercase tracking-wider">
                  {["When", "Operator", "Tool", "Repo", "Status", "Activity", "Summary"].map(
                    (h) => (
                      <th key={h} className="text-left py-2.5 px-3 border-b border-lp-border font-semibold">
                        {h}
                      </th>
                    )
                  )}
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr
                    key={r.session_id}
                    className="border-b border-lp-border-dim last:border-b-0 hover:bg-lp-amber-glow transition-colors"
                  >
                    <td className="py-2.5 px-3 text-lp-text-faint font-mono text-xs whitespace-nowrap">
                      {fmtTs(r.first_timestamp)}
                    </td>
                    <td className="py-2.5 px-3">
                      <UserBadge username={r.username} displayName={r.user_display_name} />
                    </td>
                    <td className="py-2.5 px-3">
                      <SourceBadge source={r.source} />
                    </td>
                    <td className="py-2.5 px-3 max-w-[150px]">
                      {r.repo_name ? (
                        <Link
                          href={filterUrl({ repo: r.repo_name, repoRoot: r.repo_root || "", page: 1 })}
                          className="text-lp-text-dim hover:text-lp-amber no-underline font-mono text-xs"
                        >
                          {r.repo_name}
                        </Link>
                      ) : (
                        <span className="text-lp-text-faint text-xs">{displayProject(r.project)}</span>
                      )}
                      {r.git_branch && (
                        <div className="text-[0.65rem] text-lp-text-faint font-mono truncate">{r.git_branch}</div>
                      )}
                    </td>
                    <td className="py-2.5 px-3">
                      <StatusBadge status={r.session_status} />
                    </td>
                    <td className="py-2.5 px-3">
                      <ActivityBadges
                        write_path_count={r.write_path_count}
                        test_run_count={r.test_run_count}
                        test_failure_count={r.test_failure_count}
                        build_run_count={r.build_run_count}
                        build_failure_count={r.build_failure_count}
                        git_commit_count={r.git_commit_count}
                      />
                    </td>
                    <td className="py-2.5 px-3 max-w-[400px]">
                      <Link
                        href={`/sessions/${r.session_id}`}
                        className="text-lp-text hover:text-lp-amber no-underline transition-colors block"
                      >
                        {r.session_summary ? (
                          <span className="text-sm">{truncate(r.session_summary, 90)}</span>
                        ) : (
                          <span className="text-sm text-lp-text-faint">{truncate(r.first_user_message || "\u2014", 80)}</span>
                        )}
                      </Link>
                    </td>
                  </tr>
                ))}
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={7} className="text-center py-16">
                      <div className="text-lp-text-faint">
                        {invalidActivityFilter || invalidOriginFilter ? (
                          <div className="text-sm italic">Select valid filters to load sessions.</div>
                        ) : hasFilters ? (
                          <>
                            <div className="text-sm mb-2">No sessions match your filters.</div>
                            <Link href="/sessions" className="text-xs text-lp-amber hover:text-lp-amber-hot no-underline">
                              Clear all filters
                            </Link>
                          </>
                        ) : (
                          <div className="text-sm italic">
                            No sessions yet. Run <code className="text-lp-amber">logpile sync</code> to populate.
                          </div>
                        )}
                      </div>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center gap-4 mt-4 text-sm">
            {page > 1 && (
              <Link href={filterUrl({ page: page - 1 })} className="text-lp-amber hover:text-lp-amber-hot no-underline">
                &larr; prev
              </Link>
            )}
            <span className="font-mono text-lp-text-faint">page {page} / {totalPages}</span>
            {page < totalPages && (
              <Link href={filterUrl({ page: page + 1 })} className="text-lp-amber hover:text-lp-amber-hot no-underline">
                next &rarr;
              </Link>
            )}
          </div>
        )}
      </div>
    </>
  );
}
