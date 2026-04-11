/**
 * Generate nice tick values using 1-2-5 snapping.
 * Produces round numbers like 0, 50, 100, 150... or 0, 200K, 400K...
 */
export function niceTicks(
  dataMax: number,
  targetTicks = 5
): number[] {
  if (dataMax <= 0) return [0];

  // Find the order of magnitude
  const rawStep = dataMax / targetTicks;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rawStep)));

  // Snap to 1, 2, or 5 multiples
  const residual = rawStep / magnitude;
  let niceStep: number;
  if (residual <= 1.5) niceStep = magnitude;
  else if (residual <= 3.5) niceStep = 2 * magnitude;
  else if (residual <= 7.5) niceStep = 5 * magnitude;
  else niceStep = 10 * magnitude;

  const ticks: number[] = [];
  for (let v = 0; v <= dataMax * 1.05; v += niceStep) {
    ticks.push(Math.round(v));
  }

  // Ensure we have at least the max
  if (ticks[ticks.length - 1] < dataMax) {
    ticks.push(ticks[ticks.length - 1] + Math.round(niceStep));
  }

  return ticks;
}

/** Format a number for axis labels: 1500000 -> "1.5M", 50000 -> "50K" */
export function formatAxisValue(value: number): string {
  if (value === 0) return "0";
  if (Math.abs(value) >= 1_000_000)
    return `${(value / 1_000_000).toFixed(value % 1_000_000 === 0 ? 0 : 1)}M`;
  if (Math.abs(value) >= 1_000)
    return `${(value / 1_000).toFixed(value % 1_000 === 0 ? 0 : 1)}K`;
  return value.toLocaleString();
}
