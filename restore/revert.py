#!/usr/bin/env python3
"""Undo a stage restore: swaps every record back to its `before` value.

    python3 restore/revert.py restore/stage_restore_2026-09-22.json
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import twenty_api as tw

path = sys.argv[1] if len(sys.argv) > 1 else "restore/stage_restore_2026-09-22.json"
data = json.load(open(path))
for r in data["records"]:
    if r["before"] == r["after"]:
        continue
    tw.patch("/opportunities/" + r["id"], {"stage": r["before"]})
    print(f"{r['name']}: {r['after']} -> {r['before']}")
