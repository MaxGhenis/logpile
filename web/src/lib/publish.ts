import "server-only";

import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { promisify } from "node:util";

import { config } from "./config";
import type { PublishQueueResponse, PublishReview, SessionStatus } from "./types";

const execFileAsync = promisify(execFile);

type PublishReviewErrorPayload = {
  error?: string;
  code?: string;
};

const PUBLISH_VISIBILITIES = new Set(["pending", "all", "private", "unlisted", "public"]);
const PUBLISH_STATUSES = new Set(["exploration", "success", "partial", "failed"]);

export class PublishReviewCommandError extends Error {
  status: number;

  constructor(message: string, status = 500) {
    super(message);
    this.name = "PublishReviewCommandError";
    this.status = status;
  }
}

function parseErrorPayload(text: string): PublishReviewErrorPayload | null {
  if (!text) {
    return null;
  }
  try {
    const payload = JSON.parse(text) as PublishReviewErrorPayload;
    return payload && typeof payload === "object" ? payload : null;
  } catch {
    return null;
  }
}

function classifyError(payload: PublishReviewErrorPayload, fallback: string): number {
  if (payload.code === "not_found" || fallback.includes("not found")) {
    return 404;
  }
  if (payload.code === "ambiguous" || fallback.toLowerCase().includes("ambiguous")) {
    return 400;
  }
  return 500;
}

function getPythonBin(): string {
  if (process.env.LOGPILE_PYTHON_BIN) {
    return process.env.LOGPILE_PYTHON_BIN;
  }
  const localVenv = `${config.repoRoot}/.venv/bin/python`;
  return existsSync(localVenv) ? localVenv : "python3";
}

async function runPublishJsonCommand(args: string[]) {
  const pythonBin = getPythonBin();
  try {
    const { stdout } = await execFileAsync(pythonBin, args, {
      cwd: config.repoRoot,
      maxBuffer: 10 * 1024 * 1024,
      env: process.env,
    });
    return stdout;
  } catch (error) {
    const execError = error as NodeJS.ErrnoException & {
      stdout?: string;
      stderr?: string;
    };
    if (execError.code === "ENOENT") {
      throw new PublishReviewCommandError(
        `Python interpreter not found: ${pythonBin}`,
        500,
      );
    }

    const stdout = (execError.stdout || "").trim();
    const stderr = (execError.stderr || "").trim();
    const payload = parseErrorPayload(stdout) || parseErrorPayload(stderr);
    const message = payload?.error || stderr || stdout || "Publish command failed";
    const status = classifyError(payload || {}, message);
    if (status === 404) {
      return null;
    }
    throw new PublishReviewCommandError(message, status);
  }
}

export async function getPublishReview(sessionId: string): Promise<PublishReview | null> {
  if (config.publicMode) {
    return null;
  }

  const args = [
    "-m",
    "logpile.cli",
    "publish",
    "review",
    sessionId,
    "--db",
    config.dbPath,
    "--json",
  ];
  const stdout = await runPublishJsonCommand(args);
  return stdout ? (JSON.parse(stdout) as PublishReview) : null;
}

function normalizeQueueVisibility(visibility?: string): string {
  const normalized = (visibility || "pending").trim().toLowerCase();
  if (!PUBLISH_VISIBILITIES.has(normalized)) {
    throw new RangeError(`Invalid publish queue visibility: ${visibility}`);
  }
  return normalized;
}

function normalizeQueueStatus(status?: string): SessionStatus | undefined {
  if (!status) {
    return undefined;
  }
  const normalized = status.trim().toLowerCase();
  if (!PUBLISH_STATUSES.has(normalized)) {
    throw new RangeError(`Invalid publish queue status: ${status}`);
  }
  return normalized as SessionStatus;
}

export async function getPublishQueueResponse(opts?: {
  visibility?: string;
  status?: string;
  user?: string;
  limit?: number;
  reviews?: boolean;
}): Promise<PublishQueueResponse> {
  if (config.publicMode) {
    throw new PublishReviewCommandError("not found", 404);
  }

  const visibility = normalizeQueueVisibility(opts?.visibility);
  const status = normalizeQueueStatus(opts?.status);
  const user = opts?.user?.trim();
  const limit = Math.min(Math.max(opts?.limit ?? 25, 1), 200);
  const reviews = opts?.reviews ?? false;

  const args = [
    "-m",
    "logpile.cli",
    "publish",
    "queue",
    "--db",
    config.dbPath,
    "--visibility",
    visibility,
    "--limit",
    String(limit),
    reviews ? "--reviews" : "--no-reviews",
    "--json",
  ];
  if (status) {
    args.push("--status", status);
  }
  if (user) {
    args.push("--user", user);
  }

  const stdout = await runPublishJsonCommand(args);
  return JSON.parse(stdout || "{}") as PublishQueueResponse;
}
