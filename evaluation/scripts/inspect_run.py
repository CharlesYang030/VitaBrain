#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify and summarize one Agent Lab run")
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    path = args.run_dir / "events.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    previous = "0" * 64
    valid = True
    for event in events:
        recorded = event.pop("event_hash")
        valid &= event["prev_hash"] == previous and hashlib.sha256(canonical(event)).hexdigest() == recorded
        previous = recorded
    print(json.dumps({
        "run_dir": str(args.run_dir),
        "event_count": len(events),
        "hash_chain_valid": valid,
        "stage_counts": Counter(event["stage"] for event in events),
        "visibility_counts": Counter(event["visibility"] for event in events),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

