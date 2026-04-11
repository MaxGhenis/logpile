/**
 * JSONL transcript parsers — TypeScript port of Python parsers.py
 * Only the render_* functions (for display), not the parse_* functions (for sync).
 */
import fs from "fs";

/* ── Types ─────────────────────────────────────────────────────────── */

export interface TurnUser {
  type: "user";
  content: string;
  timestamp: string | null;
  cwd?: string;
}

export interface TurnAssistant {
  type: "assistant";
  blocks: ContentBlock[];
  timestamp: string | null;
  model: string | null;
  input_tokens: number;
  output_tokens: number;
}

export interface ContentBlock {
  type: "text" | "thinking" | "tool_use";
  text?: string;
  name?: string;
  id?: string;
  input?: Record<string, unknown>;
}

export interface TurnToolUse {
  type: "tool_use";
  name: string;
  id: string;
  input: Record<string, unknown>;
  timestamp: string | null;
}

export interface TurnToolResult {
  type: "tool_result";
  tool_use_id: string;
  content: string;
  is_error: boolean;
  timestamp: string | null;
}

export interface TurnThinking {
  type: "thinking";
  text: string;
  timestamp: string | null;
}

export interface TurnError {
  type: "error";
  content: string;
}

export type Turn =
  | TurnUser
  | TurnAssistant
  | TurnToolUse
  | TurnToolResult
  | TurnThinking
  | TurnError;

/* ── Helpers ───────────────────────────────────────────────────────── */

function loadJsonl(path: string): Record<string, unknown>[] {
  try {
    const text = fs.readFileSync(path, "utf-8");
    const records: Record<string, unknown>[] = [];
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        records.push(JSON.parse(trimmed));
      } catch {
        // skip malformed lines
      }
    }
    return records;
  } catch {
    return [];
  }
}

function extractText(content: unknown): string {
  if (typeof content === "string") return content;
  if (content && typeof content === "object" && !Array.isArray(content)) {
    const obj = content as Record<string, unknown>;
    if (typeof obj.text === "string") return obj.text;
    if (typeof obj.thinking === "string") return obj.thinking;
    if (obj.content !== undefined) return extractText(obj.content);
    return "";
  }
  if (Array.isArray(content)) {
    return content
      .map((block) => extractText(block))
      .filter(Boolean)
      .join(" ");
  }
  return "";
}

/* eslint-disable-next-line @typescript-eslint/no-explicit-any */
type Rec = Record<string, any>;

const USER_CONTEXT_RE =
  /#\s*Context from my IDE setup:.*?## My request for Codex:\n/s;
const ENV_CONTEXT_RE = /^\s*<environment_context>.*?<\/environment_context>\s*$/s;
const CWD_CONTEXT_RE = /^\s*<cwd>.*?<\/cwd>\s*$/s;

function cleanCodexUserText(text: string): string {
  const clean = text.replace(USER_CONTEXT_RE, "").trim();
  if (ENV_CONTEXT_RE.test(clean) || CWD_CONTEXT_RE.test(clean)) return "";
  return clean;
}

function normalizeCodexRecord(
  record: Rec
): [string, Rec, string | null] {
  const recordType = (record.type as string) || (record.record_type as string) || "";
  const timestamp = (record.timestamp as string) || null;

  if (recordType === "response_item" && record.payload && typeof record.payload === "object") {
    const payload = record.payload as Rec;
    return [payload.type || "", payload, timestamp];
  }
  if (
    ["session_meta", "event_msg", "turn_context"].includes(recordType) &&
    record.payload &&
    typeof record.payload === "object"
  ) {
    return [recordType, record.payload as Rec, timestamp];
  }
  return [recordType, record, timestamp];
}

function loadToolArgs(args: unknown): Rec {
  if (typeof args === "object" && args !== null && !Array.isArray(args)) return args as Rec;
  if (typeof args !== "string") return {};
  try {
    const parsed = JSON.parse(args);
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
      ? parsed
      : {};
  } catch {
    return {};
  }
}

function extractCommand(args: Rec): string | null {
  const rawCmd = args.command ?? args.cmd;
  if (Array.isArray(rawCmd)) {
    if (rawCmd.length >= 3 && rawCmd[1] === "-lc") return String(rawCmd[rawCmd.length - 1]);
    return rawCmd.map(String).join(" ");
  }
  if (typeof rawCmd === "string") return rawCmd;
  return null;
}

function parseToolOutput(outputRaw: unknown): [string, boolean] {
  let isError = false;
  let parsed: unknown = null;

  if (typeof outputRaw === "string") {
    try {
      parsed = JSON.parse(outputRaw);
    } catch {
      parsed = null;
    }
  } else if (typeof outputRaw === "object" && outputRaw !== null) {
    parsed = outputRaw;
  }

  let display: unknown;
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const obj = parsed as Rec;
    display = obj.output ?? obj.content ?? parsed;
    const meta = obj.metadata;
    if (meta && typeof meta === "object") {
      const exitCode = (meta as Rec).exit_code;
      if (typeof exitCode === "number" && exitCode !== 0) isError = true;
    }
    if (obj.is_error) isError = true;
  } else if (parsed !== null) {
    display = parsed;
  } else {
    display = outputRaw;
  }

  const displayText = String(display);
  const exitMatch = displayText.match(/Process exited with code (\d+)/);
  if (exitMatch && parseInt(exitMatch[1], 10) !== 0) isError = true;

  return [displayText, isError];
}

