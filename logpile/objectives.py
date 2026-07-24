"""Deterministic objective-family helpers for session grouping and filtering."""

import re

SESSION_OBJECTIVE_VERSION = 1


def first_nonempty_line(text: str | None) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        trimmed = line.strip()
        if trimmed:
            return trimmed
    return ""


def objective_seed_text(
    session_goal: str | None,
    first_user_message: str | None,
    session_summary: str | None,
) -> str:
    return (
        first_nonempty_line(session_goal)
        or first_nonempty_line(first_user_message)
        or first_nonempty_line(session_summary)
    )


def normalize_objective_family(text: str) -> str | None:
    first_line = first_nonempty_line(text)
    if not first_line:
        return None
    normalized = re.sub(r"`[^`]+`", " <code> ", first_line.lower())
    normalized = re.sub(r"https?://\S+", " <url> ", normalized)
    normalized = re.sub(r"/[a-z0-9._~/-]+", " <path> ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\b[0-9a-f]{8,}\b", " <id> ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\b\d+\b", " <n> ", normalized)
    normalized = re.sub(r"[^a-z0-9<> ]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or None


def display_objective_label(text: str, max_len: int = 110) -> str:
    return re.sub(r"\s+", " ", first_nonempty_line(text)).strip()[:max_len]


def derive_session_objective(
    session_goal: str | None,
    first_user_message: str | None,
    session_summary: str | None,
) -> dict[str, str | int | None]:
    seed = objective_seed_text(session_goal, first_user_message, session_summary)
    family = normalize_objective_family(seed)
    label = display_objective_label(seed) if family else None
    return {
        "objective_family": family,
        "objective_label": label,
        "objective_version": SESSION_OBJECTIVE_VERSION,
    }
