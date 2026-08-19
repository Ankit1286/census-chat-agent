"""Curated description of the real US Open Census Data schema (SafeGraph's
free Census dataset, via the Snowflake Marketplace listing
US_OPEN_CENSUS_DATA_NEIGHBORHOOD_INSIGHTS_FREE_DATASET), rendered into the
system prompt as grounding context.

Discovered via snowflake/explore_schema.sql + scripts/discover_schema.py and
scripts/describe_tables.py. Key facts about the real dataset that shape this
file:

- Grain: US Census Block Group (CBG) - the finest ACS geography. There is
  NO city/place/ZIP table; only state and county (via FIPS codes) plus raw
  lat/long per block group.
- Data is ACS 5-year *survey estimates* (with margins of error), not a full
  headcount, except for the separate 2020 Redistricting tables which are
  actual decennial Census counts.
- ~150 ACS table families (B01, B02, ... B99, plus a handful of C-prefixed
  "collapsed" tables), each becoming one physical Snowflake table per year,
  e.g. 2020_CBG_B19 bundles every B19xxx (income) sub-table's columns. This
  is too much to hardcode column-by-column, so the metadata table itself
  (also queryable) is the escape hatch for anything not in the curated list
  below - see the "long tail" note in render_schema_context().
"""

from dataclasses import dataclass

DATABASE = "US_OPEN_CENSUS_DATA_NEIGHBORHOOD_INSIGHTS_FREE_DATASET"
SCHEMA = "PUBLIC"
YEARS_AVAILABLE = ["2019", "2020"]  # ACS 5-year estimates; also "2020_REDISTRICTING" (decennial counts, different shape)


@dataclass
class HeadlineMetric:
    label: str
    table_family: str  # e.g. "B01" -> physical table "<YEAR>_CBG_B01"
    column_estimate: str  # e.g. "B01003e1"
    column_moe: str | None  # margin of error column, if any
    notes: str = ""


HEADLINE_METRICS: list[HeadlineMetric] = [
    HeadlineMetric("Total population", "B01", "B01003e1", "B01003m1"),
    HeadlineMetric(
        "Population by sex and age breakdown", "B01", "B01001e1..B01001e49",
        "B01001m1..B01001m49",
        notes="Field 1 = total; use list_concept_columns('B01001', year) for a specific age/sex bracket.",
    ),
    HeadlineMetric(
        "Race", "B02", "B02001e1..e8", "B02001m1..m8",
        notes="e1=total, e2=White alone, e3=Black/African American alone, e4=American Indian/Alaska Native alone, "
        "e5=Asian alone, e6=Native Hawaiian/Pacific Islander alone, e7=Some other race alone, e8=Two or more races.",
    ),
    HeadlineMetric(
        "Hispanic or Latino origin", "B03", "B03002e1..e12", "B03002m1..m12",
        notes="e1=total, e12=Hispanic or Latino (of any race); see B03003 for the simpler total/Hispanic/not-Hispanic split.",
    ),
    HeadlineMetric("Median household income (per block group)", "B19", "B19013e1", "B19013m1",
                    notes="A per-CBG MEDIAN - cannot be summed or averaged across block groups (see rules below)."),
    HeadlineMetric(
        "Household income distribution (brackets)", "B19", "B19001e1..e17", "B19001m1..m17",
        notes="e1=total households; e2..e17 are income brackets from <$10k to $200k+ - counts, safe to sum across CBGs.",
    ),
    HeadlineMetric("Median home value (per block group)", "B25", "B25077e1", "B25077m1",
                    notes="A per-CBG MEDIAN - cannot be summed or averaged across block groups."),
    HeadlineMetric("Median gross rent (per block group)", "B25", "B25064e1", "B25064m1",
                    notes="A per-CBG MEDIAN - cannot be summed or averaged across block groups."),
    HeadlineMetric("Housing units (total)", "B25", "B25001e1", "B25001m1"),
    HeadlineMetric(
        "Educational attainment (25+)", "B15", "B15003e1..e25", "B15003m1..m25",
        notes="e1=total population 25+; other fields are attainment levels (e.g. bachelor's, high school) - counts, safe to sum.",
    ),
    HeadlineMetric(
        "Employment status (16+)", "B23", "B23025e1..e7", "B23025m1..m7",
        notes="e1=total, e2=in labor force, e3=civilian labor force, e4=employed, e5=unemployed, e7=not in labor force.",
    ),
    HeadlineMetric("Poverty status", "B17", "B17010e1... (family type x age of children)", None,
                    notes="Multiple related B17xxx tables; use search_census_concepts('poverty') to find the right one."),
]

