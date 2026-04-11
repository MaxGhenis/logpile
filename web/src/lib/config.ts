import path from "path";

export const config = {
  /** Absolute path to the SQLite database */
  dbPath:
    process.env.LOGPILE_DB_PATH ||
    path.resolve(process.cwd(), "..", "logpile.db"),

  /** Absolute path to the shared JSONL directory */
  sharedDir:
    process.env.LOGPILE_SHARED_DIR ||
    path.resolve(process.cwd(), "..", "shared"),

  /** When true, hide unlisted/private content, show only public profiles */
  publicMode: process.env.LOGPILE_PUBLIC_MODE === "true",
};
