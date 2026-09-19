#!/usr/bin/env python3
"""Quick & dirty: strip a .lorebook's entries down to just text + displayName.

Usage:
    python3 filter_lorebook.py INPUT.lorebook [OUTPUT.json]

Prints to stdout if OUTPUT is omitted.
"""
import json
import sys

KEEP = ["text", "displayName"]


def main() -> None:
    in_path = sys.argv[1]
    with open(in_path, encoding="utf-8") as f:
        data = json.load(f)

    filtered = [{k: e.get(k) for k in KEEP} for e in data.get("entries", [])]

    out = json.dumps(filtered, ensure_ascii=False, indent=2)
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w", encoding="utf-8") as f:
            f.write(out + "\n")
    else:
        print(out)


if __name__ == "__main__":
    main()
