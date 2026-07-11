/** Format an ISO timestamp to "YYYY-MM-DD HH:MM" for display. */
export function fmtTs(ts: string | null | undefined): string {
  if (!ts) return "\u2014";
  try {
    const d = new Date(ts.endsWith("Z") ? ts : ts.replace("Z", "+00:00"));
    if (isNaN(d.getTime())) return ts.slice(0, 16);
    return d.toISOString().slice(0, 16).replace("T", " ");
  } catch {
    return ts.slice(0, 16);
  }
}

/** Format seconds into a human-readable duration like "5m 23s" or "1h 12m". */
export function fmtDuration(secs: number | null | undefined): string {
  if (secs == null) return "\u2014";
  const s = Math.round(secs);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

/** Extract the leaf directory name from a project path. */
export function displayProject(project: string | null | undefined): string {
  if (!project || project === "unknown") return "unknown";
  const parts = project.replace(/\\/g, "/").split("/");
  return parts[parts.length - 1] || project;
}

/** Format a number with commas: 1234567 -> "1,234,567". */
export function fmtNum(n: number | null | undefined): string {
  if (n == null) return "0";
  return n.toLocaleString("en-US");
}

/** Format tokens as compact: 9842000000 -> "9.8B", 12400000 -> "12M". */
export function fmtTokens(n: number | null | undefined): string {
  if (n == null || n === 0) return "0";
  if (n >= 1_000_000_000) {
    const b = n / 1_000_000_000;
    return `${b >= 100 ? fmtNum(Math.round(b)) : b.toFixed(1)}B`;
  }
  if (n >= 1_000_000) return `${fmtNum(Math.round(n / 1_000_000))}M`;
  if (n >= 1_000) return `${fmtNum(Math.round(n / 1_000))}K`;
  return fmtNum(n);
}

/**
 * Session title for list rows: prefer the operator's ask, falling back to the
 * enriched summary. Harness-injected preambles (e.g. Codex's leading
 * <recommended_plugins> block, which the enricher can also copy into the goal,
 * and whose stored prefix never reaches the real prompt) count as empty.
 */
function stripHarnessPreamble(s: string | null | undefined): string {
  let t = (s ?? "").trim();
  if (!t) return "";
  if (/^<recommended_plugins\b/i.test(t)) return "";
  // Drop other leading <tag>...</tag> blocks and orphan <tag> lines.
  for (let i = 0; i < 4 && t.startsWith("<"); i++) {
    const tag = t.match(/^<([a-zA-Z][\w-]*)[^>]*>/);
    if (!tag) break;
    const close = t.indexOf(`</${tag[1]}>`);
    t = close !== -1 ? t.slice(close + tag[1].length + 3).trim() : t.slice(tag[0].length).trim();
  }
  return t;
}

export function sessionTitle(
  goal: string | null | undefined,
  summary: string | null | undefined,
  firstUserMessage: string | null | undefined
): string {
  return (
    stripHarnessPreamble(goal) ||
    (summary ?? "").trim() ||
    stripHarnessPreamble(firstUserMessage) ||
    "—"
  );
}

/** Truncate a string with an ellipsis. */
export function truncate(s: string, len: number): string {
  if (s.length <= len) return s;
  return s.slice(0, len) + "\u2026";
}
