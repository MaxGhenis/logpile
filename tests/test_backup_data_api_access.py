"""The Data API's anon and authenticated roles can't reach the archive; the
service_role key still can.

The database tests need LOGPILE_TEST_POSTGRES_URL to name a PostgreSQL server
and a superuser on it. Each test creates a database and roles, including
Supabase's cluster-wide anon, authenticated and service_role when the server
lacks them, and drops them afterwards, so don't run two copies of this file
against one server at once. LOGPILE_REQUIRE_TEST_POSTGRES=1, which CI sets,
turns every skip into a failure.
"""

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

import logpile.backup as backup_module
from logpile.backup import (
    SCHEMA_LOCK_KEY,
    SUPABASE_ACCESS_SQL,
    SUPABASE_INDEX_SQL,
    SUPABASE_INDEXES,
    SUPABASE_SCHEMA_SQL,
    SUPABASE_SEARCH_INDEX,
    SUPABASE_SEARCH_INDEX_SQL,
    SUPABASE_TABLES_SQL,
    RawFileCandidate,
    RawTextChunk,
    SupabaseArchive,
    _sql_statements,
)

POSTGRES_URL = os.environ.get("LOGPILE_TEST_POSTGRES_URL")
REQUIRE_POSTGRES = os.environ.get("LOGPILE_REQUIRE_TEST_POSTGRES") == "1"
TABLES = ("logpile_raw_objects", "logpile_raw_files", "logpile_raw_chunks")
TABLE_KEYS = {
    "logpile_raw_objects": "sha256",
    "logpile_raw_files": "file_id",
    "logpile_raw_chunks": "file_id, event_index, fragment_index, chunk_index",
}
SUPABASE_ROLES = ("anon", "authenticated", "service_role")
DENIED_ROLES = ("anon", "authenticated")
TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
RELATION_KINDS = ["r", "p", "v", "m", "f"]
INDEX_NAMES = [index.name for index in SUPABASE_INDEXES]
ALL_INDEX_NAMES = {*INDEX_NAMES, SUPABASE_SEARCH_INDEX.name}
# The longest any single wait in these tests may take before the test fails.
WAIT = 15.0
SQL_WHITESPACE = " \t\n\r\f\v"


def _unavailable(reason: str):
    if REQUIRE_POSTGRES:
        pytest.fail(
            f"{reason}, and LOGPILE_REQUIRE_TEST_POSTGRES=1 makes that a failure",
            pytrace=False,
        )
    pytest.skip(reason)


def _admin_url() -> str:
    if not POSTGRES_URL:
        _unavailable("LOGPILE_TEST_POSTGRES_URL is not set")
    return POSTGRES_URL


def _url(base: str, *, dbname=None, role=None, **settings) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(base)
    if dbname:
        params["dbname"] = dbname
    options = [f"-c role={role}"] if role else []
    options += [f"-c {name}={value}" for name, value in settings.items()]
    if options:
        params["options"] = " ".join(options)
    return make_conninfo(**params)


def _connect(url: str, **kwargs):
    import psycopg

    return psycopg.connect(url, **kwargs)


# --- The splitter -------------------------------------------------------------

_FRAGMENTS = [
    "a",
    "z",
    "_",
    "7",
    " ",
    "\n",
    ";",
    "'",
    '"',
    "\\",
    "$",
    "$$",
    "$a$",
    "$fn$",
    "--",
    "/*",
    "*/",
    "é",
    "E",
]
_text = st.lists(st.sampled_from(_FRAGMENTS), max_size=10).map("".join)
_comment_text = _text.filter(lambda text: "/*" not in text and "*/" not in text)
_tags = st.sampled_from(["", "a", "fn", "é", "_x1"])