GEOGRAPHY_NOTES = """
Geography (CENSUS_BLOCK_GROUP, a 12-digit code): digits 1-2 = state FIPS,
digits 3-5 = county FIPS, digits 6-11 = census tract, digit 12 = block group.

- <YEAR>_METADATA_CBG_FIPS_CODES: columns STATE (2-letter abbrev), STATE_FIPS,
  COUNTY_FIPS, COUNTY (e.g. "Travis County"). Use this to resolve a place
  name to a FIPS prefix, then filter CENSUS_BLOCK_GROUP LIKE
  '<state_fips><county_fips>%' in a data table to get all block groups in
  that county (or just '<state_fips>%' for a whole state).
- <YEAR>_METADATA_CBG_GEOGRAPHIC_DATA: CENSUS_BLOCK_GROUP, AMOUNT_LAND,
  AMOUNT_WATER, LATITUDE, LONGITUDE. No name field - this is for area/location,
  not for resolving place names.
- There is NO city/place/ZIP-level table. A question about a city that is
  not itself a county (e.g. "Austin, TX" is not Travis County, though it's
  mostly within it) cannot be answered exactly from this dataset - say so,
  and offer the containing county's figures as a labeled approximation if
  useful, rather than silently substituting one for the other.
"""

AGGREGATION_RULES = """
- COUNT-like fields (population, housing units, income brackets, education
  levels, etc.) can be validly SUMmed across block groups to roll up to a
  county or state total.
- MEDIAN fields (median household income, median home value, median gross
  rent, and anything else literally named "Median ..." in the metadata) are
  already an aggregate over one block group's households - they CANNOT be
  summed or averaged across block groups to produce a valid county/state
  median. If asked for a median at a geography larger than one block group,
  say plainly that this dataset doesn't support recomputing a true median at
  that level, and offer the alternative of a population-weighted average
  (clearly labeled as an approximation, not a true median) or per-block-group
  figures instead.
- Every estimate column (Xe#) has a paired margin-of-error column (Xm#) at
  90% confidence - these are ACS 5-year survey estimates, not a full
  headcount, except for the separate 2020 Redistricting tables (actual
  decennial Census counts, no sampling error, different table shape).
"""


SQL_SYNTAX_NOTES = """
CRITICAL: table names and ACS column names in this dataset MUST be double-quoted in every
query, or Snowflake will fail to resolve them:
- Table names start with a digit (e.g. 2020_CBG_B01), which is not valid as a bare SQL
  identifier at all - always write "2020_CBG_B01".
- ACS estimate/margin-of-error columns are case-sensitive mixed-case identifiers (e.g.
  B01003e1, not B01003E1) - always write "B01003e1". Unquoted, Snowflake folds it to
  uppercase and you get "invalid identifier 'B01003E1'".
- CENSUS_BLOCK_GROUP and the plain metadata table columns (STATE, COUNTY_FIPS, etc.) are
  ordinary uppercase identifiers and do not need quoting, though quoting them is harmless.

Worked example - total population of Travis County, TX from the 2020 data:
    SELECT SUM(b."B01003e1") AS total_population
    FROM "US_OPEN_CENSUS_DATA_NEIGHBORHOOD_INSIGHTS_FREE_DATASET"."PUBLIC"."2020_CBG_B01" b
    JOIN "US_OPEN_CENSUS_DATA_NEIGHBORHOOD_INSIGHTS_FREE_DATASET"."PUBLIC"."2020_METADATA_CBG_FIPS_CODES" f
      ON b.CENSUS_BLOCK_GROUP LIKE f.STATE_FIPS || f.COUNTY_FIPS || '%'
    WHERE f.STATE = 'TX' AND f.COUNTY = 'Travis County'
    LIMIT 10;
(Or resolve the FIPS prefix in a first query, then filter
CENSUS_BLOCK_GROUP LIKE '48453%' directly in a second - either works.)
"""


def render_schema_context() -> str:
    metrics = "\n".join(
        f"  - {m.label}: table family {m.table_family} (physical table <YEAR>_CBG_{m.table_family}), "
        f"column(s) {m.column_estimate}"
        + (f" (margin of error: {m.column_moe})" if m.column_moe else "")
        + (f" — {m.notes}" if m.notes else "")
        for m in HEADLINE_METRICS
    )
    return f"""Database: {DATABASE}, schema {SCHEMA}. Years available: {", ".join(YEARS_AVAILABLE)} \
(ACS 5-year estimates), plus 2020_REDISTRICTING_* (actual 2020 decennial counts, different table shape).

Table naming convention: <YEAR>_CBG_<FAMILY> holds one row per Census Block Group and columns named \
<TABLE_ID>e<n> (estimate) / <TABLE_ID>m<n> (margin of error) for each ACS field n in that family.

Headline metrics (most questions map to one of these):
{metrics}

For any concept NOT in that list, do NOT try to guess a column code or hand-write SQL against the \
metadata table - use the two dedicated lookup tools instead, in order:
1. search_census_concepts(query) - finds the right high-level CONCEPT (e.g. "Sex By Age", "Travel \
Time To Work") from a plain-English description. Returns several ranked candidates - read their \
titles/descriptions and judge which one actually matches the question (e.g. prefer "Travel Time To \
Work" over "Allocation Of Travel Time To Work" - "Allocation Of ..." tables are data-quality flags, \
not the actual measurement) rather than blindly taking the first result.
2. list_concept_columns(table_number, year) - once you've picked a concept, lists every column it \
contains so you can pick the exact one matching the question's specific detail (e.g. "Male" + \
"Under 5 years") before using that column code in query_census_data.

Geography:{GEOGRAPHY_NOTES}
Aggregation rules (important - violating these produces misleading numbers):{AGGREGATION_RULES}
SQL syntax (important - queries fail without this):{SQL_SYNTAX_NOTES}"""
