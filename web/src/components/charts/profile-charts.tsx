"use client";

import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
} from "recharts";
import { niceTicks, formatAxisValue } from "@/lib/chart-utils";

interface ActivityData {
  labels: string[];
  messages: number[];
  tool_calls: number[];
}

interface SourceData {
  labels: string[];
  sessions: number[];
  colors: string[];
}

export function UserActivityChart({ data }: { data: ActivityData }) {
  const points = data.labels.map((day, i) => ({
    day,
    Messages: data.messages[i] ?? 0,
    "Tool calls": data.tool_calls[i] ?? 0,
  }));

  return (
    <ResponsiveContainer width="100%" height={180}>
      <LineChart data={points}>
        <CartesianGrid strokeDasharray="3 3" stroke="#292524" />
        <XAxis
          dataKey="day"
          tick={{ fill: "#78716c", fontSize: 10 }}
          tickFormatter={(v: string) => v.slice(5)}
          interval="preserveStartEnd"
        />
        <YAxis
          tick={{ fill: "#78716c", fontSize: 10 }}
          width={55}
          ticks={niceTicks(Math.max(...data.messages, ...data.tool_calls))}
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
        <Legend wrapperStyle={{ fontSize: 11, color: "#a8a29e" }} iconSize={10} />
        <Line
          type="monotone"
          dataKey="Messages"
          stroke="#f59e0b"
          strokeWidth={2}
          dot={false}
          fill="#f59e0b18"
        />
        <Line
          type="monotone"
          dataKey="Tool calls"
          stroke="#60a5fa"
          strokeWidth={2}
          dot={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

export function UserSourceChart({ data }: { data: SourceData }) {
  const points = data.labels.map((label, i) => ({
    name: label,
    value: data.sessions[i] ?? 0,
  }));

  return (
    <ResponsiveContainer width="100%" height={180}>
      <PieChart>
        <Pie
          data={points}
          dataKey="value"
          nameKey="name"
          cx="50%"
          cy="50%"
          innerRadius={40}
          outerRadius={70}
          stroke="#0c0a09"
          strokeWidth={3}
        >
          {points.map((_, i) => (
            <Cell key={i} fill={data.colors[i] || "#78716c"} />
          ))}
        </Pie>
        <Tooltip
          contentStyle={{
            background: "#1c1917",
            border: "1px solid #44403c",
            borderRadius: 6,
            fontSize: 12,
          }}
        />
        <Legend wrapperStyle={{ fontSize: 11, color: "#a8a29e" }} iconSize={10} />
      </PieChart>
    </ResponsiveContainer>
  );
}
