"""Reference oracle: the derived-row writers as they were before diffing.

Verbatim copies of insert_session_daily_usage, apply_message_claims,
insert_tool_calls and insert_session_paths from logpile/db.py at commit
2a066a6, each of which deleted and reinserted every row it owned. The
write-minimal writers must leave exactly the rows these leave.
"""


def legacy_insert_session_daily_usage(conn, session_id: str, daily_usage: list):
    daily_usage = list(daily_usage)
    session_row = conn.execute(
        """
        SELECT total_input_tokens, total_output_tokens, fresh_input_tokens,
               cached_input_tokens, cache_creation_input_tokens,
               cache_creation_5m_input_tokens, cache_creation_1h_input_tokens,
               cache_creation_unknown_input_tokens, reasoning_output_tokens,
               user_message_count, assistant_message_count, tool_call_count
        FROM sessions WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    component_fields = (
        "total_input_tokens",
        "total_output_tokens",
        "fresh_input_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "cache_creation_5m_input_tokens",
        "cache_creation_1h_input_tokens",
        "cache_creation_unknown_input_tokens",
        "reasoning_output_tokens",
        "user_message_count",
        "assistant_message_count",
        "tool_call_count",
    )
    if session_row is not None:
        mismatches = {
            field: (
                sum(int(getattr(day, field, 0) or 0) for day in daily_usage),
                int(session_row[field] or 0),
            )
            for field in component_fields
            if sum(int(getattr(day, field, 0) or 0) for day in daily_usage)
            != int(session_row[field] or 0)
        }
        if mismatches:
            raise ValueError(
                f"daily usage does not reconcile for session {session_id}: {mismatches}"
            )
    for day in daily_usage:
        cache_creation = int(day.cache_creation_input_tokens or 0)
        split = (
            int(day.cache_creation_5m_input_tokens or 0)
            + int(day.cache_creation_1h_input_tokens or 0)
            + int(day.cache_creation_unknown_input_tokens or 0)
        )
        if split != cache_creation:
            raise ValueError(
                f"cache-creation daily split does not reconcile for {session_id} "
                f"on {day.day}: {split} != {cache_creation}"
            )
    conn.execute("DELETE FROM session_daily_usage WHERE session_id = ?", (session_id,))
    if not daily_usage:
        return
    conn.executemany(
        """
        INSERT INTO session_daily_usage (
            session_id, day,
            total_input_tokens, total_output_tokens,
            fresh_input_tokens, cached_input_tokens,
            cache_creation_input_tokens, cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens,
            cache_creation_unknown_input_tokens, reasoning_output_tokens,
            user_message_count, assistant_message_count, tool_call_count,
            approximated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                session_id,
                d.day,
                d.total_input_tokens,
                d.total_output_tokens,
                d.fresh_input_tokens,
                d.cached_input_tokens,
                d.cache_creation_input_tokens,
                d.cache_creation_5m_input_tokens,
                d.cache_creation_1h_input_tokens,
                d.cache_creation_unknown_input_tokens,
                d.reasoning_output_tokens,
                d.user_message_count,
                d.assistant_message_count,
                d.tool_call_count,
                1 if d.approximated else 0,
            )
            for d in daily_usage
        ],
    )


