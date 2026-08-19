"""One-off discovery script: connects to Snowflake using .streamlit/secrets.toml
and prints databases / schemas / tables so we can identify the Census dataset
and fill in agent/schema_context.py. Not part of the app or the test suite -
delete or ignore after use.
"""

import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.tools import get_connection


def main():
    secrets_path = Path(__file__).resolve().parents[1] / ".streamlit" / "secrets.toml"
    with secrets_path.open("rb") as f:
        secrets = tomllib.load(f)

    conn = get_connection(secrets)
    cur = conn.cursor()

    print("=== DATABASES ===")
    cur.execute("SHOW DATABASES")
    databases = [row[1] for row in cur.fetchall()]
    for name in databases:
        print(" ", name)

    census_candidates = [
        d for d in databases if "census" in d.lower() or "safegraph" in d.lower() or "open" in d.lower()
    ]
    if not census_candidates:
        print("\nNo obviously-named Census database found. Inspect the list above manually.")
        sys.exit(0)

    for db in census_candidates:
        print(f"\n=== SCHEMAS in {db} ===")
        cur.execute(f'SHOW SCHEMAS IN DATABASE "{db}"')
        schemas = [row[1] for row in cur.fetchall()]
        for s in schemas:
            print(" ", s)

        for schema in schemas:
            if schema.upper() == "INFORMATION_SCHEMA":
                continue
            print(f"\n--- TABLES in {db}.{schema} ---")
            try:
                cur.execute(f'SHOW TABLES IN SCHEMA "{db}"."{schema}"')
                tables = [row[1] for row in cur.fetchall()]
                for t in tables:
                    print(" ", t)
                cur.execute(f'SHOW VIEWS IN SCHEMA "{db}"."{schema}"')
                views = [row[1] for row in cur.fetchall()]
                for v in views:
                    print(" ", v, "(view)")
            except Exception as exc:
                print("   (error listing objects:", exc, ")")

    conn.close()


if __name__ == "__main__":
    main()