/* ── Claude Code transcript ────────────────────────────────────────── */

export function renderClaudecodeTranscript(path: string): Turn[] {
  const records = loadJsonl(path);
  const turns: Turn[] = [];
  const seenMsgIds = new Set<string>();

  for (const r of records) {
    const rtype = r.type as string || "";
    const ts = (r.timestamp as string) || null;

    if (rtype === "user") {
      const msg = (r.message || {}) as Rec;
      const content = msg.content;

      // Tool results come as user-role messages
      if (Array.isArray(content)) {
        const toolResults = content.filter(
          (b: unknown) =>
            b && typeof b === "object" && (b as Rec).type === "tool_result"
        );
        if (toolResults.length > 0) {
          for (const block of toolResults) {
            let resultContent = (block as Rec).content ?? "";
            if (Array.isArray(resultContent)) {
              resultContent = resultContent
                .filter((b: unknown) => b && typeof b === "object")
                .map((b: unknown) => ((b as Rec).text as string) || "")
                .join("\n");
            }
            turns.push({
              type: "tool_result",
              tool_use_id: ((block as Rec).tool_use_id as string) || "",
              content: String(resultContent).slice(0, 5000),
              is_error: !!((block as Rec).is_error),
              timestamp: ts,
            });
          }
          continue;
        }
      }

      const text = extractText(content);
      if (!text.trim()) continue;
      turns.push({ type: "user", content: text, timestamp: ts, cwd: r.cwd as string });

    } else if (rtype === "assistant") {
      const msg = (r.message || {}) as Rec;
      const msgId = msg.id as string;
      if (msgId) {
        if (seenMsgIds.has(msgId)) continue;
        seenMsgIds.add(msgId);
      }

      const content = msg.content;
      if (!Array.isArray(content)) continue;

      const usage = (msg.usage || {}) as Rec;
      const blocks: ContentBlock[] = [];

      for (const block of content) {
        if (!block || typeof block !== "object") continue;
        const btype = (block as Rec).type as string;
        if (btype === "text") {
          blocks.push({ type: "text", text: ((block as Rec).text as string) || "" });
        } else if (btype === "thinking") {
          blocks.push({ type: "thinking", text: ((block as Rec).thinking as string) || "" });
        } else if (btype === "tool_use") {
          blocks.push({
            type: "tool_use",
            name: ((block as Rec).name as string) || "",
            id: ((block as Rec).id as string) || "",
            input: ((block as Rec).input as Rec) || {},
          });
        }
      }

      if (blocks.length === 0) continue;

      turns.push({
        type: "assistant",
        blocks,
        timestamp: ts,
        model: (msg.model as string) || null,
        input_tokens:
          ((usage.input_tokens as number) || 0) +
          ((usage.cache_read_input_tokens as number) || 0),
        output_tokens: (usage.output_tokens as number) || 0,
      });
    }
  }

  return turns;
}

/* ── Codex transcript ──────────────────────────────────────────────── */

export function renderCodexTranscript(path: string): Turn[] {
  const records = loadJsonl(path);
  const turns: Turn[] = [];

  for (const record of records) {
    const [recordType, payload, timestamp] = normalizeCodexRecord(record as Rec);

    if (recordType === "message") {
      const role = payload.role as string;
      const content = payload.content;

      if (role === "user") {
        const text = extractText(content);
        const clean = cleanCodexUserText(text);
        if (!clean || clean.length < 3) continue;
        turns.push({ type: "user", content: clean, timestamp });

      } else if (role === "assistant") {
        const blocks: ContentBlock[] = [];
        if (Array.isArray(content)) {
          for (const block of content) {
            if (!block || typeof block !== "object") continue;
            const bt = (block as Rec).type as string;
            if (["text", "output_text", "input_text"].includes(bt) && (block as Rec).text) {
              blocks.push({ type: "text", text: (block as Rec).text as string });
            }
          }
        }
        if (blocks.length === 0) {
          const text = extractText(content);
          if (text.trim()) blocks.push({ type: "text", text });
        }
        if (blocks.length > 0) {
          turns.push({
            type: "assistant",
            blocks,
            timestamp,
            model: null,
            input_tokens: 0,
            output_tokens: 0,
          });
        }
      }

    } else if (recordType === "function_call") {
      const toolName = (payload.name as string) || "unknown";
      const args = loadToolArgs(payload.arguments);
      const cmd = extractCommand(args);
      turns.push({
        type: "tool_use",
        name: toolName,
        id: (payload.call_id as string) || "",
        input: Object.keys(args).length > 0 ? args : cmd ? { command: cmd } : {},
        timestamp,
      });

    } else if (recordType === "function_call_output") {
      const [display, isError] = parseToolOutput(payload.output);
      turns.push({
        type: "tool_result",
        tool_use_id: (payload.call_id as string) || "",
        content: display.slice(0, 5000),
        is_error: isError,
        timestamp,
      });

    } else if (recordType === "reasoning") {
      const summaries = payload.summary;
      if (Array.isArray(summaries)) {
        const text = summaries
          .filter((s: unknown) => s && typeof s === "object")
          .map((s: unknown) => ((s as Rec).text as string) || "")
          .join(" ");
        if (text.trim()) {
          turns.push({ type: "thinking", text, timestamp });
        }
      }
    }
  }

  return turns;
}
