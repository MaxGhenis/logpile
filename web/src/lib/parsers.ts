/**
 * JSONL transcript parsers — TypeScript port of Python parsers.py
 * Only the render_* functions (for display), not the parse_* functions (for sync).
 */
import { promises as fs } from "fs";
import type { FileHandle } from "fs/promises";

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

export interface TranscriptPage {
  turns: Turn[];
  nextCursor: number | null;
  startCursor: number;
  bytesRead: number;
  fileSize: number;
  byteLimitReached: boolean;
}

export interface TranscriptPageOptions {
  cursor?: number;
  turnLimit?: number;
  byteLimit?: number;
}

/* ── Helpers ───────────────────────────────────────────────────────── */

export const DEFAULT_TRANSCRIPT_TURN_LIMIT = 100;
export const DEFAULT_TRANSCRIPT_BYTE_LIMIT = 4 * 1024 * 1024;
const TRANSCRIPT_READ_CHUNK_BYTES = 64 * 1024;

type RecordToTurns = (record: Rec) => Turn[];

async function readTranscriptPage(
  source: string | FileHandle,
  recordToTurns: RecordToTurns,
  options: TranscriptPageOptions,
): Promise<TranscriptPage> {
  const turnLimit = Math.max(
    1,
    Math.min(200, Math.floor(options.turnLimit ?? DEFAULT_TRANSCRIPT_TURN_LIMIT)),
  );
  const byteLimit = Math.max(
    TRANSCRIPT_READ_CHUNK_BYTES,
    Math.min(16 * 1024 * 1024, Math.floor(options.byteLimit ?? DEFAULT_TRANSCRIPT_BYTE_LIMIT)),
  );
  let handle: Awaited<ReturnType<typeof fs.open>> | null = null;
  const ownsHandle = typeof source === "string";
  try {
    handle = ownsHandle ? await fs.open(source, "r") : source;
    const stat = await handle.stat();
    const fileSize = Number(stat.size);
    const startCursor = Math.max(
      0,
      Math.min(fileSize, Math.floor(options.cursor ?? 0)),
    );
    const readEnd = Math.min(fileSize, startCursor + byteLimit);
    const turns: Turn[] = [];
    let position = startCursor;
    let pending = Buffer.alloc(0);
    let pendingStart = startCursor;
    let nextCursor = startCursor;

    const consumeLine = (line: Buffer, afterLine: number): boolean => {
      nextCursor = afterLine;
      const trimmed = line.toString("utf8").trim();
      if (!trimmed) return false;
      try {
        const parsed = JSON.parse(trimmed);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return false;
        turns.push(...recordToTurns(parsed as Rec));
      } catch {
        // A malformed record is isolated to this line.
      }
      return turns.length >= turnLimit;
    };

    let pageFull = false;
    while (position < readEnd && !pageFull) {
      const toRead = Math.min(
        TRANSCRIPT_READ_CHUNK_BYTES,
        readEnd - position,
      );
      const chunk = Buffer.allocUnsafe(toRead);
      const { bytesRead } = await handle.read(chunk, 0, toRead, position);
      if (bytesRead === 0) break;
      position += bytesRead;
      pending = Buffer.concat([pending, chunk.subarray(0, bytesRead)]);

      let newlineIndex = pending.indexOf(0x0a);
      while (newlineIndex >= 0) {
        const line = pending.subarray(0, newlineIndex);
        const afterLine = pendingStart + newlineIndex + 1;
        pending = pending.subarray(newlineIndex + 1);
        pendingStart = afterLine;
        if (consumeLine(line, afterLine)) {
          pageFull = true;
          break;
        }
        newlineIndex = pending.indexOf(0x0a);
      }
    }

    if (!pageFull && position === fileSize && pending.length > 0) {
      consumeLine(pending, fileSize);
      pending = Buffer.alloc(0);
    }

    const byteLimitReached = position < fileSize && position >= readEnd;
    if (
      turns.length === 0
      && byteLimitReached
      && nextCursor === startCursor
    ) {
      turns.push({
        type: "error",
        content: `A transcript record exceeds the ${Math.round(byteLimit / 1024 / 1024)} MiB page read cap.`,
      });
      return {
        turns,
        // Advance through an oversized JSONL record a bounded page at a
        // time. A later page will discard the remaining malformed suffix at
        // its newline and can then resume with subsequent complete records.
        nextCursor: position < fileSize ? position : null,
        startCursor,
        bytesRead: position - startCursor,
        fileSize,
        byteLimitReached: true,
      };
    }

    return {
      turns,
      nextCursor: nextCursor < fileSize ? nextCursor : null,
      startCursor,
      bytesRead: position - startCursor,
      fileSize,
      byteLimitReached,
    };
  } catch {
    return {
      turns: [],
      nextCursor: null,
      startCursor: Math.max(0, Math.floor(options.cursor ?? 0)),
      bytesRead: 0,
      fileSize: 0,
      byteLimitReached: false,
    };
  } finally {
    if (ownsHandle) await handle?.close();
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

function claudeRecordToTurns(r: Rec, seenMsgIds: Set<string>): Turn[] {
  const turns: Turn[] = [];
  const rtype = (r.type as string) || "";
  const ts = (r.timestamp as string) || null;

  if (rtype === "user") {
    const msg = (r.message || {}) as Rec;
    const content = msg.content;

    // Tool results come as user-role messages.
    if (Array.isArray(content)) {
      const toolResults = content.filter(
        (block: unknown) =>
          block
          && typeof block === "object"
          && (block as Rec).type === "tool_result",
      );
      if (toolResults.length > 0) {
        for (const block of toolResults) {
          let resultContent = (block as Rec).content ?? "";
          if (Array.isArray(resultContent)) {
            resultContent = resultContent
              .filter((item: unknown) => item && typeof item === "object")
              .map((item: unknown) => ((item as Rec).text as string) || "")
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
        return turns;
      }
    }

    const text = extractText(content);
    if (text.trim()) {
      turns.push({
        type: "user",
        content: text,
        timestamp: ts,
        cwd: r.cwd as string,
      });
    }
  } else if (rtype === "assistant") {
    const msg = (r.message || {}) as Rec;
    const msgId = msg.id as string;
    if (msgId) {
      if (seenMsgIds.has(msgId)) return turns;
      seenMsgIds.add(msgId);
    }

    const content = msg.content;
    if (!Array.isArray(content)) return turns;

    const usage = (msg.usage || {}) as Rec;
    const blocks: ContentBlock[] = [];

    for (const block of content) {
      if (!block || typeof block !== "object") continue;
      const blockType = (block as Rec).type as string;
      if (blockType === "text") {
        blocks.push({ type: "text", text: ((block as Rec).text as string) || "" });
      } else if (blockType === "thinking") {
        blocks.push({
          type: "thinking",
          text: ((block as Rec).thinking as string) || "",
        });
      } else if (blockType === "tool_use") {
        blocks.push({
          type: "tool_use",
          name: ((block as Rec).name as string) || "",
          id: ((block as Rec).id as string) || "",
          input: ((block as Rec).input as Rec) || {},
        });
      }
    }

    if (blocks.length > 0) {
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

export async function renderClaudecodeTranscript(
  path: string | FileHandle,
  options: TranscriptPageOptions = {},
): Promise<TranscriptPage> {
  const seenMsgIds = new Set<string>();
  return readTranscriptPage(
    path,
    (record) => claudeRecordToTurns(record, seenMsgIds),
    options,
  );
}

/* ── Codex transcript ──────────────────────────────────────────────── */

function codexRecordToTurns(record: Rec): Turn[] {
  const turns: Turn[] = [];
  const [recordType, payload, timestamp] = normalizeCodexRecord(record);

  if (recordType === "message") {
    const role = payload.role as string;
    const content = payload.content;

    if (role === "user") {
      const text = extractText(content);
      const clean = cleanCodexUserText(text);
      if (clean && clean.length >= 3) {
        turns.push({ type: "user", content: clean, timestamp });
      }
    } else if (role === "assistant") {
      const blocks: ContentBlock[] = [];
      if (Array.isArray(content)) {
        for (const block of content) {
          if (!block || typeof block !== "object") continue;
          const blockType = (block as Rec).type as string;
          if (
            ["text", "output_text", "input_text"].includes(blockType)
            && (block as Rec).text
          ) {
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
        .filter((summary: unknown) => summary && typeof summary === "object")
        .map((summary: unknown) => ((summary as Rec).text as string) || "")
        .join(" ");
      if (text.trim()) {
        turns.push({ type: "thinking", text, timestamp });
      }
    }
  }
  return turns;
}

export async function renderCodexTranscript(
  path: string | FileHandle,
  options: TranscriptPageOptions = {},
): Promise<TranscriptPage> {
  return readTranscriptPage(path, codexRecordToTurns, options);
}
