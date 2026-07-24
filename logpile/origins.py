"""Deterministic session-origin classification for workflow analytics."""

from __future__ import annotations

import re

SESSION_ORIGINS = (
    "human_direct",
    "human_delegated",
    "system_generated",
    "pipeline_eval",
    "meta_scaffolding",
)
SESSION_ORIGIN_VERSION = 1

_META_PREFIXES = (
    "warmup",
    "[suggestion mode:",
    "this session is being continued from a previous conversation",
    "your task is to create a detailed summary of the conversation so far",
    "<local-command-caveat>",
    "<system-reminder>",
)
_PIPELINE_MARKERS = (
    "you are a senior statutory-fidelity reviewer for rac",
    "review this rac file for:",
    "you are participating in an encoding eval",
    "# rac encoder",
    "/tmp/autorac",
    "_eval_workspaces",
    "autorac-",
)
_DELEGATED_MARKERS = (
    "<teammate-message",
    '"type":"task_assignment"',
    '"type": "task_assignment"',
    '"type":"shutdown_request"',
    '"type": "shutdown_request"',
    "status check",
)


def _normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def derive_session_origin(
    *,
    source: str,
    session_id: str,
    first_user_message: str | None,
    project: str | None = None,
    workspace_root: str | None = None,
    source_path: str | None = None,
) -> dict[str, str | int]:
    del source  # reserved for future source-specific rules
    raw_message = first_user_message or ""
    message = _normalize_text(raw_message)
    context = _normalize_text(
        " ".join(
            part
            for part in (raw_message, project, workspace_root, source_path, session_id)
            if part
        )
    )

    if any(message.startswith(prefix) for prefix in _META_PREFIXES):
        origin = "meta_scaffolding"
    elif any(marker in context for marker in _PIPELINE_MARKERS):
        origin = "pipeline_eval"
    elif session_id.startswith("agent-") or any(
        marker in message for marker in _DELEGATED_MARKERS
    ):
        origin = "human_delegated"
    elif not message:
        origin = (
            "human_delegated" if session_id.startswith("agent-") else "system_generated"
        )
    elif message.startswith("# agents.md instructions for "):
        origin = "system_generated"
    else:
        origin = "human_direct"

    return {
        "session_origin": origin,
        "origin_version": SESSION_ORIGIN_VERSION,
    }
