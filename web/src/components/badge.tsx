import Link from "next/link";

export function SourceBadge({ source }: { source: string }) {
  const isCC = source === "claudecode";
  return (
    <span
      className={`rounded px-2 py-0.5 text-[0.7rem] font-bold font-mono tracking-wide border ${
        isCC
          ? "bg-lp-amber/10 text-lp-amber border-lp-amber/25"
          : "bg-lp-blue/10 text-lp-blue border-lp-blue/25"
      }`}
    >
      {isCC ? "CC" : "Codex"}
    </span>
  );
}

export function UserBadge({
  username,
  displayName,
}: {
  username: string;
  displayName: string;
}) {
  return (
    <Link href={`/u/${username}`} className="no-underline group">
      <span className="bg-lp-raised border border-lp-border-dim rounded px-2 py-0.5 text-xs font-medium text-lp-text group-hover:border-lp-amber group-hover:text-lp-amber transition-colors">
        {displayName}
      </span>
    </Link>
  );
}
