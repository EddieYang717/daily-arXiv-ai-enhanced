"""Shared CLI for local runs and Actions; publication happens only after validation."""
import argparse
import copy
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile

from .client import ArxivClient, FetchError
from .metadata import InvalidMetadata, paper_id, parse_abstract, parse_listing, validate_metadata
from .storage import (atomic_text, cache_key, load_history, read_jsonl, reindex,
                      successful_ai, write_json, write_jsonl)
from to_md.convert import render

REPO = Path(__file__).resolve().parents[1]


def utc_date():
    return datetime.now(timezone.utc).date().isoformat()


def ai_context(model, language):
    from ai.enhance import load_research_profile, system, template
    return dict(model=model, language=language, profile=load_research_profile(),
                system=system, template=template, schema=(REPO / 'ai/structure.py').read_text(),
                sensitive_check=os.environ.get('ENABLE_SENSITIVE_CHECK', ''),
                provider=os.environ.get('OPENAI_BASE_URL', '').split('?')[0])


def enhance(items, model, language, callback):
    from ai.enhance import process_all_items
    if not os.environ.get('OPENAI_API_KEY'):
        raise ValueError('OPENAI_API_KEY is not configured')
    process_all_items(items, model, language, 1, on_result=callback)


class Runner:
    def __init__(self, root, report_dir, language='Chinese', model='deepseek-chat',
                 targets=('cs.IT', 'eess.SP'), dry_run=False, client=None,
                 enhancer=enhance, context=None):
        self.root, self.report_dir = Path(root), Path(report_dir)
        self.language, self.model, self.targets = language, model, set(targets)
        self.dry_run, self.enhancer = dry_run, enhancer
        self.context = context
        self.state_path = self.root / 'state/crawl.json'
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {'version': 1, 'days': {}, 'transport': {}}
        if self.state.get('version') != 1:
            raise ValueError('Unsupported crawl state version')
        self.dates, self.published = load_history(root, language)
        self.original_published = dict(self.published)
        self.report = {'mode': None, 'dry_run': dry_run, 'days': {}, 'reconciliation': [], 'errors': []}
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.client = client or ArxivClient(self.state['transport'], self.save, self.log_request)

    def save(self):
        if not self.dry_run:
            write_json(self.state_path, self.state)

    def log_request(self, event):
        with (self.report_dir / 'requests.jsonl').open('a', encoding='utf-8') as file:
            file.write(json.dumps(event, ensure_ascii=False) + '\n')

    def day(self, day):
        date.fromisoformat(day)
        return self.state['days'].setdefault(day, {'candidates': [], 'metadata': {}, 'ai': {},
            'errors': {}, 'sources': {}, 'listing_complete': False, 'status': 'pending',
            'targets': sorted(self.targets), 'language': self.language})

    def collect_daily(self, day):
        state = self.day(day)
        if state['targets'] != sorted(self.targets) or state['language'] != self.language:
            raise ValueError('Cannot change categories/language for an existing daily checkpoint')
        state['listing_complete'] = False
        state['status'] = 'pending'
        state['sources'] = {}
        self.save()
        for category in sorted(self.targets):
            url = f'https://arxiv.org/list/{category}/new'
            visited, all_ids, announcements, totals = set(), set(), set(), set()
            try:
                while url:
                    if url in visited or len(visited) >= 100:
                        raise InvalidMetadata('Listing pagination loop or excessive pages')
                    visited.add(url)
                    html = self.client.get(url)
                    atomic_text(self.report_dir / f'{day}-{category}-{len(visited)}.html', html)
                    page = parse_listing(html, url, self.targets)
                    if all_ids.intersection(page.all_ids):
                        raise InvalidMetadata('Duplicate IDs across listing pages')
                    all_ids.update(page.all_ids)
                    announcements.add(page.announcement)
                    totals.add(page.total)
                    state['candidates'] = sorted(set(state['candidates']) | set(page.candidates))
                    state['metadata'].update(page.items)
                    # A rolled-over /new page can still contain an older pending day's
                    # papers. Reuse the metadata without moving their publication date.
                    for previous_day, previous in self.state['days'].items():
                        if previous_day < day:
                            for identifier in set(previous['candidates']) & page.items.keys():
                                previous['metadata'][identifier] = page.items[identifier]
                                previous['errors'].pop(identifier, None)
                    state['errors'].update(page.errors)
                    for identifier in page.items:
                        state['errors'].pop(identifier, None)
                    self.save()
                    url = page.next_url
                if len(announcements) != 1 or len(totals) != 1 or len(all_ids) != next(iter(totals)):
                    raise InvalidMetadata('Listing pagination is incomplete or changed during crawl')
                announcement = next(iter(announcements))
                if not 0 <= (date.fromisoformat(day) - date.fromisoformat(announcement)).days <= 4:
                    raise InvalidMetadata('Listing announcement date is stale or in the future')
                state['sources'][category] = {'announcement': announcement, 'total': len(all_ids), 'complete': True}
            except (FetchError, ValueError) as exc:
                state['sources'][category] = {'complete': False, 'error': str(exc)}
            self.save()
        sources = list(state['sources'].values())
        state['listing_complete'] = (len(sources) == len(self.targets) and all(s.get('complete') for s in sources)
            and len({s.get('announcement') for s in sources}) == 1)
        if not state['listing_complete']:
            state['errors']['_listing'] = 'Missing, inconsistent or unverified listing pages; candidate set is incomplete'
        else:
            state['errors'].pop('_listing', None)
        self.save()

    def seed_backfill(self, manifest):
        entries = json.loads(Path(manifest).read_text(encoding='utf-8'))
        papers = entries['papers']
        if len({paper_id(p['id']) for p in papers}) != entries['candidate_count']:
            raise ValueError('Backfill manifest count mismatch')
        days = set()
        for paper in papers:
            identifier = paper_id(paper['id'])
            day = min(paper['failed_dates'])
            state = self.day(day)
            if state['sources'] and not state['listing_complete']:
                raise ValueError(f'{day}: a listing gap cannot be cleared by a partial ID manifest')
            state['scope'] = 'explicit-id-manifest'
            state['listing_complete'] = True
            state['candidates'] = sorted(set(state['candidates']) | {identifier})
            days.add(day)
        self.save()
        return sorted(days), papers

    def process_day(self, day):
        state = self.day(day)
        if state['language'] != self.language:
            raise ValueError(f'{day}: pending language differs from current language')
        existing = self.dates.get(day, {})
        # Existing successful records on this date are always retained verbatim.
        retained = {i: row for i, row in existing.items() if successful_ai(row, historical=True)}
        expected = set(state['candidates']) | set(existing)
        reserved = {}
        for previous_day, previous in sorted(self.state['days'].items()):
            if previous_day < day:
                for identifier in set(previous['candidates']) & expected - existing.keys():
                    if identifier not in self.published:
                        reserved.setdefault(identifier, previous_day)
        expected -= reserved.keys()
        skipped = {i for i in expected if i not in existing and i in self.published and self.published[i] != day}
        expected -= skipped
        work = expected - retained.keys()
        for identifier in sorted(work):
            if identifier in state['metadata']:
                validate_metadata(state['metadata'][identifier])
                continue
            try:
                item = parse_abstract(self.client.get(f'https://arxiv.org/abs/{identifier}'), identifier)
                # Unknown subjects in a daily listing must be checked after recovery.
                if state.get('scope') != 'explicit-id-manifest' and item['categories'][0] not in state['targets']:
                    expected.remove(identifier)
                    state['candidates'] = [i for i in state['candidates'] if i != identifier]
                    state['errors'].pop(identifier, None)
                else:
                    state['metadata'][identifier] = item
                    state['errors'].pop(identifier, None)
            except (FetchError, ValueError) as exc:
                state['errors'][identifier] = str(exc)
            self.save()
        missing = expected - retained.keys() - state['metadata'].keys()
        info = {'candidates': len(state['candidates']), 'already_published': sorted(skipped),
                'reserved_for_earlier_day': reserved,
                'expected_ids': sorted(expected), 'missing_metadata': sorted(missing),
                'listing_complete': state['listing_complete']}
        self.report['days'][day] = info
        if self.dry_run:
            info['status'] = 'ready_for_ai' if state['listing_complete'] and not missing else 'incomplete'
            return info['status'] != 'incomplete'
        # Collect metadata even for a listing gap, but never spend AI/publish an unknown set.
        if missing or not state['listing_complete']:
            info['status'] = state['status'] = 'incomplete'
            self.save()
            return False
        if not expected:
            info['status'] = state['status'] = 'no_new_content'
            self.save()
            return True
        if self.context is None:
            self.context = ai_context(self.model, self.language)
        results = dict(retained)
        pending = []
        keys = {}
        for identifier in sorted(expected - retained.keys()):
            item = state['metadata'][identifier]
            key = cache_key(item, self.context)
            keys[identifier] = key
            cache = state['ai'].get(identifier, {})
            if cache.get('key') == key and successful_ai(cache.get('item')):
                results[identifier] = cache['item']
            else:
                pending.append(copy.deepcopy(item))

        def checkpoint(identifier, item):
            if identifier not in keys or not item or item.get('id') != identifier or not successful_ai(item):
                state['errors'][identifier] = 'AI output incomplete or contains fallback placeholders'
            else:
                validate_metadata(item)
                if cache_key(item, self.context) != keys[identifier]:
                    raise ValueError('AI changed source metadata')
                state['ai'][identifier] = {'key': keys[identifier], 'item': item}
                state['errors'].pop(identifier, None)
                results[identifier] = item
            self.save()

        if pending:
            try:
                self.enhancer(pending, self.model, self.language, checkpoint)
            except Exception as exc:
                state['errors']['_ai'] = f'AI processing failed: {type(exc).__name__}'
                self.report['errors'].append(f'{day}: AI processing failed: {type(exc).__name__}')
        if set(results) != expected:
            info['status'] = state['status'] = 'incomplete'
            info['missing_ai'] = sorted(expected - results.keys())
            self.save()
            return False
        state['errors'].pop('_ai', None)
        self.stage_day(day, results, expected)
        info['status'] = state['status'] = 'prepared'
        info['papers'] = len(results)
        self.dates[day] = results
        self.published.update({i: day for i in results})
        self.save()
        return True

    def stage_day(self, day, results, expected):
        items = [results[i] for i in sorted(results)]
        markdown, rendered = render(items)
        if rendered != expected:
            raise ValueError('Markdown IDs differ from expected IDs')
        # No public file changes until all three artifacts have been generated/validated.
        with tempfile.TemporaryDirectory(prefix='arxiv-stage-') as temp:
            staging = Path(temp)
            raw = [{k: v for k, v in item.items() if k != 'AI'} for item in items]
            raw_name, ai_name = f'{day}.jsonl', f'{day}_AI_enhanced_{self.language}.jsonl'
            write_jsonl(staging / raw_name, raw)
            write_jsonl(staging / ai_name, items)
            atomic_text(staging / f'{day}.md', markdown)
            if {i['id'] for i in read_jsonl(staging / raw_name)} != expected or {i['id'] for i in read_jsonl(staging / ai_name)} != expected:
                raise ValueError('Staged output IDs differ from expected IDs')
            for name in (raw_name, ai_name, f'{day}.md'):
                atomic_text(self.root / 'data' / name, (staging / name).read_text(encoding='utf-8'))

    def run(self, mode, day=None, manifest=None):
        self.report['mode'] = mode
        ok, papers = True, []
        try:
            if mode == 'reindex':
                self.report['indexed_files'] = len(reindex(self.root, self.dry_run))
                self.report['publication_ready'] = not self.dry_run
                return True
            if mode == 'daily':
                if day != utc_date():
                    raise ValueError('daily only accepts the current UTC date; use backfill for past IDs')
                if manifest:
                    _, papers = self.seed_backfill(manifest)
                self.collect_daily(day)
                days = sorted(d for d, state in self.state['days'].items()
                    if d != day and (not state['listing_complete'] or
                        any(i not in self.published for i in state['candidates']))) + [day]
            else:
                days, papers = self.seed_backfill(manifest)
            for target in days:
                ok = self.process_day(target) and ok
            if not self.dry_run:
                self.report['indexed_files'] = len(reindex(self.root))
                self.report['publication_ready'] = True
            return ok
        except (Exception, KeyboardInterrupt) as exc:
            ok = False
            self.report['errors'].append(f'{type(exc).__name__}: {exc}')
            return False
        finally:
            # Include every supplied ID even when an unexpected processing error
            # interrupts a day. Uncommittable local output is still pending.
            for paper in papers:
                identifier = paper_id(paper['id'])
                original = self.original_published.get(identifier)
                complete = self.report.get('publication_ready') and identifier in self.published
                result = 'already_published' if original else ('recovered' if complete else 'pending')
                self.report['reconciliation'].append({'id': identifier, 'status': result,
                    'date': self.published.get(identifier, min(paper['failed_dates']))})
            self.save()
            self.report['ok'] = ok
            write_json(self.report_dir / 'report.json', self.report)

