"""Compare two DLMM JSONL decision logs from shadow/backtest runs.

Usage:
  python -m opms.research.diff_decision_logs shadow-a.jsonl shadow-b.jsonl

Outputs a JSON summary of mismatches across key keeper decision fields.
"""

import argparse
import json
from pathlib import Path


COMPARE_KEYS = [
    "decision", "action", "urgency",
    "inventory_base", "inventory_quote",
    "net_delta", "mid",
]
FLOAT_KEYS = {"inventory_base", "inventory_quote", "net_delta", "mid"}


def load(path: Path) -> list[dict]:
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _close(a, b, tol):
    if a is None or b is None:
        return a == b
    return abs(a - b) < tol


def compare(reference: list[dict], candidate: list[dict], tolerance: float = 0.01) -> dict:
    common = min(len(reference), len(candidate))
    mismatches: dict[str, int] = {k: 0 for k in COMPARE_KEYS}
    for ref, cand in zip(reference[:common], candidate[:common]):
        for k in COMPARE_KEYS:
            if k in FLOAT_KEYS:
                if not _close(ref.get(k), cand.get(k), tolerance):
                    mismatches[k] += 1
            elif ref.get(k) != cand.get(k):
                mismatches[k] += 1
    return {
        "reference_rows": len(reference),
        "candidate_rows": len(candidate),
        "compared_rows": common,
        "mismatches": mismatches,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("reference", type=Path)
    ap.add_argument("candidate", type=Path)
    ap.add_argument("--tolerance", type=float, default=0.01)
    args = ap.parse_args()

    result = compare(load(args.reference), load(args.candidate),
                     tolerance=args.tolerance)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
