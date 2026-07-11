import { Topbar } from "@/components/topbar";
import { getUsers } from "@/lib/db";
import { fmtNum, fmtTokens, fmtTs } from "@/lib/format";
import Link from "next/link";

export const dynamic = "force-dynamic";

export default function PeoplePage() {
  const users = getUsers();

  return (
    <>
      <Topbar title="People" />
      <div className="p-7 max-w-[1400px] animate-fade-up">
        <div className="grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-4">
          {users.map((u) => {
            const displayName = u.display_name || u.username;
            return (
            <Link
              key={u.username}
              href={`/u/${u.username}`}
              className="bg-lp-surface border border-lp-border-dim rounded-lg p-5 flex flex-col gap-3 no-underline text-inherit hover-glow"
            >
              <div className="font-brand text-xl font-bold text-lp-text">
                @{u.username}
              </div>
              {displayName !== u.username && (
                <div className="text-sm text-lp-text-dim">{displayName}</div>
              )}
              <div className="flex gap-4 flex-wrap">
                <StatMini value={fmtNum(u.sessions)} label="sessions" />
                <StatMini value={fmtNum(u.messages)} label="messages" />
                <StatMini value={fmtNum(u.tool_calls)} label="tool calls" />
                <StatMini value={fmtTokens(u.tokens)} label="tokens" />
              </div>
              <div className="text-xs text-lp-text-faint font-mono">
                {fmtTs(u.first_seen)} &mdash; {fmtTs(u.last_seen)}
              </div>
            </Link>
            );
          })}
          {users.length === 0 && (
            <div className="text-center text-lp-text-faint py-10 italic col-span-full">
              No users indexed yet.
            </div>
          )}
        </div>
      </div>
    </>
  );
}

function StatMini({ value, label }: { value: string; label: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="font-mono text-base font-medium text-lp-text">{value}</span>
      <span className="text-[0.65rem] uppercase tracking-wider text-lp-text-faint font-semibold">
        {label}
      </span>
    </div>
  );
}
