import Link from "next/link";
import {
  ORIGIN_LENSES,
  originLensLabel,
  withOriginQuery,
  type OriginLens,
} from "@/lib/origin-lens";

/**
 * Workflow lens chip bar — consistent filter UI across the top-level pages
 * that scope analytics by session origin (dashboard, analysis, publish, profile).
 *
 * Sessions list uses a dropdown instead because origin is one of many filters there.
 */
export function WorkflowLensBar({
  basePath,
  originLens,
  extraParams,
}: {
  basePath: string;
  originLens: OriginLens;
  /** Extra query params to preserve when switching lens (publish queue uses this). */
  extraParams?: Record<string, string | undefined>;
}) {
  return (
    <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
      <div>
        <div className="text-[0.68rem] text-lp-amber-dim uppercase tracking-[1.2px] font-bold mb-1">
          workflow lens
        </div>
        <div className="text-sm text-lp-text-faint">
          Showing <span className="text-lp-text font-medium">{originLensLabel(originLens)}</span>.
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        {ORIGIN_LENSES.map((lens) => {
          const active = lens.value === originLens;
          return (
            <Link
              key={lens.value}
              href={withOriginQuery(basePath, lens.value, extraParams)}
              aria-current={active ? "page" : undefined}
              className={`inline-flex items-center justify-center min-w-[90px] px-3 py-1.5 rounded-full border text-sm font-medium no-underline transition-all ${
                active
                  ? "border-lp-amber bg-lp-amber-glow text-lp-amber"
                  : "border-lp-border text-lp-text-dim bg-lp-bg/60 hover:border-lp-amber hover:text-lp-amber"
              }`}
            >
              {lens.label}
            </Link>
          );
        })}
      </div>
    </div>
  );
}
