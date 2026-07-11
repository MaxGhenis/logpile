import Link from "next/link";
import { notFound } from "next/navigation";
import { Topbar } from "@/components/topbar";
import { SourceBadge, UserBadge } from "@/components/badge";
import { StatusBadge } from "@/components/status-badge";
import { ActivityBadges } from "@/components/activity-badges";
import { Transcript } from "@/components/transcript/transcript";
import { getSession } from "@/lib/db";
import { fmtTs, fmtDuration, fmtNum, displayProject } from "@/lib/format";
import { renderClaudecodeTranscript, renderCodexTranscript } from "@/lib/parsers";
import { config } from "@/lib/config";
import { constants as fsConstants, existsSync, promises as fs } from "fs";
import { createHash } from "crypto";
import path from "path";
import type { FileHandle } from "fs/promises";
import type { TranscriptPage, Turn } from "@/lib/parsers";

export const dynamic = "force-dynamic";

// Cursor pagination must not turn a multi-gigabyte reviewed transcript into a
// full-file rehash on every page.  Cache only successful verifications and tie
// them to the opened file's immutable identity/change metadata.  Path
// containment and no-symlink checks still run for every request, and any
// replacement or write (inode/size/ctime/mtime change) forces a fresh hash.
const PUBLIC_ARTIFACT_VERIFICATION_CACHE_LIMIT = 128;
const verifiedPublicArtifacts = new Map<string, true>();

function hasCachedArtifactVerification(key: string): boolean {
  if (!verifiedPublicArtifacts.delete(key)) return false;
  // Reinsert to keep the bounded map in least-recently-used order.
  verifiedPublicArtifacts.set(key, true);
  return true;
}

function cacheArtifactVerification(key: string): void {
  verifiedPublicArtifacts.delete(key);
  verifiedPublicArtifacts.set(key, true);
  while (verifiedPublicArtifacts.size > PUBLIC_ARTIFACT_VERIFICATION_CACHE_LIMIT) {
    const oldest = verifiedPublicArtifacts.keys().next().value;
    if (oldest === undefined) break;
    verifiedPublicArtifacts.delete(oldest);
  }
}

function currentPublicationMetadataSha256(
  session: NonNullable<ReturnType<typeof getSession>>,
): string {
  // Keep this ordered string/null schema aligned with
  // logpile.publish.PUBLICATION_METADATA_FIELDS.  Avoid JS/Python numeric
  // representation differences in this cross-runtime fingerprint.
  const canonical: Array<[string, string | null]> = [
    ["source", session.source],
    ["username", session.username],
    ["display_name", session.user_display_name],
    ["bio", session.user_bio],
    ["avatar_url", session.user_avatar_url],
    ["machine", session.machine],
    ["project", session.project],
    ["repo_name", session.repo_name],
    ["git_branch", session.git_branch],
    ["git_commit", session.git_commit],
    ["first_timestamp", session.first_timestamp],
    ["last_timestamp", session.last_timestamp],
    ["session_goal", session.session_goal],
    ["session_summary", session.session_summary],
    ["session_outcome", session.session_outcome],
    ["session_status", session.session_status],
    ["objective_family", session.objective_family],
    ["objective_label", session.objective_label],
    ["session_origin", session.session_origin],
    ["first_user_message", session.first_user_message],
    ["model", session.model],
  ];
  return createHash("sha256").update(JSON.stringify(canonical), "utf8").digest("hex");
}

