import type { SessionStatus } from "@/lib/types";

const STATUS_STYLES: Record<string, { bg: string; text: string; border: string; label: string }> = {
  success:     { bg: "bg-lp-green/10",  text: "text-lp-green",    border: "border-lp-green/25",    label: "success" },
  partial:     { bg: "bg-lp-amber/10",  text: "text-lp-amber",    border: "border-lp-amber/25",    label: "partial" },
  failed:      { bg: "bg-lp-red/10",    text: "text-lp-red",      border: "border-lp-red/25",      label: "failed" },
  exploration: { bg: "bg-lp-blue/10",   text: "text-lp-blue",     border: "border-lp-blue/25",     label: "exploration" },
};

export function StatusBadge({ status }: { status: SessionStatus | string | null }) {
  const normalized = (status || "exploration").toLowerCase();
  const style = STATUS_STYLES[normalized] ?? STATUS_STYLES.exploration;

  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-[0.65rem] font-semibold font-mono tracking-wide border ${style.bg} ${style.text} ${style.border}`}
    >
      {style.label}
    </span>
  );
}

const VIS_STYLES: Record<string, { bg: string; text: string; border: string }> = {
  public:   { bg: "bg-lp-green/10",  text: "text-lp-green",    border: "border-lp-green/25" },
  unlisted: { bg: "bg-lp-amber/10",  text: "text-lp-amber",    border: "border-lp-amber/25" },
  private:  { bg: "bg-lp-text-faint/10", text: "text-lp-text-faint", border: "border-lp-text-faint/25" },
};

export function VisibilityBadge({ visibility }: { visibility: string }) {
  const style = VIS_STYLES[visibility] ?? VIS_STYLES.private;
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-[0.65rem] font-semibold font-mono tracking-wide border ${style.bg} ${style.text} ${style.border}`}
    >
      {visibility}
    </span>
  );
}

const REC_STYLES: Record<string, { bg: string; text: string; border: string; label: string }> = {
  public:   { bg: "bg-lp-green/10",  text: "text-lp-green",  border: "border-lp-green/25",  label: "publish" },
  unlisted: { bg: "bg-lp-amber/10",  text: "text-lp-amber",  border: "border-lp-amber/25",  label: "unlisted" },
  private:  { bg: "bg-lp-red/10",    text: "text-lp-red",    border: "border-lp-red/25",    label: "keep private" },
};

export function RecommendationBadge({ recommendation }: { recommendation?: string }) {
  if (!recommendation) return null;
  const style = REC_STYLES[recommendation] ?? REC_STYLES.private;
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-[0.65rem] font-bold tracking-wide border ${style.bg} ${style.text} ${style.border}`}
    >
      {style.label}
    </span>
  );
}