def legacy_apply_message_claims(conn, session_id: str, message_usage) -> set[str]:
    """Replace one session's occurrences and return every possibly stale owner.

    Losing occurrences remain in the ledger. The `message_claim_owners` view
    derives the minimum-ranked live claimant from all rows, so a reparse that
    drops a winning key or changes a session rank immediately promotes an
    unchanged loser. Returning every claimant for touched keys makes the
    scoped native refresh correct before and after any such ownership change.
    """
    # Stage the current iterable in SQLite rather than converting it to a
    # list/set. Claude's parser deliberately returns a disk-backed reusable
    # sequence, and materializing it here would restore output-proportional
    # heap usage during sync.
    conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS _logpile_current_message_claims (
            claim_key TEXT PRIMARY KEY,
            day TEXT,
            model TEXT,
            fresh_input_tokens INTEGER NOT NULL,
            cached_input_tokens INTEGER NOT NULL,
            cache_creation_input_tokens INTEGER NOT NULL,
            cache_creation_5m_input_tokens INTEGER NOT NULL,
            cache_creation_1h_input_tokens INTEGER NOT NULL,
            cache_creation_unknown_input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS _logpile_touched_message_claims (
            claim_key TEXT PRIMARY KEY
        ) WITHOUT ROWID
        """
    )
    conn.execute("DELETE FROM _logpile_current_message_claims")
    conn.execute("DELETE FROM _logpile_touched_message_claims")

    def normalized_rows():
        for message in message_usage:
            total = max(0, int(message.cache_creation_input_tokens or 0))
            cache_5m = max(0, int(message.cache_creation_5m_input_tokens or 0))
            cache_1h = max(0, int(message.cache_creation_1h_input_tokens or 0))
            cache_unknown = max(
                0,
                int(
                    getattr(
                        message,
                        "cache_creation_unknown_input_tokens",
                        0,
                    )
                    or 0
                ),
            )
            if cache_5m + cache_1h > total:
                cache_5m = cache_1h = 0
                cache_unknown = total
            elif cache_5m + cache_1h + cache_unknown != total:
                cache_unknown = total - cache_5m - cache_1h
            yield (
                message.claim_key,
                message.day,
                message.model,
                message.fresh_input_tokens,
                message.cached_input_tokens,
                total,
                cache_5m,
                cache_1h,
                cache_unknown,
                message.output_tokens,
            )

    conn.executemany(
        """
        INSERT INTO _logpile_current_message_claims (
            claim_key, day, model, fresh_input_tokens,
            cached_input_tokens, cache_creation_input_tokens,
            cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens,
            cache_creation_unknown_input_tokens, output_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(claim_key) DO UPDATE SET
            day = excluded.day,
            model = excluded.model,
            fresh_input_tokens = excluded.fresh_input_tokens,
            cached_input_tokens = excluded.cached_input_tokens,
            cache_creation_input_tokens = excluded.cache_creation_input_tokens,
            cache_creation_5m_input_tokens =
                excluded.cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens =
                excluded.cache_creation_1h_input_tokens,
            cache_creation_unknown_input_tokens =
                excluded.cache_creation_unknown_input_tokens,
            output_tokens = excluded.output_tokens
        """,
        normalized_rows(),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO _logpile_touched_message_claims (claim_key)
        SELECT claim_key FROM message_claims WHERE session_id = ?
        """,
        (session_id,),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO _logpile_touched_message_claims (claim_key)
        SELECT claim_key FROM _logpile_current_message_claims
        """
    )
    if (
        conn.execute("SELECT 1 FROM _logpile_touched_message_claims LIMIT 1").fetchone()
        is None
    ):
        return set()

    affected: set[str] = {session_id}

    def add_current_claimants() -> None:
        affected.update(
            row[0]
            for row in conn.execute(
                """
                SELECT DISTINCT claims.session_id
                FROM message_claims AS claims
                JOIN _logpile_touched_message_claims AS touched
                  ON touched.claim_key = claims.claim_key
                """
            )
        )

    add_current_claimants()
    conn.execute(
        """
        INSERT INTO message_claims (
            claim_key, session_id, day, model,
            fresh_input_tokens, cached_input_tokens,
            cache_creation_input_tokens, cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens,
            cache_creation_unknown_input_tokens, output_tokens
        )
        SELECT claim_key, ?, day, model,
               fresh_input_tokens, cached_input_tokens,
               cache_creation_input_tokens, cache_creation_5m_input_tokens,
               cache_creation_1h_input_tokens,
               cache_creation_unknown_input_tokens, output_tokens
        FROM _logpile_current_message_claims
        WHERE 1
        ON CONFLICT(claim_key, session_id) DO UPDATE SET
            day = excluded.day,
            model = excluded.model,
            fresh_input_tokens = excluded.fresh_input_tokens,
            cached_input_tokens = excluded.cached_input_tokens,
            cache_creation_input_tokens = excluded.cache_creation_input_tokens,
            cache_creation_5m_input_tokens =
                excluded.cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens =
                excluded.cache_creation_1h_input_tokens,
            cache_creation_unknown_input_tokens =
                excluded.cache_creation_unknown_input_tokens,
            output_tokens = excluded.output_tokens
        """,
        (session_id,),
    )
    conn.execute(
        """
        DELETE FROM message_claims
        WHERE session_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM _logpile_current_message_claims AS current
              WHERE current.claim_key = message_claims.claim_key
          )
        """,
        (session_id,),
    )
    add_current_claimants()
    return affected


def legacy_insert_tool_calls(conn, session_id: str, tool_calls):
    conn.execute("DELETE FROM tool_calls WHERE session_id = ?", (session_id,))
    conn.executemany(
        "INSERT INTO tool_calls (session_id, tool_name, command, timestamp, is_error) VALUES (?,?,?,?,?)",
        (
            (
                session_id,
                tc.tool_name,
                tc.command,
                tc.timestamp,
                1 if tc.is_error else 0,
            )
            for tc in tool_calls
        ),
    )


def legacy_insert_session_paths(conn, session_id: str, session_paths):
    conn.execute("DELETE FROM session_paths WHERE session_id = ?", (session_id,))
    # Aggregate in SQLite so a transcript touching millions of unique paths
    # does not build an equally large Python dictionary (and then a second
    # list for executemany). ``tool_name_missing`` keeps None distinct from
    # the empty string while still giving the WITHOUT ROWID table a fully
    # non-null primary key equivalent to the old tuple key.
    conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS _logpile_current_session_paths (
            normalized_path TEXT NOT NULL,
            operation TEXT NOT NULL,
            source TEXT NOT NULL,
            tool_name_missing INTEGER NOT NULL,
            tool_name_key TEXT NOT NULL,
            raw_path TEXT NOT NULL,
            relative_path TEXT,
            repo_relative_path TEXT,
            display_path TEXT NOT NULL,
            first_timestamp TEXT,
            last_timestamp TEXT,
            occurrence_count INTEGER NOT NULL,
            PRIMARY KEY (
                normalized_path, operation, source,
                tool_name_missing, tool_name_key
            )
        ) WITHOUT ROWID
        """
    )
    conn.execute("DELETE FROM _logpile_current_session_paths")

    def staged_rows():
        for path in session_paths:
            missing_tool_name = 1 if path.tool_name is None else 0
            yield (
                path.normalized_path,
                path.operation,
                path.source,
                missing_tool_name,
                "" if missing_tool_name else path.tool_name,
                path.raw_path,
                path.relative_path,
                getattr(path, "repo_relative_path", None),
                path.display_path,
                path.timestamp,
                path.timestamp,
                1,
            )

    conn.executemany(
        """
        INSERT INTO _logpile_current_session_paths (
            normalized_path, operation, source,
            tool_name_missing, tool_name_key, raw_path,
            relative_path, repo_relative_path, display_path,
            first_timestamp, last_timestamp, occurrence_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (
            normalized_path, operation, source,
            tool_name_missing, tool_name_key
        ) DO UPDATE SET
            first_timestamp = CASE
                WHEN excluded.first_timestamp IS NULL
                    THEN _logpile_current_session_paths.first_timestamp
                WHEN _logpile_current_session_paths.first_timestamp IS NULL
                  OR excluded.first_timestamp
                     < _logpile_current_session_paths.first_timestamp
                    THEN excluded.first_timestamp
                ELSE _logpile_current_session_paths.first_timestamp
            END,
            last_timestamp = CASE
                WHEN excluded.last_timestamp IS NULL
                    THEN _logpile_current_session_paths.last_timestamp
                WHEN _logpile_current_session_paths.last_timestamp IS NULL
                  OR excluded.last_timestamp
                     > _logpile_current_session_paths.last_timestamp
                    THEN excluded.last_timestamp
                ELSE _logpile_current_session_paths.last_timestamp
            END,
            occurrence_count =
                _logpile_current_session_paths.occurrence_count + 1
        """,
        staged_rows(),
    )
    conn.execute(
        """
        INSERT INTO session_paths (
            session_id, raw_path, normalized_path, relative_path,
            repo_relative_path, display_path, operation, source,
            tool_name, first_timestamp, last_timestamp, occurrence_count
        )
        SELECT ?, raw_path, normalized_path, relative_path,
               repo_relative_path, display_path, operation, source,
               CASE WHEN tool_name_missing = 1 THEN NULL ELSE tool_name_key END,
               first_timestamp, last_timestamp, occurrence_count
        FROM _logpile_current_session_paths
        """,
        (session_id,),
    )