export default async function SessionDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams?: Promise<{ cursor?: string }>;
}) {
  const { id } = await params;
  const requestedCursor = (await searchParams)?.cursor;
  const parsedCursor = Number(requestedCursor ?? "0");
  const cursor = Number.isSafeInteger(parsedCursor) && parsedCursor >= 0
    ? parsedCursor
    : 0;
  const session = getSession(id);
  if (!session) notFound();

  if (session.visibility === "private") {
    notFound();
  }

  if (config.publicMode && session.user_profile_visibility === "private") {
    notFound();
  }
  if (config.publicMode && session.visibility !== "public") {
    notFound();
  }

  // Resolve the artifact separately from bounded transcript parsing. Public
  // mode tightens only this resolver to a verified reviewed artifact.
  let turns: Turn[] = [];
  let transcriptPage: TranscriptPage | null = null;
  const transcriptSource = await resolveTranscriptPath(session);
  if (config.publicMode && !transcriptSource) {
    notFound();
  }

  if (transcriptSource) {
    try {
      transcriptPage = await (
        session.source === "claudecode"
          ? renderClaudecodeTranscript(transcriptSource, { cursor })
          : renderCodexTranscript(transcriptSource, { cursor })
      );
      turns = transcriptPage.turns;
    } catch (e) {
      turns = [{ type: "error", content: `Failed to parse transcript: ${e}` }];
    } finally {
      if (typeof transcriptSource !== "string") {
        await transcriptSource.close();
      }
    }
  }

  const hasNarrative =
    !!session.session_goal ||
    !!session.session_summary ||
    !!session.session_outcome ||
    !!session.session_status;

  const hasActivity =
    (session.write_path_count ?? 0) > 0 ||
    (session.test_run_count ?? 0) > 0 ||
    (session.build_run_count ?? 0) > 0 ||
    (session.git_commit_count ?? 0) > 0;

  return (
    <>
      <Topbar title="Session" />
      <div className="p-7 max-w-[1400px] animate-fade-up">
        {/* Narrative header — status, goal, summary, outcome */}
        {hasNarrative && (
          <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5 mb-4">
            <div className="flex items-center gap-2 mb-3 flex-wrap">
              {session.session_status && <StatusBadge status={session.session_status} />}
              <SourceBadge source={session.source} />
              {session.repo_name && (
                <span className="font-mono text-xs text-lp-text-dim">{session.repo_name}</span>
              )}
              {session.git_branch && (
                <span className="font-mono text-xs text-lp-text-faint">
                  {session.git_branch}
                </span>
              )}
              <span className="ml-auto text-xs text-lp-text-faint font-mono">
                {fmtTs(session.first_timestamp)}
              </span>
            </div>
            {session.session_goal && (
              <div className="text-base font-medium text-lp-text mb-2">
                {session.session_goal}
              </div>
            )}
            {session.session_summary && (
              <div className="text-sm text-lp-text-dim leading-relaxed mb-2">
                {session.session_summary}
              </div>
            )}
            {session.session_outcome && (
              <div className="text-sm text-lp-text-faint italic">
                Outcome: {session.session_outcome}
              </div>
            )}
            {hasActivity && (
              <div className="mt-3 pt-3 border-t border-lp-border-dim">
                <ActivityBadges
                  write_path_count={session.write_path_count}
                  test_run_count={session.test_run_count}
                  test_failure_count={session.test_failure_count}
                  build_run_count={session.build_run_count}
                  build_failure_count={session.build_failure_count}
                  git_commit_count={session.git_commit_count}
                />
              </div>
            )}
          </div>
        )}

        <SessionHeader session={session} hasNarrative={hasNarrative} />
        <Transcript turns={turns} />
        {transcriptPage && (
          <div className="mt-4 flex items-center justify-between gap-3 text-xs text-lp-text-faint">
            <span>
              Loaded {turns.length} turn{turns.length === 1 ? "" : "s"}
              {transcriptPage.byteLimitReached ? " within the per-page read cap" : ""}.
            </span>
            <div className="flex items-center gap-3">
              {cursor > 0 && (
                <Link
                  href={`/sessions/${encodeURIComponent(session.session_id)}`}
                  className="text-lp-text-dim hover:text-lp-text"
                >
                  First page
                </Link>
              )}
              {transcriptPage.nextCursor !== null && (
                <Link
                  href={`/sessions/${encodeURIComponent(session.session_id)}?cursor=${transcriptPage.nextCursor}`}
                  className="text-lp-green hover:text-lp-green-bright font-medium"
                >
                  Next turns →
                </Link>
              )}
            </div>
          </div>
        )}
      </div>
    </>
  );
}

async function resolveTranscriptPath(
  session: NonNullable<ReturnType<typeof getSession>>,
): Promise<string | FileHandle | undefined> {
  if (config.publicMode) {
    return resolveVerifiedPublicArtifact(session);
  }
  const candidates = [
    session.shared_path,
    session.shared_path
      ? path.join(
          config.sharedDir,
          session.username,
          session.source,
          displayProject(session.project),
          path.basename(session.shared_path),
        )
      : null,
    session.source_path,
  ].filter(Boolean) as string[];

  return candidates.find((candidate) => {
    try {
      return existsSync(candidate);
    } catch {
      return false;
    }
  });
}

