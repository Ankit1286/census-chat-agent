"""Tests for the two-stage retrieval system (retrieval/search.py).

Uses the real, committed corpus artifacts (retrieval/data/*) rather than a
synthetic fixture - this is a static, offline-built dataset checked into the
repo specifically so it doesn't need Snowflake or network access to test
against. The one thing that does need a local model load is the semantic
half, which uses the sentence-transformers cache already populated by
running retrieval/build_corpus.py.
"""

import pytest

from retrieval.search import list_columns_for_concept, search_concepts


def test_search_concepts_finds_sex_by_age():
    """Regression test for the real bug found via live testing: a flat,
    column-level ranking let a 98-column concept crowd out unrelated ones.
    Concept-level ranking should reliably surface B01001 for its own name."""
    results = search_concepts("sex by age", top_k=5)
    assert any(r.table_number == "B01001" for r in results)


def test_search_concepts_finds_travel_time():
    """Regression test for the exact live failure: 'travel time' used to not
    surface B08303 ('Travel Time To Work') in the results at all."""
    results = search_concepts("travel time", top_k=5)
    assert any(r.table_number == "B08303" for r in results)


def test_search_concepts_returns_ranked_scores():
    results = search_concepts("median household income", top_k=5)
    assert len(results) <= 5
    assert len(results) > 0
    scores = [r.rank_score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_list_columns_for_concept_returns_expected_shape():
    columns = list_columns_for_concept("B01001", year="2020")
    assert len(columns) == 49  # verified manually: Sex By Age has 49 estimate fields
    assert all(c.table_number == "B01001" for c in columns)
    assert all(c.year == "2020" for c in columns)
    assert all(c.physical_table == "2020_CBG_B01" for c in columns)


def test_list_columns_for_concept_finds_specific_column():
    columns = list_columns_for_concept("B01001", year="2020")
    matches = [c for c in columns if "Male" in c.text and "Under 5" in c.text]
    assert len(matches) == 1
    assert matches[0].table_id == "B01001e3"


def test_list_columns_for_concept_filters_by_year():
    columns_2019 = list_columns_for_concept("B01001", year="2019")
    columns_2020 = list_columns_for_concept("B01001", year="2020")
    assert all(c.physical_table == "2019_CBG_B01" for c in columns_2019)
    assert all(c.physical_table == "2020_CBG_B01" for c in columns_2020)


def test_list_columns_for_concept_unknown_returns_empty():
    assert list_columns_for_concept("B99999", year="2020") == []