def main(argv=None):
    def interrupted(signum, frame):
        raise InterruptedError('Run interrupted; saved checkpoints remain resumable')
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['daily', 'backfill', 'reindex'])
    parser.add_argument('--data-root', type=Path, required=True, help='Separate checkout of the data branch')
    parser.add_argument('--report-dir', type=Path, default=Path('run-artifacts'))
    parser.add_argument('--date', default=utc_date())
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    language = os.environ.get('LANGUAGE') or 'Chinese'
    if not re.fullmatch('[A-Za-z0-9-]+', language):
        parser.error('LANGUAGE must be a filename-safe language name')
    categories = [c.strip() for c in (os.environ.get('CATEGORIES') or 'cs.IT,eess.SP').split(',') if c.strip()]
    if not categories or any(not re.fullmatch(r'[A-Za-z0-9.-]+', c) for c in categories):
        parser.error('Invalid categories')
    if args.mode == 'backfill' and not args.manifest:
        parser.error('backfill requires --manifest')
    try:
        runner = Runner(args.data_root, args.report_dir, language, os.environ.get('MODEL_NAME') or 'deepseek-chat', categories, args.dry_run)
        runner.original_published = dict(runner.published)
        ok = runner.run(args.mode, args.date, args.manifest)
        print(json.dumps(runner.report, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    except Exception as exc:
        write_json(args.report_dir / 'report.json', {'ok': False, 'errors': [f'{type(exc).__name__}: {exc}']})
        print(f'Run failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
