"""Strict input validation and publication staging."""
import hashlib
import json
import os
from pathlib import Path
import re

from .metadata import paper_id, validate_metadata

AI_FIELDS = ("tldr", "motivation", "method", "result", "conclusion")
PLACEHOLDERS = {"Summary generation failed", "Processing failed", "Motivation analysis unavailable",
                "Method extraction failed", "Result analysis unavailable", "Conclusion extraction failed",
                "Relevance scoring unavailable"}
DAILY_FILE = re.compile(r"\d{4}-\d{2}-\d{2}(?:_AI_enhanced_[A-Za-z0-9-]+)?\.jsonl")


def successful_ai(item, historical=False):
    if not isinstance(item, dict) or not isinstance(item.get("AI"), dict):
        return False
    ai = item["AI"]
    fields = AI_FIELDS if historical else (*AI_FIELDS, "relevance_reason")
    if any(not isinstance(ai.get(f), str) or not ai[f].strip() or ai[f] in PLACEHOLDERS for f in fields):
        return False
    if historical:
        return True  # Older published records predate relevance scoring.
    return (type(ai.get("relevance_score")) is int and 0 <= ai["relevance_score"] <= 5
            and isinstance(ai.get("relevance_topics"), list)
            and all(isinstance(topic, str) for topic in ai["relevance_topics"]))


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    temp.replace(path)


def write_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def read_jsonl(path):
    rows = []
    with Path(path).open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                validate_metadata(item)
                item["id"] = paper_id(item["id"])
                rows.append(item)
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
    return rows


def write_jsonl(path, items):
    atomic_text(path, "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items))


def load_history(root, language):
    data = Path(root) / "data"
    if not data.is_dir():
        raise ValueError("Missing historical data directory; load the data branch first")
    dates, published = {}, {}
    for path in sorted(data.glob(f"????-??-??_AI_enhanced_{language}.jsonl")):
        rows = read_jsonl(path)  # Corrupt history is fatal, never silently empty.
        day = path.name[:10]
        dates[day] = {row["id"]: row for row in rows}
        for row in rows:
            if successful_ai(row, historical=True):
                published.setdefault(row["id"], day)
    return dates, published


def reindex(root, dry_run=False):
    root = Path(root)
    names = sorted(p.name for p in (root / "data").iterdir() if DAILY_FILE.fullmatch(p.name))
    for name in names:
        read_jsonl(root / "data" / name)
    if not dry_run:
        atomic_text(root / "assets/file-list.txt", "".join(name + "\n" for name in names))
    return names


def cache_key(item, context):
    metadata = {key: item.get(key) for key in ("id", "title", "authors", "summary", "categories", "comment", "abs", "pdf")}
    return hashlib.sha256(json.dumps([metadata, context], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
