"use client";

import { useEffect, useState } from "react";
import {
  BarChart as RechartsBarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import type { ChartData } from "@/lib/types";
import { niceTicks, formatAxisValue } from "@/lib/chart-utils";

export function BarChartCard({ url, title }: { url: string; title: string }) {
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

  const points = data.labels.map((label, i) => ({
    name: label,
    value: data.datasets[0]?.data[i] ?? 0,
  }));

  return (
    <div className="bg-lp-surface border border-lp-border-dim rounded-lg p-5 h-full">
      <div className="text-[0.72rem] text-lp-text-faint uppercase tracking-widest font-semibold mb-4">
        {title}
      </div>
      <ResponsiveContainer width="100%" height={180}>
        <RechartsBarChart data={points}>
          <CartesianGrid strokeDasharray="3 3" stroke="#292524" />
          <XAxis
            dataKey="name"
            tick={{ fill: "#78716c", fontSize: 9 }}
            angle={-35}
            textAnchor="end"
            height={50}
            interval={0}
          />
          <YAxis
            tick={{ fill: "#78716c", fontSize: 10 }}
            width={55}
            ticks={niceTicks(Math.max(...points.map((p) => p.value)))}
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
          <Bar
            dataKey="value"
            fill="#f59e0b99"
            stroke="#f59e0b"
            strokeWidth={1}
            radius={[3, 3, 0, 0]}
          />
        </RechartsBarChart>
      </ResponsiveContainer>
    </div>
  );
}