def _standard(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


@st.composite
def _escaped(draw, text: str) -> str:
    """Quote text for an E'' string, escaping each quote as '' or as \\'."""
    first, *rest = text.replace("\\", "\\\\").split("'")
    escapes = st.sampled_from(["''", "\\'"])
    return "'" + first + "".join(draw(escapes) + part for part in rest) + "'"


_words = st.from_regex(r"c_[a-z0-9_]{0,6}", fullmatch=True)
_dollar_words = st.from_regex(r"c_[a-z0-9_]{0,3}\$[a-z0-9_$]{0,4}", fullmatch=True)
_standard_strings = _text.map(_standard)
_escape_strings = _text.flatmap(_escaped).map(lambda quoted: "E" + quoted)
# A type name that ends in e, then a standard string: only a lone E starts an
# escape string, so a backslash here stays literal.
_name_strings = _text.map(lambda text: "name" + _standard(text))
_quoted_identifiers = _text.filter(bool).map(
    lambda text: '"' + text.replace('"', '""') + '"'
)
_line_comments = st.builds(
    lambda text, end: "--" + text.replace("\n", " ").replace("\r", " ") + end,
    _text,
    st.sampled_from(["\n", "\r", "\r\n"]),
)
_block_comments = st.recursive(
    _comment_text.map(lambda text: f"/* {text} */"),
    lambda inner: st.lists(st.one_of(inner, _comment_text), min_size=1, max_size=3).map(
        lambda parts: "/* " + " ".join(parts) + " */"
    ),
    max_leaves=4,
)
_comments = st.one_of(_line_comments, _block_comments)
# Whitespace and -- comments that hold a line break: what lets one string
# literal continue another.
_line_breaks = st.builds(
    lambda before, after: f"{before}\n{after}",
    st.sampled_from(["", " ", "\t", "\f"])
    | _line_comments.map(lambda comment: " " + comment.rstrip("\r\n")),
    st.lists(
        st.sampled_from([" ", "\t", "\n", "\r\n"]) | _line_comments, max_size=3
    ).map("".join),
)


@st.composite
def _continued_strings(draw):
    """A string literal that goes on across line breaks, in its first part's mode."""
    escape = draw(st.booleans())

    def quote(text):
        return draw(_escaped(text)) if escape else _standard(text)

    parts = [("E" if escape else "") + quote(draw(_text))]
    for text in draw(st.lists(_text, min_size=1, max_size=2)):
        parts.append(draw(_line_breaks) + quote(text))
    return "".join(parts)


@st.composite
def _dollar_quoted(draw, body=_text):
    text = draw(body)
    # PostgreSQL ends a dollar quote at the first copy of its delimiter. No
    # generated text holds a b, so $body$ always works when the drawn tag doesn't.
    for tag in (draw(_tags), "body"):
        delimiter = f"${tag}$"
        if (text + delimiter).find(delimiter) == len(text):
            return f"{delimiter}{text}{delimiter}"
    raise AssertionError(f"no delimiter fits {text!r}")


_code = st.one_of(
    _words,
    _dollar_words,
    _standard_strings,
    _escape_strings,
    _name_strings,
    _quoted_identifiers,
    _continued_strings(),
    _dollar_quoted(),
    st.integers(0, 999).map(str),
    st.just("DO $$ BEGIN PERFORM 1; END $$"),
    st.just("DO $body$\nBEGIN\n    PERFORM 1;\nEND\n$body$"),
)


@st.composite
def _statements(draw):
    pieces = draw(st.lists(st.one_of(_code, _comments), max_size=5))
    pieces.insert(draw(st.integers(0, len(pieces))), draw(_code))
    gap = draw(st.sampled_from([" ", "\n"]))
    # A quote after a line break would continue a string before it, so a word
    # goes ahead of each piece that opens with a quote.
    return gap.join(f"w{gap}{p}" if p.startswith("'") else p for p in pieces) + ";"


@st.composite
def _scripts(draw):
    statements = draw(st.lists(_statements(), max_size=6))
    separators = st.sampled_from(["\n", " ", "\n\n", "\t", "\r\n"])
    script = "".join(statement + draw(separators) for statement in statements)
    expected = list(statements)
    ending = draw(st.sampled_from(["plain", "trailing comment", "no semicolon"]))
    if ending == "trailing comment":
        script += draw(_comments)
    elif ending == "no semicolon" and statements:
        script = script.rstrip(SQL_WHITESPACE)[:-1]
        expected[-1] = expected[-1][:-1].strip(SQL_WHITESPACE)
    return script, expected


@given(_scripts())
@settings(max_examples=400)
def test_sql_statements_splits_only_at_top_level_semicolons(script_and_expected):
    script, expected = script_and_expected
    assert list(_sql_statements(script)) == expected


@given(st.lists(st.sampled_from([*_FRAGMENTS, "\r", "x;y", "E'", "-"]), max_size=40))
@settings(max_examples=400)
def test_sql_statements_output_splits_back_into_itself(fragments):
    statements = list(_sql_statements("".join(fragments)))
    for statement in statements:
        assert statement
        assert statement == statement.strip(SQL_WHITESPACE)
        assert list(_sql_statements(statement)) == [statement]
    assert list(_sql_statements("\n".join(statements))) == statements


FUNCTION_SQL = (
    "CREATE FUNCTION f() RETURNS int AS $fn$\nBEGIN\n    RETURN 1;\nEND\n"
    "$fn$ LANGUAGE plpgsql;"
)


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        (f"{FUNCTION_SQL}\nSELECT 2;", [FUNCTION_SQL, "SELECT 2;"]),
        (
            "-- a $$ comment\nDO $$ BEGIN PERFORM 1; END $$;",
            ["-- a $$ comment\nDO $$ BEGIN PERFORM 1; END $$;"],
        ),
        ("SELECT '$$;';\nSELECT 2;", ["SELECT '$$;';", "SELECT 2;"]),
        ("SELECT 1; -- note\nSELECT 2;", ["SELECT 1;", "-- note\nSELECT 2;"]),
        (
            "DO $$\nBEGIN\n\n  PERFORM 1;\nEND\n$$;",
            ["DO $$\nBEGIN\n\n  PERFORM 1;\nEND\n$$;"],
        ),
        ("SELECT 1 AS a$b$c; SELECT 2;", ["SELECT 1 AS a$b$c;", "SELECT 2;"]),
        ("SELECT E'it\\'s; ok'; SELECT 2;", ["SELECT E'it\\'s; ok';", "SELECT 2;"]),
        ("SELECT name'a\\'';x'; SELECT 2;", ["SELECT name'a\\'';x';", "SELECT 2;"]),
        ("SELECT e'it\\'s; ok'; SELECT 2;", ["SELECT e'it\\'s; ok';", "SELECT 2;"]),
        ("SELECT E'a''\\';x'; SELECT 2;", ["SELECT E'a''\\';x';", "SELECT 2;"]),
        ("SELECT \"x\"\n';'; SELECT 2;", ["SELECT \"x\"\n';';", "SELECT 2;"]),
        ("SELECT E'a'\n'b\\';x'; SELECT 2;", ["SELECT E'a'\n'b\\';x';", "SELECT 2;"]),
        (
            "SELECT 'a' -- c;\n'b\\'; SELECT 2;",
            ["SELECT 'a' -- c;\n'b\\';", "SELECT 2;"],
        ),
        ("SELECT E'a' 'b\\'; SELECT 2;", ["SELECT E'a' 'b\\';", "SELECT 2;"]),
        (
            "SELECT E'a' /* c */\n'b\\'; SELECT 2;",
            ["SELECT E'a' /* c */\n'b\\';", "SELECT 2;"],
        ),
        (
            "SELECT /* a /* b; */ c; */ 1; SELECT 2;",
            ["SELECT /* a /* b; */ c; */ 1;", "SELECT 2;"],
        ),
        ('SELECT 1 AS "x;y"""; SELECT 2;', ['SELECT 1 AS "x;y""";', "SELECT 2;"]),
        ("SELECT 1;\n-- done\n/* really */\n", ["SELECT 1;"]),
        (";;\n;", []),
    ],
)
def test_sql_statements_handles_known_cases(script, expected):
    assert list(_sql_statements(script)) == expected


