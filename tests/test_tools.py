from unittest.mock import MagicMock

import pytest

from agent.tools import QueryResult, SqlValidationError, run_query, validate_sql


def test_validate_sql_accepts_select():
    assert validate_sql("SELECT 1 LIMIT 10") == "SELECT 1 LIMIT 10"


def test_validate_sql_adds_limit_if_missing():
    result = validate_sql("SELECT * FROM t")
    assert "LIMIT" in result.upper()


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE t",
        "DELETE FROM t",
        "INSERT INTO t VALUES (1)",
        "SELECT 1; DROP TABLE t",
        "UPDATE t SET x = 1",
        "GRANT SELECT ON t TO ROLE r",
        "CALL some_procedure()",
    ],
)
def test_validate_sql_rejects_non_select(sql):
    with pytest.raises(SqlValidationError):
        validate_sql(sql)


def test_validate_sql_rejects_multiple_statements():
    with pytest.raises(SqlValidationError):
        validate_sql("SELECT 1; SELECT 2")


def test_run_query_returns_rows():
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.description = [("GEO_NAME",), ("POPULATION",)]
    cursor.fetchmany.return_value = [("Travis County, TX", 1290000)]

    result = run_query(conn, "SELECT geo_name, population FROM t LIMIT 10")

    assert isinstance(result, QueryResult)
    assert result.columns == ["GEO_NAME", "POPULATION"]
    assert result.rows == [("Travis County, TX", 1290000)]
    assert result.truncated is False


def test_run_query_empty_result():
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.description = [("GEO_NAME",)]
    cursor.fetchmany.return_value = []

    result = run_query(conn, "SELECT geo_name FROM t WHERE 1=0")

    assert result.rows == []


def test_run_query_rejects_bad_sql_before_hitting_snowflake():
    conn = MagicMock()

    with pytest.raises(SqlValidationError):
        run_query(conn, "DROP TABLE t")

    conn.cursor.assert_not_called()
