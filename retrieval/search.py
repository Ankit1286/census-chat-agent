"""Two-stage query-time lookup over the pre-built corpus (see build_corpus.py).

Stage 1 - search_concepts(): hybrid (TF-IDF + semantic) search over ~150
concept-level documents, one per TABLE_NUMBER. Every concept gets exactly
one shot at the ranking regardless of how many columns it has, which is
what fixes the "a 98-column concept crowds out a 2-column concept" bug
found via live testing.

Stage 2 - list_columns_for_concept(): once a concept is chosen, no search is
needed - just a deterministic filter returning every column that concept
has, for the model to read and pick the exact one it needs.

Both stages load their data once (module-level singletons), never per-query.
"""

import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import sparse
from sklearn.metrics.pairwise import cosine_similarity

DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass
class ConceptResult:
    table_number: str
    table_title: str
    text: str
    rank_score: float


@dataclass
class ColumnResult:
    table_id: str
    year: str
    table_number: str
    physical_table: str
    text: str


class ConceptIndex:
    def __init__(self):
        self.concepts: pd.DataFrame = pd.read_parquet(DATA_DIR / "concepts.parquet")
        with open(DATA_DIR / "concept_tfidf_vectorizer.pkl", "rb") as f:
            self.vectorizer = pickle.load(f)
        self.tfidf_matrix = sparse.load_npz(DATA_DIR / "concept_tfidf_matrix.npz")
        self.embeddings = np.load(DATA_DIR / "concept_embeddings.npy")


_concept_index: ConceptIndex | None = None
_columns_df: pd.DataFrame | None = None
_embedding_model = None


def get_concept_index() -> ConceptIndex:
    global _concept_index
    if _concept_index is None:
        _concept_index = ConceptIndex()
    return _concept_index


def get_columns_df() -> pd.DataFrame:
    global _columns_df
    if _columns_df is None:
        _columns_df = pd.read_parquet(DATA_DIR / "columns.parquet")
    return _columns_df


def _get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer

        _embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _embedding_model


def _concept_results(index: ConceptIndex, positions, scores) -> list[ConceptResult]:
    out = []
    for pos, score in zip(positions, scores):
        row = index.concepts.iloc[pos]
        out.append(ConceptResult(row["TABLE_NUMBER"], row["TABLE_TITLE"], row["TEXT"], float(score)))
    return out


def _tfidf_concept_search(query: str, top_k: int, index: ConceptIndex) -> list[ConceptResult]:
    query_vec = index.vectorizer.transform([query])
    scores = cosine_similarity(query_vec, index.tfidf_matrix)[0]
    top = np.argsort(-scores)[:top_k]
    return _concept_results(index, top, scores[top])


def _semantic_concept_search(query: str, top_k: int, index: ConceptIndex) -> list[ConceptResult]:
    model = _get_embedding_model()
    query_vec = model.encode([query])
    scores = cosine_similarity(query_vec, index.embeddings)[0]
    top = np.argsort(-scores)[:top_k]
    return _concept_results(index, top, scores[top])


def search_concepts(query: str, top_k: int = 5, per_method_k: int = 20) -> list[ConceptResult]:
    """Stage 1: hybrid search for the right CONCEPT (TABLE_NUMBER), via
    Reciprocal Rank Fusion of keyword (TF-IDF) and semantic search - combined
    by rank position, not raw score, since the two methods' scores live on
    different, uncalibrated scales."""
    index = get_concept_index()
    tfidf_results = _tfidf_concept_search(query, per_method_k, index)
    semantic_results = _semantic_concept_search(query, per_method_k, index)

    RRF_K = 60
    fused_scores: dict[str, float] = {}
    by_number: dict[str, ConceptResult] = {}
    for rank, r in enumerate(tfidf_results):
        fused_scores[r.table_number] = fused_scores.get(r.table_number, 0.0) + 1.0 / (RRF_K + rank + 1)
        by_number[r.table_number] = r
    for rank, r in enumerate(semantic_results):
        fused_scores[r.table_number] = fused_scores.get(r.table_number, 0.0) + 1.0 / (RRF_K + rank + 1)
        by_number.setdefault(r.table_number, r)

    ranked = sorted(fused_scores, key=lambda tn: -fused_scores[tn])[:top_k]
    return [
        ConceptResult(tn, by_number[tn].table_title, by_number[tn].text, fused_scores[tn]) for tn in ranked
    ]


def list_columns_for_concept(table_number: str, year: str | None = None) -> list[ColumnResult]:
    """Stage 2: deterministic - no search, just every column belonging to
    this one concept (optionally filtered to one year)."""
    df = get_columns_df()
    mask = df["TABLE_NUMBER"] == table_number
    if year:
        mask &= df["YEAR"] == year
    matches = df[mask]
    return [
        ColumnResult(row["TABLE_ID"], row["YEAR"], row["TABLE_NUMBER"], row["PHYSICAL_TABLE"], row["TEXT"])
        for _, row in matches.iterrows()
    ]