def test_schema_splits_into_its_statements_in_order():
    tables = list(_sql_statements(SUPABASE_TABLES_SQL))
    access = list(_sql_statements(SUPABASE_ACCESS_SQL))
    indexes = [index.sql for index in (*SUPABASE_INDEXES, SUPABASE_SEARCH_INDEX)]
    assert [len(tables), len(access)] == [3, 1]
    assert access[0].endswith("$$;")
    assert list(_sql_statements(SUPABASE_SCHEMA_SQL)) == tables + access + indexes
    assert list(_sql_statements(SUPABASE_INDEX_SQL)) == indexes[:-1]
    assert list(_sql_statements(SUPABASE_SEARCH_INDEX_SQL)) == indexes[-1:]


@st.composite
def _valid_statements(draw):
    gaps = st.one_of(st.just(" "), _comments.map(lambda comment: f" {comment} "))
    strings = st.one_of(_standard_strings, _escape_strings, _continued_strings())
    if draw(st.booleans()):
        literal = draw(strings)
        comment = draw(st.one_of(st.just(""), _comments))
        body = st.just(f" BEGIN PERFORM {literal}; {comment}\nEND ")
        return f"DO {draw(_dollar_quoted(body=body))};"
    expression = draw(
        st.one_of(
            strings,
            _name_strings,
            _dollar_quoted(),
            st.integers(0, 999).map(str),
        )
    )
    alias = draw(st.one_of(_words, _dollar_words, _quoted_identifiers))
    return f"SELECT{draw(gaps)}{expression}{draw(gaps)}AS {alias}{draw(gaps)};"


def _results(cursor):
    results = []
    while True:
        names = None
        rows = None
        if cursor.description is not None:
            names = [column.name for column in cursor.description]
            rows = cursor.fetchall()
        results.append((cursor.statusmessage, names, rows))
        if not cursor.nextset():
            return results


def test_sql_statements_splits_where_postgresql_does():
    """PostgreSQL splits a multi-statement query with its own lexer: compare."""
    with _connect(_admin_url(), autocommit=True) as conn:

        @given(st.lists(_valid_statements(), min_size=1, max_size=5))
        @settings(max_examples=150, deadline=None)
        def check(statements):
            script = "\n".join(statements)
            whole = _results(conn.execute(script))
            split = [_results(conn.execute(s))[0] for s in _sql_statements(script)]
            assert split == whole

        check()


# --- Databases ------------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveDatabase:
    admin_url: str
    owner: str

    def owner_url(self, **settings) -> str:
        settings.setdefault("statement_timeout", "60s")
        return _url(self.admin_url, role=self.owner, **settings)

    def archive(self, **settings) -> SupabaseArchive:
        return SupabaseArchive(self.owner_url(**settings))


@contextmanager
def _disposable_database(*, bypassrls: bool, supabase_roles: bool):
    admin_url = _admin_url()
    suffix = uuid4().hex[:12]
    name = f"logpile_access_{suffix}"
    owner = f"logpile_owner_{suffix}"
    created_roles: list[str] = []
    created_database = False
    with _connect(admin_url, autocommit=True) as admin:
        try:
            existing = {
                row[0]
                for row in admin.execute(
                    "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                    [list(SUPABASE_ROLES)],
                )
            }
            if not supabase_roles and existing & set(DENIED_ROLES):
                _unavailable("This PostgreSQL server already has Supabase's roles")
            for role in SUPABASE_ROLES if supabase_roles else ():
                if role not in existing:
                    bypass = " BYPASSRLS" if role == "service_role" else ""
                    admin.execute(f"CREATE ROLE {role} NOLOGIN NOINHERIT{bypass}")
                    created_roles.append(role)
            # Supabase's postgres role is NOSUPERUSER BYPASSRLS; on plain
            # PostgreSQL an application's owner role usually lacks BYPASSRLS.
            bypass = "BYPASSRLS" if bypassrls else "NOBYPASSRLS"
            admin.execute(f"CREATE ROLE {owner} NOLOGIN NOSUPERUSER {bypass}")
            created_roles.append(owner)
            admin.execute(f'CREATE DATABASE "{name}"')
            created_database = True
            database_url = _url(admin_url, dbname=name, statement_timeout="60s")
            with _connect(database_url, autocommit=True) as conn:
                conn.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {owner}")
                if supabase_roles:
                    conn.execute(
                        "GRANT USAGE ON SCHEMA public "
                        "TO anon, authenticated, service_role"
                    )
                    conn.execute(
                        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
                        "GRANT ALL ON TABLES TO anon, authenticated, service_role"
                    )
            yield ArchiveDatabase(database_url, owner)
        finally:
            if created_database:
                admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            for role in reversed(created_roles):
                admin.execute(f"DROP ROLE IF EXISTS {role}")


@pytest.fixture(params=["owner-bypassrls", "owner-nobypassrls"])
def archive_db(request):
    """Supabase's roles and default grants, and a NOSUPERUSER table owner."""
    with _disposable_database(
        bypassrls=request.param == "owner-bypassrls", supabase_roles=True
    ) as db:
        yield db


@pytest.fixture
def supabase_db():
    """The same, with an owner that has BYPASSRLS, as Supabase's postgres does."""
    with _disposable_database(bypassrls=True, supabase_roles=True) as db:
        yield db


@pytest.fixture(params=["owner-bypassrls", "owner-nobypassrls"])
def plain_db(request):
    """A server without Supabase's roles, and a NOSUPERUSER table owner."""
    with _disposable_database(
        bypassrls=request.param == "owner-bypassrls", supabase_roles=False
    ) as db:
        yield db


