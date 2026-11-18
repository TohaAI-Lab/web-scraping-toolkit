"""UTF-8 CSV/JSON output, with atomic replacement of the target file."""
import csv
import json
import os
import tempfile
from pathlib import Path


def safe_cell(value: str) -> str:
    # Mitigate common spreadsheet formula injection, including leading whitespace.
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value


def export(rows: list[dict], fields: list[str], output: Path, fmt: str) -> None:
    output = Path(output)
    if fmt not in ("csv", "json"):
        raise ValueError("Unsupported output format")
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=output.parent, delete=False) as stream:
            tmp = Path(stream.name)
            if fmt == "json":
                json.dump(rows, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            else:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows({k: safe_cell(v) for k, v in row.items()} for row in rows)
        os.replace(tmp, output)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
