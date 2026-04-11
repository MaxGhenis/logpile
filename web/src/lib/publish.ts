import "server-only";

import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { promisify } from "node:util";

import { config } from "./config";
import type { PublishReview } from "./types";

const execFileAsync = promisify(execFile);

type PublishReviewErrorPayload = {
  error?: string;
  code?: string;
};

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

export async function getPublishReview(sessionId: string): Promise<PublishReview | null> {
  if (config.publicMode) {
    return null;
  }

  const pythonBin = getPythonBin();
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

  try {
    const { stdout } = await execFileAsync(pythonBin, args, {
      cwd: config.repoRoot,
      maxBuffer: 10 * 1024 * 1024,
      env: process.env,
    });
    return JSON.parse(stdout) as PublishReview;
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
    const message = payload?.error || stderr || stdout || "Publish review failed";
    const status = classifyError(payload || {}, message);
    if (status === 404) {
      return null;
    }
    throw new PublishReviewCommandError(message, status);
  }
}