def _create_archive_before_this_check(db, *, search_index=True):
    """Create the tables and indexes the way logpile did before it closed them."""
    sql = SUPABASE_TABLES_SQL + SUPABASE_INDEX_SQL
    if search_index:
        sql += SUPABASE_SEARCH_INDEX_SQL
    with _connect(db.owner_url()) as conn:
        for statement in _sql_statements(sql):
            conn.execute(statement)


def _drop_tables(db):
    with _connect(db.admin_url, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {', '.join(reversed(TABLES))}")


def _candidate(n: int) -> RawFileCandidate:
    sha256 = f"{n:064x}"
    return RawFileCandidate(
        path=Path(f"/tmp/session-{n}.jsonl"),
        relative_path=f"session-{n}.jsonl",
        source="codex",
        size_bytes=100 + n,
        mtime_ns=n,
        sha256=sha256,
        file_id=f"file-{n}",
        object_key=f"raw/sha256/{sha256}.jsonl",
        content_type="application/x-ndjson",
    )


def _chunks(candidate: RawFileCandidate, count: int = 3) -> list[RawTextChunk]:
    return [
        RawTextChunk(
            file_id=candidate.file_id,
            event_index=k,
            fragment_index=0,
            chunk_index=0,
            session_id="session",
            source=candidate.source,
            record_type="message",
            role="user",
            tool_name=None,
            timestamp_text=None,
            byte_start=0,
            byte_end=10,
            content=f"exact thing {k} from {candidate.file_id}",
            content_sha256="0" * 64,
        )
        for k in range(count)
    ]


def _push(archive, conn, candidate, chunk_count=3):
    """What `logpile backup push` does for one file, in one transaction."""
    archive.upsert_file(conn, candidate, provider="r2", bucket="logpile-raw")
    archive.replace_chunks(conn, candidate, _chunks(candidate, chunk_count))


def _rows(conn, table):
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY {TABLE_KEYS[table]}")
    return [tuple(row.values() if isinstance(row, dict) else row) for row in rows]


# --- Catalog checks ---------------------------------------------------------------

_ARCHIVE_SCHEMA = (
    "(SELECT relnamespace FROM pg_class WHERE oid = 'logpile_raw_objects'::regclass)"
)


def _relations(conn) -> dict[str, str]:
    """Every relation in the archive's schema except indexes, with its kind."""
    rows = conn.execute(
        f"""
        SELECT relname, relkind::text FROM pg_class
        WHERE relnamespace = {_ARCHIVE_SCHEMA} AND relkind NOT IN ('i', 'I')
        """
    ).fetchall()
    return dict(rows)


def _row_security(conn) -> dict[str, bool]:
    rows = conn.execute(
        "SELECT relname, relrowsecurity FROM pg_class WHERE relname = ANY(%s)",
        [list(TABLES)],
    ).fetchall()
    return dict(rows)


def _privileges(conn, roles) -> list[tuple[str, str, str]]:
    """Every privilege a role holds on a relation in the archive's schema.

    has_table_privilege and has_any_column_privilege count grants to PUBLIC and
    to roles the role inherits from, as PostgreSQL does when it checks access.
    """
    table_privileges = list(TABLE_PRIVILEGES)
    if conn.info.server_version >= 170000:
        table_privileges.append("MAINTAIN")
    return conn.execute(
        f"""
        WITH archive AS (
            SELECT oid, relname FROM pg_class
            WHERE relnamespace = {_ARCHIVE_SCHEMA}
              AND relkind::text = ANY(%(kinds)s::text[])
        )
        SELECT a.relname, r.role, p.privilege
        FROM archive a, unnest(%(roles)s::text[]) AS r(role),
             unnest(%(table_privileges)s::text[]) AS p(privilege)
        WHERE has_table_privilege(r.role, a.oid, p.privilege)
        UNION ALL
        SELECT a.relname, r.role, 'column ' || p.privilege
        FROM archive a, unnest(%(roles)s::text[]) AS r(role),
             unnest(%(column_privileges)s::text[]) AS p(privilege)
        WHERE has_any_column_privilege(r.role, a.oid, p.privilege)
        ORDER BY 1, 2, 3
        """,
        {
            "kinds": RELATION_KINDS,
            "roles": list(roles),
            "table_privileges": table_privileges,
            "column_privileges": list(COLUMN_PRIVILEGES),
        },
    ).fetchall()


def _acl_entries(conn) -> list[tuple[str, str, str, str]]:
    """Each (table, column or '', grantee or PUBLIC, privilege) the ACLs list."""
    return conn.execute(
        """
        SELECT c.relname, '', coalesce(r.rolname, 'PUBLIC'), acl.privilege_type
        FROM pg_class c
        CROSS JOIN aclexplode(c.relacl) AS acl
        LEFT JOIN pg_roles r ON r.oid = acl.grantee
        WHERE c.relname = ANY(%(tables)s)
        UNION ALL
        SELECT c.relname, a.attname, coalesce(r.rolname, 'PUBLIC'), acl.privilege_type
        FROM pg_class c
        JOIN pg_attribute a ON a.attrelid = c.oid
        CROSS JOIN aclexplode(a.attacl) AS acl
        LEFT JOIN pg_roles r ON r.oid = acl.grantee
        WHERE c.relname = ANY(%(tables)s)
        ORDER BY 1, 2, 3, 4
        """,
        {"tables": list(TABLES)},
    ).fetchall()


def _catalog_versions(conn) -> dict[str, int]:
    """The xmin of the pg_class row of each relation in the archive's schema."""
    rows = conn.execute(
        f"""
        SELECT relname, xmin::text::bigint FROM pg_class
        WHERE relnamespace = {_ARCHIVE_SCHEMA}
        """
    ).fetchall()
    return dict(rows)


def _indexes(conn) -> set[str]:
    rows = conn.execute(
        """
        SELECT relname FROM pg_class
        WHERE relkind = 'i' AND starts_with(relname, 'idx_logpile_')
        """
    ).fetchall()
    return {row[0] for row in rows}


def _probes(table):
    column = "bucket" if table == "logpile_raw_objects" else "source"
    return (
        f"SELECT * FROM {table} LIMIT 1",
        f"INSERT INTO {table} DEFAULT VALUES",
        f"UPDATE {table} SET {column} = {column}",
        f"DELETE FROM {table}",
        f"TRUNCATE {table} CASCADE",
    )


def _errors_as(url, role, statements) -> dict[str, str | None]:
    """Run each statement as role, roll it back, and return its error, if any."""
    import psycopg

    errors: dict[str, str | None] = {}
    with _connect(url, autocommit=True) as conn, conn.transaction():
        conn.execute(f"SET LOCAL ROLE {role}")
        for statement in statements:
            try:
                with conn.transaction():
                    conn.execute(statement)
                    raise psycopg.Rollback()
            except psycopg.Error as exc:
                errors[statement] = exc.diag.message_primary
            else:
                errors[statement] = None
        raise psycopg.Rollback()
    return errors


def _assert_closed(db):
    """No relation in the archive's schema admits anon or authenticated."""
    with _connect(db.admin_url) as conn:
        assert _relations(conn) == dict.fromkeys(TABLES, "r")
        assert _row_security(conn) == dict.fromkeys(TABLES, True)
        assert _privileges(conn, DENIED_ROLES) == []
        opened = [e for e in _acl_entries(conn) if e[2] in ("PUBLIC", *DENIED_ROLES)]
        assert opened == []
    statements = [statement for table in TABLES for statement in _probes(table)]
    for role in DENIED_ROLES:
        for statement, error in _errors_as(db.admin_url, role, statements).items():
            assert error and error.startswith("permission denied"), (role, statement)


# --- Access -----------------------------------------------------------------------


def test_ensure_schema_closes_a_new_archive(archive_db):
    archive_db.archive().ensure_schema()
    _assert_closed(archive_db)


def test_ensure_schema_closes_an_archive_created_before_this_check(archive_db):
    db = archive_db
    _create_archive_before_this_check(db)
    archive = db.archive()
    with archive.connect() as conn:
        _push(archive, conn, _candidate(1))
    with _connect(db.admin_url) as conn:
        before = {table: _rows(conn, table) for table in TABLES}
    reads = [f"SELECT * FROM {table}" for table in TABLES]
    assert set(_errors_as(db.admin_url, "anon", reads).values()) == {None}

    archive.ensure_schema()

    _assert_closed(db)
    with _connect(db.admin_url) as conn:
        assert {table: _rows(conn, table) for table in TABLES} == before


@pytest.mark.parametrize("mode", ["one statement at a time", "one script"])
def test_printed_schema_closes_the_archive(archive_db, mode):
    """`logpile backup schema` prints this for psql or Supabase's SQL editor."""
    with _connect(archive_db.owner_url(), autocommit=True) as conn:
        if mode == "one script":
            conn.execute(SUPABASE_SCHEMA_SQL)
        else:
            for statement in _sql_statements(SUPABASE_SCHEMA_SQL):
                conn.execute(statement)
    _assert_closed(archive_db)


def test_owner_and_service_role_keep_access(archive_db):
    db = archive_db
    archive = db.archive()
    archive.ensure_schema()
    with archive.connect() as conn:
        _push(archive, conn, _candidate(1))
    with _connect(db.owner_url()) as conn:
        owner_rows = {table: _rows(conn, table) for table in TABLES}
    assert [len(owner_rows[table]) for table in TABLES] == [1, 1, 3]
    with _connect(db.admin_url) as conn:
        conn.execute("SET ROLE service_role")
        assert {table: _rows(conn, table) for table in TABLES} == owner_rows
    found = [row["relative_path"] for row in archive.search("exact thing")]
    assert found == ["session-1.jsonl"] * 3


def test_row_level_security_hides_rows_from_a_later_grant(archive_db):
    db = archive_db
    archive = db.archive()
    archive.ensure_schema()
    with archive.connect() as conn:
        _push(archive, conn, _candidate(1))
    with _connect(db.owner_url(), autocommit=True) as conn:
        conn.execute(f"GRANT ALL ON {', '.join(TABLES)} TO anon, authenticated")
    for role in DENIED_ROLES:
        with _connect(db.admin_url) as conn:
            conn.execute(f"SET ROLE {role}")
            for table in TABLES:
                assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
                assert conn.execute(f"DELETE FROM {table}").rowcount == 0
            conn.rollback()
        inserts = [_probes(table)[1] for table in TABLES]
        for error in _errors_as(db.admin_url, role, inserts).values():
            assert error.startswith("new row violates row-level security policy")

    archive.ensure_schema()

    _assert_closed(db)


TABLE_GRANTS = [*TABLE_PRIVILEGES]
COLUMNS = {
    "logpile_raw_objects": ["sha256", "bucket"],
    "logpile_raw_files": ["file_id", "source"],
    "logpile_raw_chunks": ["file_id", "content"],
}


@st.composite
def _grant_steps(draw):
    verb = draw(st.sampled_from(["GRANT", "REVOKE"]))
    table = draw(st.sampled_from(TABLES))
    grantee = draw(st.sampled_from(["PUBLIC", "anon", "authenticated", "service_role"]))
    direction = "TO" if verb == "GRANT" else "FROM"
    if draw(st.booleans()):
        privileges = draw(
            st.lists(st.sampled_from(TABLE_GRANTS), min_size=1, unique=True)
        )
    else:
        column = draw(st.sampled_from(COLUMNS[table]))
        privileges = [
            f"{privilege} ({column})"
            for privilege in draw(
                st.lists(st.sampled_from(COLUMN_PRIVILEGES), min_size=1, unique=True)
            )
        ]
    return f"{verb} {', '.join(privileges)} ON {table} {direction} {grantee}"


@given(
    closed_first=st.booleans(),
    steps=st.lists(_grant_steps(), max_size=8),
    secured=st.tuples(*(st.booleans() for _ in TABLES)).map(
        lambda flags: {table for table, on in zip(TABLES, flags, strict=True) if on}
    ),
)
@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_ensure_schema_closes_any_grants_and_touches_nothing_else(
    archive_db, closed_first, steps, secured
):
    """Whatever grants the owner has made, ensure_schema leaves PUBLIC, anon and
    authenticated with none, keeps every other grant, and rewrites a table's
    catalog row only when that table needed closing."""
    db = archive_db
    denied = ("PUBLIC", *DENIED_ROLES)
    _drop_tables(db)
    _create_archive_before_this_check(db)
    with _connect(db.owner_url(), autocommit=True) as conn:
        if closed_first:
            # Supabase's default grants open every new table; start some closed.
            conn.execute(f"REVOKE ALL ON {', '.join(TABLES)} FROM {', '.join(denied)}")
        for step in steps:
            conn.execute(step)
        for table in secured:
            conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    with _connect(db.admin_url) as conn:
        acl_before = _acl_entries(conn)
        versions_before = _catalog_versions(conn)
    needed_closing = {
        table: table not in secured
        or any(entry[0] == table and entry[2] in denied for entry in acl_before)
        for table in TABLES
    }
    event(f"tables that need no change: {list(needed_closing.values()).count(False)}")

    db.archive().ensure_schema()

    with _connect(db.admin_url) as conn:
        assert _privileges(conn, DENIED_ROLES) == []
        assert _row_security(conn) == dict.fromkeys(TABLES, True)
        assert _acl_entries(conn) == [e for e in acl_before if e[2] not in denied]
        versions_after = _catalog_versions(conn)
    rewritten = {t: versions_after[t] != versions_before[t] for t in TABLES}
    assert rewritten == needed_closing


@pytest.mark.parametrize("search_index", [True, False])
def test_ensure_schema_creates_every_index(archive_db, search_index):
    archive_db.archive().ensure_schema(create_search_index=search_index)
    with _connect(archive_db.admin_url) as conn:
        assert _indexes(conn) == (ALL_INDEX_NAMES if search_index else set(INDEX_NAMES))


def test_a_settled_archive_sees_no_lock_and_no_catalog_write(archive_db):
    db = archive_db
    archive = db.archive()
    archive.ensure_schema()
    with _connect(db.admin_url) as conn:
        before = (_catalog_versions(conn), _acl_entries(conn))
    with _connect(db.admin_url) as holder:
        holder.execute(f"LOCK TABLE {', '.join(TABLES)} IN ACCESS EXCLUSIVE MODE")
        try:
            _Background(archive.ensure_schema).finish()
        finally:
            holder.rollback()
    with _connect(db.admin_url) as conn:
        assert (_catalog_versions(conn), _acl_entries(conn)) == before


def test_access_change_commits_before_any_index_build(archive_db):
    db = archive_db
    # What `logpile backup push --defer-search-index` left before this check.
    _create_archive_before_this_check(db, search_index=False)

    db.archive().ensure_schema(create_search_index=True)

    with _connect(db.admin_url) as conn:
        versions = _catalog_versions(conn)
    closed_by = {versions[table] for table in TABLES}
    assert len(closed_by) == 1
    assert closed_by.pop() < versions[SUPABASE_SEARCH_INDEX.name]


def test_ensure_schema_works_without_supabase_roles(plain_db):
    db = plain_db
    archive = db.archive()
    archive.ensure_schema()
    archive.ensure_schema()
    with archive.connect() as conn:
        _push(archive, conn, _candidate(1))
    with _connect(db.owner_url()) as conn:
        assert [len(_rows(conn, table)) for table in TABLES] == [1, 1, 3]
    with _connect(db.admin_url) as conn:
        assert _relations(conn) == dict.fromkeys(TABLES, "r")
        assert _row_security(conn) == dict.fromkeys(TABLES, True)
        assert _indexes(conn) == ALL_INDEX_NAMES


# --- Concurrency ------------------------------------------------------------------


class _Background:
    """Run a function on a daemon thread; finish() re-raises what it raised."""

    def __init__(self, function):
        self.error = None
        self.done = threading.Event()
        threading.Thread(target=self._run, args=(function,), daemon=True).start()

    def _run(self, function):
        try:
            function()
        except BaseException as exc:
            self.error = exc
        finally:
            self.done.set()

    def finish(self, timeout=WAIT):
        assert self.done.wait(timeout), f"still running after {timeout} s"
        if self.error is not None:
            raise self.error


class _RetryCounter:
    def __init__(self, wait):
        self.count = 0
        self._wait = wait
        self._changed = threading.Condition()

    def __call__(self):
        with self._changed:
            self.count += 1
            self._changed.notify_all()
        self._wait()

    def reached(self, count, timeout=5.0) -> bool:
        with self._changed:
            return self._changed.wait_for(lambda: self.count >= count, timeout)


@pytest.fixture
def retries(monkeypatch):
    counter = _RetryCounter(backup_module._wait_before_retry)
    monkeypatch.setattr(backup_module, "_wait_before_retry", counter)
    return counter


def _pause_after_access_step(monkeypatch):
    """Make ensure_schema stop after its access step until the test lets it go."""
    reached = threading.Event()
    proceed = threading.Event()
    access_step = SupabaseArchive._create_tables_and_close_access

    def paused(self, conn):
        access_step(self, conn)
        reached.set()
        if not proceed.wait(WAIT):
            raise TimeoutError("the test never let ensure_schema go on")

    monkeypatch.setattr(SupabaseArchive, "_create_tables_and_close_access", paused)
    return reached, proceed


class _PausingConnection:
    """Pass statements to a real connection, pausing after the n-th one."""

    def __init__(self, conn, pause_after):
        self.conn = conn
        self.pause_after = pause_after
        self.statements = 0
        self.paused = threading.Event()
        self.resume = threading.Event()

    def counted(self):
        self.statements += 1
        if self.statements == self.pause_after:
            self.paused.set()
            if not self.resume.wait(WAIT):
                raise TimeoutError("the test never resumed the writer")

    def execute(self, *args, **kwargs):
        cursor = self.conn.execute(*args, **kwargs)
        self.counted()
        return cursor

    def cursor(self, *args, **kwargs):
        return _PausingCursor(self.conn.cursor(*args, **kwargs), self)


class _PausingCursor:
    def __init__(self, cursor, connection):
        self.cursor = cursor
        self.connection = connection

    def __enter__(self):
        self.cursor.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self.cursor.__exit__(*exc_info)

    def executemany(self, *args, **kwargs):
        self.cursor.executemany(*args, **kwargs)
        self.connection.counted()


def _writer(db, kind, candidate, pause_after):
    """Start a push or `backup index` transaction that pauses partway through.

    A push writes objects, then files, then chunks; `logpile backup index`
    re-chunks a file, so it writes chunks and then files.
    """
    raw = _connect(db.owner_url(lock_timeout="5s"))
    writer = _PausingConnection(raw, pause_after)
    archive = db.archive()

    def run():
        try:
            if kind == "push":
                archive.upsert_file(writer, candidate, provider="r2", bucket="b")
            archive.replace_chunks(writer, candidate, _chunks(candidate, 5))
            raw.commit()
        finally:
            raw.close()

    return writer, _Background(run)


def _queued_locks(db) -> list[tuple[str, str, str]]:
    """The lock requests that sessions in the database wait on."""
    with _connect(db.admin_url, autocommit=True) as conn:
        return conn.execute(
            """
            SELECT l.locktype, coalesce(c.relname, ''), l.mode
            FROM pg_locks l LEFT JOIN pg_class c ON c.oid = l.relation
            WHERE NOT l.granted AND l.pid IN (
                SELECT pid FROM pg_stat_activity WHERE datname = current_database()
            )
            ORDER BY 1, 2, 3
            """
        ).fetchall()


def _queued_on_schema_lock(db) -> bool:
    with _connect(db.admin_url, autocommit=True) as conn:
        return conn.execute(
            """
            SELECT count(*) = 1 FROM pg_locks
            WHERE locktype = 'advisory' AND NOT granted
              AND classid::bigint = %s AND objid::bigint = %s AND objsubid = 1
            """,
            (SCHEMA_LOCK_KEY >> 32, SCHEMA_LOCK_KEY & 0xFFFFFFFF),
        ).fetchone()[0]


def _eventually(predicate, timeout=WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


WRITER_PAUSES = [
    pytest.param("push", 1, id="push-holding-objects"),
    pytest.param("push", 2, id="push-holding-objects-and-files"),
    pytest.param("push", 3, id="push-holding-all-three"),
    pytest.param("index", 1, id="index-holding-chunks"),
    pytest.param("index", 3, id="index-holding-chunks-and-files"),
]


@pytest.mark.parametrize(("kind", "pause_after"), WRITER_PAUSES)
def test_first_run_never_waits_for_a_writer_or_blocks_it(
    supabase_db, retries, kind, pause_after
):
    db = supabase_db
    _create_archive_before_this_check(db)
    archive = db.archive()
    with archive.connect() as conn:
        _push(archive, conn, _candidate(1))
    candidate = _candidate(1 if kind == "index" else 2)
    writer, writing = _writer(db, kind, candidate, pause_after)
    try:
        assert writer.paused.wait(WAIT)
        run = _Background(archive.ensure_schema)
        assert retries.reached(2), "ensure_schema never backed off"
        assert _queued_locks(db) == []
        assert not run.done.is_set()
    finally:
        writer.resume.set()
    writing.finish()
    run.finish()
    _assert_closed(db)
    with _connect(db.owner_url()) as conn:
        chunks = [row[0] for row in _rows(conn, "logpile_raw_chunks")]
    # The writer re-chunked file 1 into 5 chunks, or pushed file 2 with 5.
    if kind == "index":
        assert chunks == ["file-1"] * 5
    else:
        assert chunks == ["file-1"] * 3 + ["file-2"] * 5


@pytest.mark.parametrize(
    "start", ["empty database", "archive before this check", "rls on, grants left"]
)
def test_runs_released_together_all_succeed(supabase_db, start):
    db = supabase_db
    for _ in range(3):
        _drop_tables(db)
        if start != "empty database":
            _create_archive_before_this_check(db)
        if start == "rls on, grants left":
            with _connect(db.owner_url(), autocommit=True) as conn:
                for table in TABLES:
                    conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        barrier = threading.Barrier(3)

        def run(barrier=barrier):
            barrier.wait(WAIT)
            db.archive().ensure_schema()

        runs = [_Background(run) for _ in range(3)]
        for each in runs:
            each.finish()
        _assert_closed(db)
        with _connect(db.admin_url) as conn:
            assert _indexes(conn) == ALL_INDEX_NAMES


def test_ensure_schema_waits_for_the_schema_lock(supabase_db):
    db = supabase_db
    with _connect(db.admin_url, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_KEY,))
        try:
            run = _Background(db.archive().ensure_schema)
            assert _eventually(lambda: _queued_on_schema_lock(db))
            created = holder.execute("SELECT to_regclass('logpile_raw_objects')")
            assert created.fetchone()[0] is None
        finally:
            holder.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_KEY,))
    run.finish()
    _assert_closed(db)


