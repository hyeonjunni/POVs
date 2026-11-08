from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from app.retrieval.evaluation import (
    METRIC_CUTOFFS,
    evaluate_rankings,
    nearest_rank_percentiles,
    query_metrics,
    run_fixture_evaluation,
)
from app.retrieval.chunking import CHUNKER_VERSION


FIXTURE_PATH = Path(__file__).parent / "data" / "retrieval_eval_v1.json"


def test_query_metrics_use_best_chunk_rank_per_record_and_hand_calculated_values():
    # Removing record-level deduplication or changing the binary metric formula breaks these literals.
    metrics = query_metrics(
        ["record-a", "record-a", "record-b", "record-c"], {"record-b", "record-c"}, 3
    )

    assert metrics == pytest.approx(
        {"recall": 1.0, "mrr": 0.5, "ndcg": (1 / math.log2(3) + 1 / math.log2(4)) / (1 + 1 / math.log2(3))}
    )


def test_query_metrics_cover_empty_no_relevant_and_full_recall_cases():
    # Returning nonzero values without a hit, or including no-qrel queries in macro metrics, breaks this.
    assert query_metrics([], {"record-a"}, 10) == {"recall": 0.0, "mrr": 0.0, "ndcg": 0.0}
    assert query_metrics(["record-a"], set(), 10) is None
    assert query_metrics(["record-a", "record-b"], {"record-a", "record-b"}, 10) == pytest.approx(
        {"recall": 1.0, "mrr": 1.0, "ndcg": 1.0}
    )


def test_macro_evaluation_excludes_no_qrels_and_reports_empty_counts():
    # Averaging unjudged queries or chunk duplicates would change this macro report.
    report = evaluate_rankings(
        {
            "hit": {"ranked_record_ids": ["record-a", "record-a"], "relevant_record_ids": ["record-a"]},
            "miss": {"ranked_record_ids": [], "relevant_record_ids": ["record-b"]},
            "unjudged": {"ranked_record_ids": [], "relevant_record_ids": []},
        },
        cutoffs=(1,),
    )

    assert report == {
        "query_count": 3,
        "judged_query_count": 2,
        "no_qrel_query_count": 1,
        "empty_result_query_count": 2,
        "macro": {"1": {"recall": 0.5, "mrr": 0.5, "ndcg": 0.5}},
    }


def test_nearest_rank_percentiles_and_timing_stage_samples_are_explicit():
    # Interpolating percentiles or silently omitting stages would hide latency evidence gaps.
    assert nearest_rank_percentiles([10, 20, 30, 40]) == {
        "sample_count": 4,
        "p50_ns": 20,
        "p95_ns": 40,
    }


def test_fixture_evaluation_uses_production_indexing_search_and_is_deterministic():
    # A different ranking pipeline, default database, or nondeterministic payload changes this result.
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    first = run_fixture_evaluation(fixture, warmups=0, iterations=1)
    second = run_fixture_evaluation(fixture, warmups=0, iterations=1)

    assert set(first["arms"]) == {"substring", "bm25", "vector", "hybrid_rrf"}
    assert first["fixture_sha256"] == second["fixture_sha256"]
    assert first["arms"] == second["arms"]
    assert json.dumps(first["arms"], ensure_ascii=False, sort_keys=True).encode() == json.dumps(
        second["arms"], ensure_ascii=False, sort_keys=True
    ).encode()
    assert first["metadata"]["corpus_record_count"] == 4
    assert first["metadata"]["chunker_version"] == CHUNKER_VERSION
    assert {"platform", "machine", "processor"} <= set(first["metadata"])
    assert "hardware" not in first["metadata"]
    assert first["metadata"]["evidence_class"] == "synthetic-control-flow"
    for timing in first["latency"].values():
        assert set(timing) == {"aggregate", "by_query_k"}
        assert set(timing["aggregate"]) == {"query_embedding_ns", "db_candidate_load_ns", "bm25_ns", "vector_ns", "fusion_ns", "end_to_end_ns"}
        assert all(stage["sample_count"] == 16 for stage in timing["aggregate"].values())
        assert all(stage["sample_count"] == 1 for query in timing["by_query_k"].values() for per_k in query.values() for stage in per_k.values())
    assert set(METRIC_CUTOFFS) == {1, 3, 5, 10}


def test_cli_can_write_separate_retrieval_and_latency_evidence(tmp_path):
    # Dropping a report section or sharing a fabricated latency payload breaks the evidence split.
    retrieval_output = tmp_path / "retrieval.json"
    latency_output = tmp_path / "latency.json"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/evaluate_retrieval.py",
            "--warmups",
            "0",
            "--iterations",
            "1",
            "--output",
            str(retrieval_output),
            "--latency-output",
            str(latency_output),
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    retrieval, latency = json.loads(retrieval_output.read_text(encoding="utf-8")), json.loads(latency_output.read_text(encoding="utf-8"))
    assert "latency" not in retrieval
    assert set(retrieval) == {"arms", "fixture_sha256", "indexing", "metadata"}
    assert set(latency) == {"fixture_sha256", "indexing", "latency", "metadata"}
    assert retrieval["fixture_sha256"] == latency["fixture_sha256"]
    assert retrieval["metadata"] == latency["metadata"]
