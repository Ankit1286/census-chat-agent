"""SQL validation and Snowflake query execution for the query_census_data tool."""

import re
from dataclasses import dataclass

import snowflake.connector
from cryptography.hazmat.primitives import serialization

# Comfortably covers a full per-county listing for any single US state (Texas has
# the most, at 254) while still bounding a truly unbounded query (e.g. all ~240k
# block groups, or all ~3,143 US counties nationwide). The system prompt's rule 3
# (agent/prompts.py) is the real safeguard beyond this cap - it tells the model to
# prefer aggregation and to never treat a truncated row list as complete data.
MAX_ROWS = 300
STATEMENT_TIMEOUT_SECONDS = 30

_SELECT_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_FORBIDDEN_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|CALL|COPY|PUT|GET|EXECUTE)\b",
    re.IGNORECASE,
)


class SqlValidationError(ValueError):
    """Raised when model-generated SQL fails the safety checks below."""


class SnowflakeUnavailableError(RuntimeError):
    """Raised when we cannot establish a Snowflake connection at all."""


def validate_sql(sql: str) -> str:
    """Defense-in-depth check on model-generated SQL before it's executed.

    The Snowflake role is already read-only via grants, so this is a second,
    independent layer against a malformed or adversarially-crafted query
    (e.g. a prompt-injection attempt embedded in user input).
    """
    statements = [s for s in sql.strip().split(";") if s.strip()]
    if len(statements) != 1:
        raise SqlValidationError("Only a single SQL statement is allowed.")
    stmt = statements[0]
    if not _SELECT_RE.match(stmt):
        raise SqlValidationError("Only SELECT statements are allowed.")
    if _FORBIDDEN_RE.search(stmt):
        raise SqlValidationError("Statement contains a forbidden keyword.")
    if not re.search(r"\bLIMIT\b", stmt, re.IGNORECASE):
        stmt = f"{stmt.rstrip()} LIMIT {MAX_ROWS}"
    return stmt


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool


def _load_private_key_der(pem_text: str) -> bytes:
    """Convert a PEM-encoded PKCS#8 private key into the DER bytes the
    Snowflake connector expects for key-pair auth."""
    private_key = serialization.load_pem_private_key(pem_text.encode(), password=None)
    return private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def get_connection(secrets: dict):
    """Connect to Snowflake.

    Prefers key-pair auth (SNOWFLAKE_PRIVATE_KEY, a PEM string) over password
    auth: Snowflake's mandatory MFA policy on new accounts blocks plain
    password logins from non-interactive clients (scripts, the deployed
    Streamlit app), but key-pair auth is exempt and is Snowflake's recommended
    approach for service/programmatic connections anyway.
    """
    try:
        common_kwargs = dict(
            account=secrets["SNOWFLAKE_ACCOUNT"],
            user=secrets["SNOWFLAKE_USER"],
            role=secrets["SNOWFLAKE_ROLE"],
            warehouse=secrets["SNOWFLAKE_WAREHOUSE"],
            database=secrets["SNOWFLAKE_DATABASE"],
            schema=secrets["SNOWFLAKE_SCHEMA"],
            login_timeout=10,
            network_timeout=15,
        )
        if secrets.get("SNOWFLAKE_PRIVATE_KEY"):
            return snowflake.connector.connect(
                private_key=_load_private_key_der(secrets["SNOWFLAKE_PRIVATE_KEY"]),
                **common_kwargs,
            )
        return snowflake.connector.connect(password=secrets["SNOWFLAKE_PASSWORD"], **common_kwargs)
    except Exception as exc:
        raise SnowflakeUnavailableError(str(exc)) from exc


def run_query(conn, sql: str) -> QueryResult:
    """Validate and execute a single SELECT, returning at most MAX_ROWS rows."""
    validated = validate_sql(sql)
    cur = conn.cursor()
    try:
        cur.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {STATEMENT_TIMEOUT_SECONDS}")
        cur.execute(validated)
        rows = cur.fetchmany(MAX_ROWS + 1)
        columns = [c[0] for c in cur.description] if cur.description else []
        truncated = len(rows) > MAX_ROWS
        return QueryResult(columns=columns, rows=rows[:MAX_ROWS], truncated=truncated)
    finally:
        cur.close()
