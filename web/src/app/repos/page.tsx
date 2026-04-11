import { Topbar } from "@/components/topbar";
import { getRepos } from "@/lib/db";
import { fmtNum, fmtTs } from "@/lib/format";
import Link from "next/link";
import {
  IconGitBranch,
  IconFolder,
  IconRoute,
} from "@tabler/icons-react";

export const dynamic = "force-dynamic";

export default function ReposPage() {
  const repos = getRepos({ limit: 100 });

  return (
    <>
      <Topbar title="Repos" />
      <div className="p-7 max-w-[1400px] animate-fade-up">
        <div className="text-xs font-mono text-lp-text-faint mb-4">
          {fmtNum(repos.length)} repo{repos.length !== 1 ? "s" : ""}
        </div>

        <div className="grid grid-cols-[repeat(auto-fill,minmax(320px,1fr))] gap-4">
          {repos.map((r) => (
            <Link
              key={`${r.repo_name}:${r.repo_root ?? "public"}`}
              href={`/sessions?repo=${encodeURIComponent(r.repo_name)}${r.repo_root ? `&repoRoot=${encodeURIComponent(r.repo_root)}` : ""}`}
              className="bg-lp-surface border border-lp-border-dim rounded-lg p-5 flex flex-col gap-3 no-underline text-inherit hover-glow"
            >
              <div className="flex items-center gap-2">
                <IconFolder size={16} stroke={1.5} className="text-lp-amber flex-shrink-0" />
                <div className="font-mono text-sm font-bold text-lp-text truncate">
                  {r.repo_name}
                </div>
              </div>

              <div className="flex gap-4 flex-wrap">
                <RepoStat value={fmtNum(r.sessions)} label="sessions" />
                <RepoStat value={fmtNum(r.messages)} label="messages" />
                <RepoStat value={fmtNum(r.tool_calls)} label="tool calls" />
              </div>

              <div className="flex gap-4 text-xs text-lp-text-faint">
                <span className="flex items-center gap-1">
                  <IconGitBranch size={12} stroke={1.5} />
                  {r.branches} branch{r.branches !== 1 ? "es" : ""}
                </span>
                {r.worktrees > 1 && (
                  <span className="flex items-center gap-1">
                    <IconRoute size={12} stroke={1.5} />
                    {r.worktrees} worktrees
                  </span>
                )}
                {r.unique_paths > 0 && (
                  <span>{fmtNum(r.unique_paths)} paths</span>
                )}
              </div>

              <div className="text-[0.68rem] text-lp-text-faint font-mono">
                last seen {fmtTs(r.last_seen)}
              </div>
            </Link>
          ))}

          {repos.length === 0 && (
            <div className="text-center text-lp-text-faint py-16 italic col-span-full">
              <div className="text-sm mb-2">No repos detected yet.</div>
              <div className="text-xs">
                Run <code className="text-lp-amber">logpile sync</code> to index sessions.
                Repo metadata is extracted from git context in session files.
              </div>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

function RepoStat({ value, label }: { value: string; label: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="font-mono text-sm font-medium text-lp-text">{value}</span>
      <span className="text-[0.62rem] uppercase tracking-wider text-lp-text-faint font-semibold">
        {label}
      </span>
    </div>
  );
}
