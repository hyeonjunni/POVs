from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.retrieval.evaluation import run_fixture_evaluation


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the deterministic M2 retrieval evaluation.")
    parser.add_argument("--fixture", type=Path, default=Path(__file__).resolve().parents[1] / "tests" / "data" / "retrieval_eval_v1.json")
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--latency-output", type=Path)
    args = parser.parse_args()
    raw = args.fixture.read_bytes()
    report = run_fixture_evaluation(json.loads(raw.decode("utf-8")), warmups=args.warmups, iterations=args.iterations, fixture_sha256=hashlib.sha256(raw).hexdigest())
    output_report = report
    if args.latency_output:
        latency_report = {key: report[key] for key in ("fixture_sha256", "metadata", "indexing", "latency")}
        args.latency_output.write_text(json.dumps(latency_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        output_report = {key: value for key, value in report.items() if key != "latency"}
    output = json.dumps(output_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