def test_index_builds_wait_for_the_schema_lock(supabase_db, monkeypatch):
    db = supabase_db
    archive = db.archive()
    archive.ensure_schema(create_search_index=False)
    reached, proceed = _pause_after_access_step(monkeypatch)
    run = _Background(archive.ensure_schema)
    with _connect(db.admin_url, autocommit=True) as holder:
        try:
            assert reached.wait(WAIT)
            holder.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_KEY,))
            proceed.set()
            assert _eventually(lambda: _queued_on_schema_lock(db))
            assert SUPABASE_SEARCH_INDEX.name not in _indexes(holder)
        finally:
            proceed.set()
            holder.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_KEY,))
    run.finish()
    with _connect(db.admin_url) as conn:
        assert _indexes(conn) == ALL_INDEX_NAMES


def test_an_index_built_during_the_wait_takes_no_lock(supabase_db, monkeypatch):
    db = supabase_db
    archive = db.archive()
    archive.ensure_schema(create_search_index=False)
    reached, proceed = _pause_after_access_step(monkeypatch)
    run = _Background(archive.ensure_schema)
    with (
        _connect(db.admin_url, autocommit=True) as holder,
        _connect(db.owner_url()) as writer,
    ):
        try:
            assert reached.wait(WAIT)
            holder.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_KEY,))
            proceed.set()
            assert _eventually(lambda: _queued_on_schema_lock(db))
            # Another run builds the index, and a writer then holds the table.
            holder.execute(SUPABASE_SEARCH_INDEX.sql)
            writer.execute("UPDATE logpile_raw_chunks SET role = role WHERE false")
        finally:
            proceed.set()
            holder.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_KEY,))
        run.finish()
        assert _queued_locks(db) == []
        writer.rollback()


