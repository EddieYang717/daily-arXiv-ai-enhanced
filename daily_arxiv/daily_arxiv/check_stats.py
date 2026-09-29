#!/usr/bin/env python3
"""Compatibility gate: consume a verified runner report, never delete daily data."""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, default=Path(__file__).resolve().parents[2] / 'run-artifacts/report.json')
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding='utf-8'))
        if not report.get('ok'):
            raise ValueError('The shared runner did not verify a complete crawl')
        if report.get('dry_run'):
            raise ValueError('A dry run does not authorize AI or publication')
        states = [day.get('status') for day in report.get('days', {}).values()]
        return 0 if 'prepared' in states else 1
    except (OSError, ValueError) as exc:
        print(f'{exc}. Run python -m arxiv_daily.runner with the data branch loaded first.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
