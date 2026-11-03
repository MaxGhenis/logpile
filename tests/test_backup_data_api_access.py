"""The Supabase schema keeps the archive out of reach of the Data API's roles."""

import os
import re
from pathlib import Path
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from logpile.backup import (
    SUPABASE_BASE_SCHEMA_SQL,
    SUPABASE_SCHEMA_SQL,
    RawFileCandidate,
    SupabaseArchive,
    _sql_statements,
)

POSTGRES_URL = os.environ.get("LOGPILE_TEST_POSTGRES_URL")
TABLES = ("logpile_raw_objects", "logpile_raw_files", "logpile_raw_chunks")
ROLES = ("anon", "authenticated", "service_role")
DENIED_ROLES = ("anon", "authenticated")
ACCESS_BLOCK_START = "-- Keep Supabase's Data API out of the archive."

requires_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="LOGPILE_TEST_POSTGRES_URL is not configured for disposable PostgreSQL tests",
)

simple_statements = st.from_regex(r"[A-Z]{1,8} [a-z_]{1,12};", fullmatch=True)
dollar_quoted_blocks = st.lists(simple_statements, min_size=1, max_size=4).map(
    lambda body: "DO $$\nBEGIN\n" + "\n".join(body) + "\nEND\n$$;"
)


def test_schema_closes_every_table_it_creates():
    created = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SUPABASE_SCHEMA_SQL)
    assert set(created) == set(TABLES)
    closed = re.search(r"ARRAY\[(.*?)\]", SUPABASE_SCHEMA_SQL, re.DOTALL)
    assert closed is not None
    assert set(re.findall(r"'(\w+)'", closed.group(1))) == set(created)


def test_access_block_reaches_the_database_as_one_statement():
    blocks = [s for s in _sql_statements(SUPABASE_SCHEMA_SQL) if "DO $$" in s]
    assert len(blocks) == 1
    assert blocks[0].rstrip().endswith("$$;")
    assert "ENABLE ROW LEVEL SECURITY" in blocks[0]
    assert "REVOKE ALL" in blocks[0]


@given(st.lists(st.one_of(simple_statements, dollar_quoted_blocks), max_size=8))
def test_sql_statements_round_trips_any_mix_of_statements(statements):
    assert list(_sql_statements("\n\n".join(statements))) == statements


def _execute(url, sql):
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql)


@pytest.fixture
def supabase_like_database():
    """A disposable database with Supabase's roles and default grants."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    assert POSTGRES_URL is not None
    name = f"logpile_access_{uuid4().hex[:12]}"
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        existing = {
            row[0]
            for row in admin.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", [list(ROLES)]
            )
        }
        created = [role for role in ROLES if role not in existing]
        for role in created:
            bypass = "BYPASSRLS" if role == "service_role" else ""
            admin.execute(f"CREATE ROLE {role} NOLOGIN NOINHERIT {bypass}")
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_conninfo(**{**conninfo_to_dict(POSTGRES_URL), "dbname": name})
    _execute(
        url,
        """
        GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
          GRANT ALL ON TABLES TO anon, authenticated, service_role;
        """,
    )
    try:
        yield url
    finally:
        with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            for role in created:
                admin.execute(f"DROP ROLE {role}")


def _denied(url, role, statement):
    import psycopg

    with psycopg.connect(url) as conn:
        conn.execute(f"SET LOCAL ROLE {role}")
        try:
            conn.execute(statement)
        except psycopg.errors.InsufficientPrivilege:
            return True
        finally:
            conn.rollback()
    return False


def _statements(table):
    return (
        f"SELECT * FROM {table} LIMIT 1",
        f"INSERT INTO {table} DEFAULT VALUES",
        f"UPDATE {table} SET source = source"
        if table != "logpile_raw_objects"
        else f"UPDATE {table} SET bucket = bucket",
        f"DELETE FROM {table}",
        f"TRUNCATE {table} CASCADE",
    )


def _candidate():
    sha256 = "a" * 64
    return RawFileCandidate(
        path=Path("/tmp/session.jsonl"),
        relative_path="session.jsonl",
        source="codex",
        size_bytes=1,
        mtime_ns=1,
        sha256=sha256,
        file_id="file-1",
        object_key=f"raw/{sha256}",
        content_type="application/x-ndjson",
    )


def _access_snapshot(url):
    import psycopg

    with psycopg.connect(url) as conn:
        return conn.execute(
            """
            SELECT relname, relrowsecurity, relacl::text
            FROM pg_class
            WHERE relname = ANY(%s)
            ORDER BY relname
            """,
            [list(TABLES)],
        ).fetchall()


@requires_postgres
def test_ensure_schema_closes_a_new_archive(supabase_like_database):
    url = supabase_like_database
    SupabaseArchive(url).ensure_schema(create_search_index=False)
    for table in TABLES:
        for role in DENIED_ROLES:
            for statement in _statements(table):
                assert _denied(url, role, statement), f"{role}: {statement}"


@requires_postgres
def test_ensure_schema_closes_an_archive_created_before_this_check(
    supabase_like_database,
):
    import psycopg

    url = supabase_like_database
    open_schema = SUPABASE_BASE_SCHEMA_SQL.split(ACCESS_BLOCK_START)[0]
    assert "ROW LEVEL SECURITY" not in open_schema
    with psycopg.connect(url) as conn:
        for statement in _sql_statements(open_schema):
            conn.execute(statement)
    archive = SupabaseArchive(url)
    with archive.connect() as conn:
        archive.upsert_file(conn, _candidate(), provider="r2", bucket="logpile-raw")
    for table in TABLES:
        assert not _denied(url, "anon", f"SELECT * FROM {table} LIMIT 1")

    archive.ensure_schema(create_search_index=False)

    for table in TABLES:
        for role in DENIED_ROLES:
            for statement in _statements(table):
                assert _denied(url, role, statement), f"{role}: {statement}"
    with psycopg.connect(url) as conn:
        assert conn.execute("SELECT count(*) FROM logpile_raw_files").fetchone()[0] == 1


@requires_postgres
def test_owner_and_service_role_keep_access(supabase_like_database):
    url = supabase_like_database
    archive = SupabaseArchive(url)
    archive.ensure_schema(create_search_index=False)
    with archive.connect() as conn:
        archive.upsert_file(conn, _candidate(), provider="r2", bucket="logpile-raw")
    with archive.connect() as conn:
        rows = conn.execute("SELECT file_id FROM logpile_raw_files").fetchall()
    assert [row["file_id"] for row in rows] == ["file-1"]
    assert not _denied(url, "service_role", "SELECT * FROM logpile_raw_files LIMIT 1")


@requires_postgres
def test_ensure_schema_can_run_again_without_changing_access(supabase_like_database):
    url = supabase_like_database
    archive = SupabaseArchive(url)
    archive.ensure_schema(create_search_index=False)
    before = _access_snapshot(url)
    assert all(row[1] for row in before)
    archive.ensure_schema(create_search_index=False)
    assert _access_snapshot(url) == before


@requires_postgres
def test_ensure_schema_works_without_supabase_roles():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    assert POSTGRES_URL is not None
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        count = admin.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)",
            [list(DENIED_ROLES)],
        ).fetchone()[0]
        if count:
            pytest.skip("This PostgreSQL server already has Supabase's roles")
        name = f"logpile_plain_{uuid4().hex[:12]}"
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_conninfo(**{**conninfo_to_dict(POSTGRES_URL), "dbname": name})
    try:
        SupabaseArchive(url).ensure_schema(create_search_index=False)
        assert all(row[1] for row in _access_snapshot(url))
    finally:
        with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