def test_index_steps_never_deadlock_with_backup_index(supabase_db):
    db = supabase_db
    archive = db.archive()
    archive.ensure_schema()
    with archive.connect() as conn:
        _push(archive, conn, _candidate(1))
    with _connect(db.admin_url, autocommit=True) as conn:
        for name in sorted(ALL_INDEX_NAMES):
            conn.execute(f"DROP INDEX {name}")
    writer, writing = _writer(db, "index", _candidate(1), pause_after=1)
    try:
        assert writer.paused.wait(WAIT)
        run = _Background(archive.ensure_schema)
        on_chunks = [("relation", "logpile_raw_chunks", "ShareLock")]
        assert _eventually(lambda: _queued_locks(db) == on_chunks)
        with _connect(db.admin_url) as conn:
            built = _indexes(conn)
        assert built == {name for name in INDEX_NAMES if "_files_" in name}
    finally:
        writer.resume.set()
    writing.finish()
    run.finish()
    with _connect(db.admin_url) as conn:
        assert _indexes(conn) == ALL_INDEX_NAMES


@pytest.mark.parametrize(
    "secured", [(), ("logpile_raw_objects",)], ids=["rls-off", "rls-on-objects-only"]
)
def test_first_run_waits_out_an_idle_reader_without_blocking_anyone(
    supabase_db, retries, secured
):
    db = supabase_db
    _create_archive_before_this_check(db)
    archive = db.archive()
    with archive.connect() as conn:
        _push(archive, conn, _candidate(1))
        for table in secured:
            conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    with _connect(db.owner_url()) as reader:
        reader.execute("SELECT count(*) FROM logpile_raw_chunks").fetchone()
        run = _Background(archive.ensure_schema)
        assert retries.reached(2)
        probes = db.archive(statement_timeout="2s")
        with probes.connect() as conn:
            assert [len(_rows(conn, table)) for table in TABLES] == [1, 1, 3]
        assert len(probes.search("exact thing")) == 3
        with probes.connect() as conn:
            _push(probes, conn, _candidate(2))
        assert _queued_locks(db) == []
        assert not run.done.is_set()
        reader.commit()
        run.finish()
    _assert_closed(db)


