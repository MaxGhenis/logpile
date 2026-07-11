import { IconBrandGithub, IconDownload } from "@tabler/icons-react";

/**
 * Intro card shown on the dashboard in public mode.
 * Gives first-time visitors to logpile.ai context for what they're looking at.
 * Hidden in private/local mode (Max doesn't need to be told what his own tool does).
 */
export function PublicIntro() {
  return (
    <div className="relative overflow-hidden mb-4 rounded-lg border border-lp-border-dim bg-[radial-gradient(ellipse_at_top_left,rgba(245,158,11,0.08),transparent_55%),var(--color-lp-surface)] px-5 py-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="min-w-0 max-w-[640px]">
          <div className="text-[0.68rem] font-bold uppercase tracking-[1.4px] text-lp-amber-dim mb-1.5">
            public logpile
          </div>
          <p className="text-sm text-lp-text leading-relaxed">
            Browsable archive of <span className="font-medium">Claude Code</span> and{" "}
            <span className="font-medium">Codex</span> sessions — indexed by repo, activity,
            and workflow origin.
          </p>
          <p className="text-xs text-lp-text-faint mt-1 leading-relaxed">
            This page shows public sessions only. Private and unlisted sessions are
            excluded from these totals. Logpile runs locally — see GitHub to index
            your own.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <a
            href="https://github.com/MaxGhenis/logpile"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 rounded-full border border-lp-border bg-lp-bg/60 px-3.5 py-1.5 text-sm font-medium text-lp-text-dim no-underline hover:border-lp-amber hover:text-lp-amber hover:bg-lp-amber-glow transition-all"
          >
            <IconBrandGithub size={14} stroke={1.75} />
            GitHub
          </a>
          <a
            href="https://github.com/MaxGhenis/logpile#quick-start"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 rounded-full border border-lp-amber bg-lp-amber-glow px-3.5 py-1.5 text-sm font-semibold text-lp-amber no-underline hover:bg-lp-amber hover:text-lp-bg transition-all"
          >
            <IconDownload size={14} stroke={1.75} />
            Install
          </a>
        </div>
      </div>
    </div>
  );
}
