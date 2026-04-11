"use client";

import { useEffect, useState } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import type { ChartData } from "@/lib/types";
import { niceTicks, formatAxisValue } from "@/lib/chart-utils";

const PALETTE = [
  "#f59e0b", "#60a5fa", "#84cc16", "#ef4444",
  "#a78bfa", "#fb923c", "#22d3ee", "#f472b6",
];

export function ActivityChart({ url, title }: { url: string; title: string }) {
  const [data, setData] = useState<ChartData | null>(null);

  useEffect(() => {
    fetch(url)
      .then((r) => r.json())
      .then(setData)
      .catch(() => {});
  }, [url]);

  if (!data) {
    return (
      <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5 h-full">
        <div className="text-[0.72rem] text-lp-text-faint uppercase tracking-widest font-semibold mb-4">
          {title}
        </div>
        <div className="h-32 flex items-center justify-center text-lp-text-faint text-sm italic">
          Loading...
        </div>
      </div>
    );
  }

  // Transform to recharts format: [{day: "2026-03-10", user1: 100, user2: 50}, ...]
  const points = data.labels.map((label, i) => {
    const point: Record<string, string | number> = { day: label };
    for (const ds of data.datasets) {
      point[ds.label] = ds.data[i] ?? 0;
    }
    return point;
  });

  return (
    <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5 h-full">
      <div className="text-[0.72rem] text-lp-text-faint uppercase tracking-widest font-semibold mb-4">
        {title}
      </div>
      <ResponsiveContainer width="100%" height={180}>
        <LineChart data={points}>
          <CartesianGrid strokeDasharray="3 3" stroke="#292524" />
          <XAxis
            dataKey="day"
            tick={{ fill: "#78716c", fontSize: 10 }}
            tickFormatter={(v: string) => v.slice(5)} // "03-10"
            interval="preserveStartEnd"
          />
          <YAxis
            tick={{ fill: "#78716c", fontSize: 10 }}
            width={55}
            ticks={niceTicks(Math.max(...data.datasets.flatMap((ds) => ds.data)))}
            tickFormatter={formatAxisValue}
          />
          <Tooltip
            contentStyle={{
              background: "#1c1917",
              border: "1px solid #44403c",
              borderRadius: 6,
              fontSize: 12,
            }}
            labelStyle={{ color: "#a8a29e" }}
          />
          <Legend
            wrapperStyle={{ fontSize: 11, color: "#a8a29e" }}
            iconSize={10}
          />
          {data.datasets.map((ds, i) => (
            <Line
              key={ds.label}
              type="monotone"
              dataKey={ds.label}
              stroke={PALETTE[i % PALETTE.length]}
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 3 }}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
