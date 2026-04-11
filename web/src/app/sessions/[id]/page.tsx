import { notFound } from "next/navigation";
import { Topbar } from "@/components/topbar";
import { SourceBadge, UserBadge } from "@/components/badge";
import { Transcript } from "@/components/transcript/transcript";
import { getSession } from "@/lib/db";
import { fmtTs, fmtDuration, fmtNum, displayProject } from "@/lib/format";
import { renderClaudecodeTranscript, renderCodexTranscript } from "@/lib/parsers";
import { config } from "@/lib/config";
import fs from "fs";
import path from "path";
import type { Turn } from "@/lib/parsers";

export const dynamic = "force-dynamic";

export default async function SessionDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const session = getSession(id);
  if (!session) notFound();

  if (session.visibility === "private") {
    notFound();
  }

  if (config.publicMode && session.user_profile_visibility === "private") {
    notFound();
  }

  // Find transcript file
  let turns: Turn[] = [];
  const candidates = [
    session.shared_path,
    // Try resolving relative to shared dir
    session.shared_path
      ? path.join(
          config.sharedDir,
          session.username,
          session.source,
          displayProject(session.project),
          path.basename(session.shared_path)
        )
      : null,
    session.source_path,
  ].filter(Boolean) as string[];

  const filePath = candidates.find((p) => {
    try {
      return fs.existsSync(p);
    } catch {
      return false;
    }
  });

  if (filePath) {
    try {
      turns =
        session.source === "claudecode"
          ? renderClaudecodeTranscript(filePath)
          : renderCodexTranscript(filePath);
    } catch (e) {
      turns = [{ type: "error", content: `Failed to parse transcript: ${e}` }];
    }
  }

  return (
    <>
      <Topbar title="Session" />
      <div className="p-7 max-w-[1400px] animate-fade-up">
        <SessionHeader session={session} />
        <Transcript turns={turns} />
      </div>
    </>
  );
}

function SessionHeader({ session }: { session: ReturnType<typeof getSession> & {} }) {
  return (
    <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5 mb-5">
      <div className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-4">
        <Meta label="ID" value={<span className="font-mono">{session.session_id.slice(0, 20)}...</span>} />
        <Meta
          label="Operator"
          value={
            <UserBadge
              slug={session.user_slug || session.username}
              displayName={session.user_display_name}
            />
          }
        />
        <Meta label="Tool" value={<SourceBadge source={session.source} />} />
        <Meta label="Project" value={displayProject(session.project)} />
        <Meta label="Started" value={<span className="font-mono">{fmtTs(session.first_timestamp)}</span>} />
        <Meta label="Duration" value={<span className="font-mono">{fmtDuration(session.duration_seconds)}</span>} />
        {session.model && (
          <Meta label="Model" value={<span className="font-mono">{session.model}</span>} />
        )}
        <Meta
          label="Messages"
          value={`${session.user_message_count} user / ${session.assistant_message_count} assistant`}
        />
        <Meta
          label="Tool calls"
          value={
            <>
              {session.tool_call_count}
              {session.error_count > 0 && (
                <span className="text-lp-red ml-1">({session.error_count} errors)</span>
              )}
            </>
          }
        />
        {session.total_input_tokens > 0 && (
          <Meta
            label="Tokens"
            value={
              <span className="font-mono">
                {fmtNum(session.total_input_tokens)} in / {fmtNum(session.total_output_tokens)} out
              </span>
            }
          />
        )}
      </div>
    </div>
  );
}

function Meta({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[0.68rem] uppercase tracking-wider text-lp-text-faint font-semibold">
        {label}
      </span>
      <span className="text-sm text-lp-text">{value}</span>
    </div>
  );
}
