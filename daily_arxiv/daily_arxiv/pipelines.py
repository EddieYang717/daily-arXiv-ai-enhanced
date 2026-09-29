"""Metadata validation only; no hidden per-item HTTP requests."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from arxiv_daily.metadata import validate_metadata


class DailyArxivPipeline:
    def process_item(self, item, spider):
        return validate_metadata(item)
