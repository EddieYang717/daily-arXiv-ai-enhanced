"""Commit only validated day bundles and checkpoints in a separate data checkout."""
import argparse
import json
from pathlib import Path
import re
import subprocess

from .storage import read_jsonl, reindex, successful_ai, write_json
from to_md.convert import render


def publication_paths(root, report, language):
    root = Path(root)
    paths = []
    if report.get('dry_run'):
        return paths
    if report.get('publication_ready'):
        for day, info in report.get('days', {}).items():
            if info.get('status') != 'prepared':
                continue
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
                raise ValueError('Invalid publication date')
            raw_path = f'data/{day}.jsonl'
            enhanced_path = f'data/{day}_AI_enhanced_{language}.jsonl'
            md_path = f'data/{day}.md'
            raw, enhanced = read_jsonl(root / raw_path), read_jsonl(root / enhanced_path)
            expected = set(info['expected_ids'])
            if (len(raw) != len(expected) or len(enhanced) != len(expected)
                    or {i['id'] for i in raw} != expected or {i['id'] for i in enhanced} != expected
                    or not all(successful_ai(i, historical=True) for i in enhanced)):
                raise ValueError(f'{day}: publication bundle incomplete')
            markdown, rendered = render(enhanced)
            if rendered != expected or (root / md_path).read_text(encoding='utf-8') != markdown:
                raise ValueError(f'{day}: Markdown differs from validated results')
            paths.extend([raw_path, enhanced_path, md_path])
        names = reindex(root, dry_run=True)
        if (root / 'assets/file-list.txt').read_text().splitlines() != names:
            raise ValueError('Historical file index is incomplete')
        paths.append('assets/file-list.txt')
    if (root / 'state/crawl.json').exists():
        paths.append('state/crawl.json')
    return paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--language', default='Chinese')
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    if report.get('dry_run'):
        print('Dry run: no files committed')
        return
    paths = publication_paths(args.data_root, report, args.language)
    if not paths:
        print('No validated artifacts or checkpoints to commit')
        return
    # A persistent per-ID result makes recovery progress inspectable on the data branch.
    if report.get('reconciliation'):
        write_json(args.data_root / 'state/last-backfill.json', report['reconciliation'])
        paths.append('state/last-backfill.json')
    def git(*arguments, **kwargs):
        return subprocess.run(['git', '-C', str(args.data_root), *arguments], check=True, **kwargs)
    git('add', '--', *paths)
    changed = subprocess.run(['git', '-C', str(args.data_root), 'diff', '--cached', '--quiet']).returncode
    if changed not in (0, 1):
        raise RuntimeError('Cannot inspect staged changes')
    if changed:
        git('commit', '-m', f"update: validated arXiv {report.get('mode', 'run')}")
        # Non-fast-forward is a failure. Never rebase or force over someone else's data.
        git('push', 'origin', 'HEAD:data')
    print('Data branch publication/checkpoint persistence completed')


if __name__ == '__main__':
    main()
