-- Ad hoc queries used to discover the real structure of the US Open Census
-- Data Marketplace listing after adding it to the trial account. Results
-- from these should be used to fill in agent/schema_context.py.

-- 1. Confirm the share was added and find its database name.
SHOW DATABASES;

-- 2. List schemas inside that database (replace <CENSUS_DB>).
SHOW SCHEMAS IN DATABASE <CENSUS_DB>;

-- 3. List tables/views in the schema you care about (replace <SCHEMA>).
SHOW TABLES IN SCHEMA <CENSUS_DB>.<SCHEMA>;
SHOW VIEWS IN SCHEMA <CENSUS_DB>.<SCHEMA>;

-- 4. Inspect columns for a specific table.
DESCRIBE TABLE <CENSUS_DB>.<SCHEMA>.<TABLE_NAME>;

-- 5. Peek at sample rows to understand grain, units, and value ranges.
SELECT * FROM <CENSUS_DB>.<SCHEMA>.<TABLE_NAME> LIMIT 20;

-- 6. Get row counts / sizes for every table to spot the main fact table(s)
--    vs. small dimension/lookup tables (e.g. geography, variable definitions).
SELECT table_name, table_type, row_count, bytes
FROM <CENSUS_DB>.information_schema.tables
ORDER BY row_count DESC;

-- 7. If there's a geography dimension table, check what levels it covers
--    (state/county/place/etc.) - this determines how to handle ambiguous
--    place names (e.g. "Washington" state vs. county vs. city).
-- SELECT DISTINCT geo_level FROM <CENSUS_DB>.<SCHEMA>.<GEOGRAPHY_TABLE>;

-- 8. If there's a "variable"/"indicator" dimension table, list what
--    measures actually exist - this defines what's answerable vs. out of scope.
-- SELECT DISTINCT variable_name FROM <CENSUS_DB>.<SCHEMA>.<VARIABLE_TABLE>;
