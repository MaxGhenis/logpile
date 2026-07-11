import type { SessionOrigin } from "./types";

export type OriginLens = SessionOrigin | "all";

export const DEFAULT_ANALYTICS_ORIGIN: SessionOrigin = "human_direct";

export const ORIGIN_LENSES: Array<{ label: string; value: OriginLens }> = [
  { label: "My work", value: "human_direct" },
  { label: "Delegated", value: "human_delegated" },
  { label: "Pipeline", value: "pipeline_eval" },
  { label: "Meta", value: "meta_scaffolding" },
  { label: "System", value: "system_generated" },
  { label: "All", value: "all" },
];

export function normalizeAnalyticsOrigin(raw: string | string[] | undefined): OriginLens {
  const value = Array.isArray(raw) ? raw[0] : raw;
  if (
    value === "human_direct" ||
    value === "human_delegated" ||
    value === "pipeline_eval" ||
    value === "meta_scaffolding" ||
    value === "system_generated" ||
    value === "all"
  ) {
    return value;
  }
  return DEFAULT_ANALYTICS_ORIGIN;
}

export function originQueryValue(origin: OriginLens): SessionOrigin | undefined {
  return origin === "all" ? undefined : origin;
}

export function originLensLabel(origin: OriginLens): string {
  return ORIGIN_LENSES.find((lens) => lens.value === origin)?.label ?? "My work";
}

export function withOriginQuery(
  path: string,
  origin: OriginLens,
  extra?: Record<string, string | undefined | null>
): string {
  const params = new URLSearchParams();
  const queryOrigin = originQueryValue(origin);
  if (queryOrigin) {
    params.set("origin", queryOrigin);
  }
  for (const [key, value] of Object.entries(extra ?? {})) {
    if (value) {
      params.set(key, value);
    }
  }
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}