async function resolveVerifiedPublicArtifact(
  session: NonNullable<ReturnType<typeof getSession>>,
): Promise<FileHandle | undefined> {
  const expectedHash = session.reviewed_sha256;
  const rawArtifact = session.reviewed_artifact_path;
  if (
    !session.reviewed_metadata_sha256
    || session.publication_metadata_sha256 !== session.reviewed_metadata_sha256
    || currentPublicationMetadataSha256(session) !== session.reviewed_metadata_sha256
  ) {
    return undefined;
  }
  if (!expectedHash || !/^[0-9a-f]{64}$/.test(expectedHash) || !rawArtifact) {
    return undefined;
  }

  const sharedRoot = path.resolve(config.sharedDir);
  const publishRoot = path.join(sharedRoot, ".published");
  const artifact = path.resolve(rawArtifact);
  const relative = path.relative(publishRoot, artifact);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    return undefined;
  }

  try {
    const sharedStat = await fs.lstat(sharedRoot);
    if (sharedStat.isSymbolicLink() || !sharedStat.isDirectory()) return undefined;
    const rootStat = await fs.lstat(publishRoot);
    if (rootStat.isSymbolicLink() || !rootStat.isDirectory()) return undefined;
    let current = publishRoot;
    const parts = relative.split(path.sep);
    for (let index = 0; index < parts.length; index += 1) {
      current = path.join(current, parts[index]);
      const currentStat = await fs.lstat(current);
      if (currentStat.isSymbolicLink()) return undefined;
      if (index < parts.length - 1 && !currentStat.isDirectory()) return undefined;
      if (index === parts.length - 1 && !currentStat.isFile()) return undefined;
    }

    const noFollow = "O_NOFOLLOW" in fsConstants ? fsConstants.O_NOFOLLOW : 0;
    const handle = await fs.open(artifact, fsConstants.O_RDONLY | noFollow);
    try {
      const openedStat = await handle.stat({ bigint: true });
      if (!openedStat.isFile()) {
        await handle.close();
        return undefined;
      }
      const verificationKey = [
        artifact,
        expectedHash,
        openedStat.dev,
        openedStat.ino,
        openedStat.mode,
        openedStat.size,
        openedStat.mtimeNs,
        openedStat.ctimeNs,
      ].join("\0");
      if (hasCachedArtifactVerification(verificationKey)) {
        return handle;
      }
      const digest = createHash("sha256");
      const buffer = Buffer.allocUnsafe(1024 * 1024);
      let position = 0;
      while (true) {
        const { bytesRead } = await handle.read(buffer, 0, buffer.length, position);
        if (bytesRead === 0) break;
        digest.update(buffer.subarray(0, bytesRead));
        position += bytesRead;
      }
      if (digest.digest("hex") !== expectedHash) {
        await handle.close();
        return undefined;
      }
      cacheArtifactVerification(verificationKey);
      return handle;
    } catch {
      await handle.close();
      return undefined;
    }
  } catch {
    return undefined;
  }
}

function SessionHeader({
  session,
  hasNarrative,
}: {
  session: ReturnType<typeof getSession> & {};
  hasNarrative: boolean;
}) {
  return (
    <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5 mb-5">
      <div className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-4">
        <Meta label="ID" value={<span className="font-mono">{session.session_id.slice(0, 20)}...</span>} />
        <Meta
          label="Operator"
          value={
            <UserBadge
              username={session.username}
              displayName={session.user_display_name}
            />
          }
        />
        {/* Only show these in metadata grid if not already in narrative header */}
        {!hasNarrative && <Meta label="Tool" value={<SourceBadge source={session.source} />} />}
        <Meta label="Project" value={displayProject(session.project)} />
        {!hasNarrative && session.repo_name && (
          <Meta label="Repo" value={<span className="font-mono">{session.repo_name}</span>} />
        )}
        {!hasNarrative && (
          <Meta label="Started" value={<span className="font-mono">{fmtTs(session.first_timestamp)}</span>} />
        )}
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
