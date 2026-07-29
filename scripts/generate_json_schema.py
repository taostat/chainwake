"""Regenerate the published output schema from the Pydantic models.

Usage:
    uv run python scripts/generate_json_schema.py            # write
    uv run python scripts/generate_json_schema.py --check    # CI check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

from chainwake.output.schema import Payload

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
SCHEMA_PATH = SCHEMA_DIR / "output.json"


def build_schema() -> dict[str, object]:
    """Build the current closed output schema."""
    adapter = TypeAdapter(Payload)
    schema = adapter.json_schema(by_alias=True, ref_template="#/$defs/{model}")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://chainwake.dev/schemas/output.json"
    schema["title"] = "chainwake output payload"
    return schema


def write(schema: dict[str, object], path: Path = SCHEMA_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")


def check(schema: dict[str, object], path: Path = SCHEMA_PATH) -> int:
    if not path.exists():
        print(f"FAIL: {path} does not exist; run without --check.", file=sys.stderr)
        return 1
    on_disk = json.loads(path.read_text())
    if on_disk == schema:
        return 0
    print(
        f"FAIL: {path} is out of sync with its Pydantic model. "
        "Regenerate via `uv run python scripts/generate_json_schema.py`.",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the on-disk schema differs from the Pydantic models.",
    )
    args = parser.parse_args()

    schema = build_schema()
    if args.check:
        return check(schema)
    write(schema)
    print(f"Wrote {SCHEMA_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
