"""One-time, offline script: builds the two-stage searchable corpus for
long-tail column lookup.

Two-stage design (fixes a real bug found via live testing - see
REFLECTION.md): a flat, column-level ranking let concepts with many columns
(e.g. a 98-column concept) flood the top-K results and crowd out a
correct-but-smaller concept just by sheer column count. Splitting into two
stages removes that bias:

  Stage 1 - CONCEPT search: one document per TABLE_NUMBER (~150 total, not
  8,000+), so every concept gets exactly one shot at the ranking regardless
  of how many columns it has. Its searchable text includes the deduplicated
  set of every field-level phrase it contains, so specific details ("male",
  "under 5 years") are still findable even though this is concept-level.

  Stage 2 - COLUMN listing: once a concept is chosen, no search/ranking is
  needed - just a deterministic filter returning ALL of that concept's
  columns (a small, complete, guaranteed-correct list) for the model to read
  and pick the exact one it needs.

Run this manually whenever the underlying Census dataset schema changes:

    python -m retrieval.build_corpus

Outputs (committed to the repo under retrieval/data/, small - tens of MB):
    columns.parquet          - one row per COLUMN (stage 2 source, no index needed)
    concepts.parquet          - one row per CONCEPT (stage 1 source)
    concept_tfidf_vectorizer.pkl
    concept_tfidf_matrix.npz
    concept_embeddings.npy
"""

import pickle
import re
import sys
import tomllib
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import sparse
from sklearn.feature_extraction.text import TfidfVectorizer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.tools import get_connection

YEARS = ["2019", "2020"]
DATA_DIR = Path(__file__).resolve().parent / "data"

# FIELD_LEVEL_1 is skipped: for estimate rows it's always the constant
# "Estimate" (we only index estimate columns, not their margin-of-error
# twins - the two share identical descriptive text, so indexing both would
# just double the corpus with near-duplicates for no search benefit; the
# model already knows the e/m column-naming convention).
FIELD_LEVEL_COLUMNS = [
    "FIELD_LEVEL_2", "FIELD_LEVEL_3", "FIELD_LEVEL_4", "FIELD_LEVEL_5",
    "FIELD_LEVEL_6", "FIELD_LEVEL_7", "FIELD_LEVEL_8",
    '"FIELD_LEVELl_9"',  # real typo in the source data - must be quoted exactly
    "FIELD_LEVEL_10",
]
FIELD_LEVEL_ALIASES = [c.strip('"') for c in FIELD_LEVEL_COLUMNS]

_ESTIMATE_RE = re.compile(r"e\d+$")


def _physical_table(year: str, table_number: str) -> str:
    return f"{year}_CBG_{table_number[:3]}"


def _combined_text(table_title: str, field_levels: list) -> str:
    parts = [table_title] + [v for v in field_levels if v]
    return " > ".join(parts)


def fetch_year_columns(conn, database: str, year: str) -> list[dict]:
    cols_sql = ", ".join(["TABLE_ID", "TABLE_NUMBER", "TABLE_TITLE"] + FIELD_LEVEL_COLUMNS)
    sql = f'SELECT {cols_sql} FROM "{database}"."PUBLIC"."{year}_METADATA_CBG_FIELD_DESCRIPTIONS"'
    cur = conn.cursor()
    cur.execute(sql)
    rows = cur.fetchall()
    cur.close()

    out = []
    for row in rows:
        table_id, table_number, table_title, *field_levels = row
        if not _ESTIMATE_RE.search(table_id or ""):
            continue  # skip margin-of-error columns
        out.append(
            {
                "TABLE_ID": table_id,
                "YEAR": year,
                "TABLE_NUMBER": table_number,
                "TABLE_TITLE": table_title,
                "PHYSICAL_TABLE": _physical_table(year, table_number),
                "TEXT": _combined_text(table_title, field_levels),
                "FIELD_PHRASES": [v for v in field_levels if v],
            }
        )
    return out


def build_concepts(columns_df: pd.DataFrame) -> pd.DataFrame:
    """One row per TABLE_NUMBER, deduplicated across years (a concept's
    structure doesn't change year to year, only its data does)."""
    concepts = []
    for table_number, group in columns_df.groupby("TABLE_NUMBER"):
        table_title = group.iloc[0]["TABLE_TITLE"]
        # Deduplicated set of every distinct field-level phrase this concept
        # contains, so specific details ("Male", "Under 5 years") remain
        # findable even at this coarser, concept-level search granularity.
        unique_phrases = sorted({p for phrases in group["FIELD_PHRASES"] for p in phrases})
        text = table_title + " | " + " | ".join(unique_phrases) if unique_phrases else table_title
        concepts.append(
            {
                "TABLE_NUMBER": table_number,
                "TABLE_TITLE": table_title,
                "TEXT": text,
                "NUM_COLUMNS": len(group),
            }
        )
    return pd.DataFrame(concepts)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with open(".streamlit/secrets.toml", "rb") as f:
        secrets = tomllib.load(f)
    conn = get_connection(secrets)
    database = secrets["SNOWFLAKE_DATABASE"]

    all_columns = []
    for year in YEARS:
        year_columns = fetch_year_columns(conn, database, year)
        print(f"{year}: {len(year_columns)} estimate columns")
        all_columns.extend(year_columns)

    columns_df = pd.DataFrame(all_columns)
    print(f"Total column rows: {len(columns_df)}")
    columns_df.drop(columns=["FIELD_PHRASES"]).to_parquet(DATA_DIR / "columns.parquet", index=False)

    concepts_df = build_concepts(columns_df)
    print(f"Total distinct concepts: {len(concepts_df)}")
    concepts_df.to_parquet(DATA_DIR / "concepts.parquet", index=False)

    print("Fitting concept-level TF-IDF vectorizer...")
    # ngram_range=(1, 2): unigram-only TF-IDF badly under-ranks multi-word
    # concept names (verified live - "travel time" failed to surface "Travel
    # Time To Work" in the top results without bigrams).
    vectorizer = TfidfVectorizer(stop_words="english", max_df=0.9, ngram_range=(1, 2))
    tfidf_matrix = vectorizer.fit_transform(concepts_df["TEXT"].tolist())
    with open(DATA_DIR / "concept_tfidf_vectorizer.pkl", "wb") as f:
        pickle.dump(vectorizer, f)
    sparse.save_npz(DATA_DIR / "concept_tfidf_matrix.npz", tfidf_matrix)
    print(f"Concept TF-IDF matrix shape: {tfidf_matrix.shape}")

    print("Computing concept-level semantic embeddings...")
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer("all-MiniLM-L6-v2")
    embeddings = model.encode(concepts_df["TEXT"].tolist(), show_progress_bar=True, batch_size=64)
    np.save(DATA_DIR / "concept_embeddings.npy", embeddings.astype(np.float32))
    print(f"Concept embeddings shape: {embeddings.shape}")

    print("Done. Artifacts written to", DATA_DIR)


if __name__ == "__main__":
    main()