def test_retries_back_off_50_to_250_ms_at_random(monkeypatch):
    waits = []
    monkeypatch.setattr(backup_module.time, "sleep", waits.append)
    for _ in range(200):
        backup_module._wait_before_retry()
    assert all(0.05 <= wait <= 0.25 for wait in waits)
    assert max(waits) - min(waits) > 0.1


def test_first_run_gives_up_with_a_clear_error(supabase_db, retries, monkeypatch):
    assert backup_module.ACCESS_LOCK_BUDGET_SECONDS == 30
    budget = 1.0
    monkeypatch.setattr(backup_module, "ACCESS_LOCK_BUDGET_SECONDS", budget)
    db = supabase_db
    _create_archive_before_this_check(db)
    archive = db.archive()
    with _connect(db.owner_url()) as reader:
        reader.execute("SELECT count(*) FROM logpile_raw_chunks").fetchone()
        started = time.monotonic()
        run = _Background(archive.ensure_schema)
        try:
            assert run.done.wait(WAIT), "ensure_schema kept retrying past its budget"
            took = time.monotonic() - started
        finally:
            reader.rollback()
    assert took >= budget, "ensure_schema gave up before its budget ran out"
    assert isinstance(run.error, RuntimeError)
    assert str(run.error).startswith("Another transaction held the archive tables")
    assert "Retry" in str(run.error)
    assert retries.count >= 2
    with _connect(db.admin_url) as conn:
        assert _row_security(conn) == dict.fromkeys(TABLES, False)
    archive.ensure_schema()
    _assert_closed(db)


def test_waiting_for_another_run_leaves_the_budget_whole(
    supabase_db, retries, monkeypatch
):
    budget = 2.0
    monkeypatch.setattr(backup_module, "ACCESS_LOCK_BUDGET_SECONDS", budget)
    db = supabase_db
    _create_archive_before_this_check(db)
    archive = db.archive()
    with (
        _connect(db.admin_url, autocommit=True) as holder,
        _connect(db.owner_url()) as reader,
    ):
        reader.execute("SELECT count(*) FROM logpile_raw_chunks").fetchone()
        holder.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_KEY,))
        try:
            run = _Background(archive.ensure_schema)
            assert _eventually(lambda: _queued_on_schema_lock(db))
            # Another run holds the schema lock for longer than the budget.
            time.sleep(budget + 0.5)
        finally:
            holder.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_KEY,))
        assert retries.reached(1), "ensure_schema gave up at its first lock failure"
        reader.commit()
        run.finish()
    _assert_closed(db)
