import copy
from datetime import datetime, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from arxiv_daily.client import ArxivClient, Cooldown, FetchError, retry_after
from arxiv_daily.metadata import InvalidMetadata, make_item, parse_abstract, parse_listing
from arxiv_daily.publish import publication_paths
from arxiv_daily.runner import Runner
from arxiv_daily.storage import cache_key, load_history, read_jsonl, reindex, successful_ai, write_jsonl
from to_md.convert import render

FIXTURES = Path(__file__).parent / 'fixtures'
TARGETS = {'cs.IT', 'eess.SP'}


def paper(identifier='2609.00001'):
    return make_item(identifier, 'Title with $x_i$', ['First Author', 'Second Author'],
                     'An abstract about wireless communication.', ['cs.IT', 'eess.SP'])


def enriched(item):
    result = copy.deepcopy(item)
    result['AI'] = {field: '有效分析' for field in ('tldr', 'motivation', 'method', 'result', 'conclusion', 'relevance_reason')}
    result['AI'].update(relevance_score=3, relevance_topics=['CSI'])
    return result


def listing(identifier='2609.00001', heading='New submissions', count=1, total=1, next_url=''):
    row = f'''<dt><a title="Abstract" href="/abs/{identifier}">id</a></dt><dd><div class="meta">
      <div class="list-title"><span class="descriptor">Title:</span> Inline <i>math</i> $x_i$</div>
      <div class="list-authors"><a>Author, Jr.</a><a>Another Author</a></div>
      <div class="list-subjects"><span class="primary-subject">Information Theory (cs.IT)</span>; Signal Processing (eess.SP)</div>
      <p class="mathjax">The abstract has $x_i$ and <i>inline</i> text.</p></div></dd>'''
    return f'''<div id="dlpage"><h3>Showing new listings for Monday, 28 September 2026</h3>
      <div>Total of {total} entries</div><dl><h3>{heading} (showing {count} of {total} entries)</h3>
      {row if count else ''}</dl>{f'<a href="{next_url}">Next</a>' if next_url else ''}</div>'''


class ParserTests(unittest.TestCase):
    def test_real_september28_has_29_unique_candidates(self):
        items = {}
        for category in TARGETS:
            page = parse_listing((FIXTURES / f'{category}-2026-09-28.html').read_text(),
                                 f'https://arxiv.org/list/{category}/new', TARGETS)
            self.assertEqual(page.announcement, '2026-09-28')
            self.assertFalse(page.errors)
            self.assertEqual(len(page.all_ids), page.total)
            items.update(page.items)
        self.assertEqual(len(items), 29)
        self.assertIn('2609.30690', items)
        self.assertNotIn('Title:', items['2609.30653']['title'])

    def test_abstract_matches_listing_and_preserves_author_names(self):
        item = parse_abstract((FIXTURES / '2609.30653.html').read_text(), '2609.30653')
        self.assertEqual(item['authors'][0], 'Yi Geng')
        self.assertNotIn('Abstract:', item['summary'])
        with self.assertRaises(InvalidMetadata):
            parse_abstract((FIXTURES / '2609.30653.html').read_text(), '2609.00001')

    def test_optional_comments_math_authors_and_no_replacement_nav(self):
        page = parse_listing(listing(), 'https://arxiv.org/list/cs.IT/new', TARGETS)
        item = page.items['2609.00001']
        self.assertIsNone(item['comment'])
        self.assertIn('$x_i$', item['summary'])
        self.assertEqual(item['authors'], ['Author, Jr.', 'Another Author'])
        self.assertEqual(item['categories'], ['cs.IT', 'eess.SP'])
        replacement = parse_listing(listing(heading='Replacement submissions'), 'https://arxiv.org/list/cs.IT/new', TARGETS)
        self.assertFalse(replacement.candidates)

    def test_missing_abstract_is_recoverable_but_not_successful(self):
        html = listing().replace('<p class="mathjax">', '<p>')
        page = parse_listing(html, 'https://arxiv.org/list/cs.IT/new', TARGETS)
        self.assertEqual(page.candidates, ['2609.00001'])
        self.assertIn('2609.00001', page.errors)
        self.assertFalse(page.items)

    def test_invalid_structure_count_and_true_zero(self):
        for html in ('<html>Service unavailable</html>', listing().replace('showing 1 of', 'showing 2 of')):
            with self.assertRaises(InvalidMetadata):
                parse_listing(html, 'https://arxiv.org/list/cs.IT/new', TARGETS)
        page = parse_listing(listing(count=0, total=0), 'https://arxiv.org/list/cs.IT/new', TARGETS)
        self.assertFalse(page.candidates)
        self.assertEqual(page.total, 0)

    def test_primary_category_filter_is_preserved(self):
        html = listing().replace('Information Theory (cs.IT)', 'Machine Learning (cs.LG)')
        page = parse_listing(html, 'https://arxiv.org/list/cs.IT/new', TARGETS)
        self.assertFalse(page.candidates)


class Clock:
    def __init__(self):
        self.now = 10000
    def time(self):
        return self.now
    def sleep(self, value):
        self.now += value


class Response:
    def __init__(self, status=200, text='ok', headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


class Session:
    def __init__(self, clock, responses):
        self.headers, self.responses, self.requests = {}, iter(responses), []
        self.clock = clock
    def get(self, url, **kwargs):
        self.requests.append((url, self.clock.time(), kwargs))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class TransportTests(unittest.TestCase):
    def client(self, responses, state=None):
        clock = Clock()
        session = Session(clock, responses)
        client = ArxivClient(state=state, session=session, clock=clock.time, sleep=clock.sleep)
        return client, clock, session

    def test_robots_delay_and_serial_requests(self):
        client, _, session = self.client([Response(text='User-agent: *\nCrawl-delay: 15\nAllow: /abs\n'), Response(), Response()])
        client.get('https://arxiv.org/abs/2609.00001')
        client.get('https://arxiv.org/abs/2609.00002')
        times = [r[1] for r in session.requests]
        self.assertEqual(times, [10000, 10015, 10030])
        self.assertTrue(all(r[2]['timeout'] == 30 and not r[2]['allow_redirects'] for r in session.requests))

    def test_robots_denial_and_non_arxiv_redirect_target(self):
        client, _, session = self.client([Response(text='User-agent: *\nDisallow: /abs\n')])
        with self.assertRaises(FetchError):
            client.get('https://arxiv.org/abs/2609.00001')
        self.assertEqual(len(session.requests), 1)
        with self.assertRaises(FetchError):
            client.get('https://example.com/abs/2609.00001')

    def test_three_rejections_stop_all_later_requests(self):
        state = {}
        client, clock, session = self.client([Response(406), Response(429), Response(406)], state)
        with self.assertRaises(Cooldown):
            client._request('https://arxiv.org/list/cs.IT/new')
        self.assertEqual([r[1] for r in session.requests], [10000, 10030, 10090])
        self.assertGreaterEqual(state['next_allowed_at'], clock.time() + 900)
        with self.assertRaises(Cooldown):
            client._request('https://arxiv.org/abs/2609.00001')
        self.assertEqual(len(session.requests), 3)

    def test_retry_after_seconds_date_and_persisted_long_cooldown(self):
        client, _, session = self.client([Response(429, headers={'Retry-After': '90'}), Response()])
        client._request('https://arxiv.org/list/cs.IT/new')
        self.assertEqual(session.requests[1][1] - session.requests[0][1], 90)
        when = format_datetime(datetime.fromtimestamp(10200, timezone.utc), usegmt=True)
        self.assertEqual(retry_after(when, 10000), 200)
        state = {}
        client, _, session = self.client([Response(429, headers={'Retry-After': '7200'})], state)
        with self.assertRaises(Cooldown):
            client._request('https://arxiv.org/list/cs.IT/new')
        self.assertEqual(len(session.requests), 1)
        again, _, second = self.client([], state)
        with self.assertRaises(Cooldown):
            again._request('https://arxiv.org/list/eess.SP/new')
        self.assertFalse(second.requests)

    def test_timeout_backoff_and_non200_atom_never_success(self):
        client, _, session = self.client([requests.Timeout(), Response(503), Response()])
        self.assertEqual(client._request('https://arxiv.org/list/cs.IT/new'), 'ok')
        self.assertEqual([r[1] for r in session.requests], [10000, 10030, 10090])
        client, _, _ = self.client([Response(406, '<feed/>')] * 3)
        with self.assertRaises(Cooldown):
            client._request('https://arxiv.org/list/cs.IT/new')


class FakeClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []
    def get(self, url):
        self.calls.append(url)
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'historical_data'
        (self.root / 'data').mkdir(parents=True)
        self.reports = Path(self.temp.name) / 'reports'
    def tearDown(self):
        self.temp.cleanup()
    def runner(self, **kwargs):
        return Runner(self.root, self.reports, client=kwargs.pop('client', FakeClient()), context={'model': 'test'}, **kwargs)
    def seed(self, runner, day='2026-09-28', items=None):
        items = items or [paper()]
        state = runner.day(day)
        state['listing_complete'] = True
        state['candidates'] = [i['id'] for i in items]
        state['metadata'] = {i['id']: i for i in items}
        runner.save()
        return state

    def test_ai_failure_preserves_day_and_resume_reuses_success(self):
        day = '2026-09-28'
        existing = enriched(paper('2609.00000'))
        path = self.root / f'data/{day}_AI_enhanced_Chinese.jsonl'
        write_jsonl(path, [existing])
        original = path.read_bytes()
        calls = []
        def partial(items, model, language, callback):
            for item in items:
                calls.append(item['id'])
                callback(item['id'], enriched(item) if item['id'].endswith('1') else None)
        runner = self.runner(enhancer=partial)
        self.seed(runner, items=[paper(), paper('2609.00002')])
        self.assertFalse(runner.process_day(day))
        self.assertEqual(path.read_bytes(), original)
        def success(items, model, language, callback):
            for item in items:
                calls.append(item['id'])
                callback(item['id'], enriched(item))
        resumed = self.runner(enhancer=success)
        self.assertTrue(resumed.process_day(day))
        self.assertEqual(calls, ['2609.00001', '2609.00002', '2609.00002'])
        rows = {i['id']: i for i in read_jsonl(path)}
        self.assertEqual(set(rows), {'2609.00000', '2609.00001', '2609.00002'})
        self.assertEqual(rows['2609.00000'], existing)
        self.assertEqual({i['id'] for i in read_jsonl(self.root / f'data/{day}.jsonl')}, set(rows))

    def test_history_corruption_is_fatal_and_failed_ai_not_deduped(self):
        path = self.root / 'data/2026-09-23_AI_enhanced_Chinese.jsonl'
        path.write_text('{broken\n')
        with self.assertRaises(ValueError):
            load_history(self.root, 'Chinese')
        row = enriched(paper())
        row['AI']['method'] = 'Method extraction failed'
        write_jsonl(path, [row])
        _, published = load_history(self.root, 'Chinese')
        self.assertFalse(published)

    def test_only_historical_success_is_skipped(self):
        write_jsonl(self.root / 'data/2026-09-23_AI_enhanced_Chinese.jsonl', [enriched(paper())])
        runner = self.runner(enhancer=lambda *args: self.fail('AI must not be called'))
        self.seed(runner)
        self.assertTrue(runner.process_day('2026-09-28'))
        self.assertEqual(runner.report['days']['2026-09-28']['status'], 'no_new_content')
        self.assertFalse((self.root / 'data/2026-09-28.jsonl').exists())

    def test_dry_run_never_calls_ai_or_changes_data_or_state(self):
        runner = self.runner(dry_run=True, enhancer=lambda *args: self.fail('AI must not be called'))
        self.seed(runner)
        self.assertTrue(runner.process_day('2026-09-28'))
        self.assertFalse(list((self.root / 'data').iterdir()))
        self.assertFalse((self.root / 'state').exists())

    def test_missing_list_page_blocks_publication(self):
        runner = self.runner(client=FakeClient({
            'https://arxiv.org/list/cs.IT/new': listing(),
            'https://arxiv.org/list/eess.SP/new': FetchError('HTTP 406')}))
        runner.collect_daily('2026-09-28')
        self.assertFalse(runner.process_day('2026-09-28'))
        self.assertEqual(runner.day('2026-09-28')['candidates'], ['2609.00001'])
        self.assertFalse(list((self.root / 'data').iterdir()))

    def test_pagination_complete_and_missing_next_detected(self):
        first_url = 'https://arxiv.org/list/cs.IT/new'
        next_url = first_url + '?skip=1&show=1'
        runner = Runner(self.root, self.reports, targets=['cs.IT'], dry_run=True,
            client=FakeClient({first_url: listing(total=2, next_url=next_url), next_url: listing('2609.00002', total=2)}))
        runner.collect_daily('2026-09-28')
        self.assertTrue(runner.day('2026-09-28')['listing_complete'])
        self.assertEqual(len(runner.day('2026-09-28')['candidates']), 2)
        runner.client = FakeClient({first_url: listing(total=2)})
        runner.collect_daily('2026-09-28')
        self.assertFalse(runner.day('2026-09-28')['listing_complete'])

    def test_different_announcement_dates_block_publication(self):
        runner = self.runner(client=FakeClient({
            'https://arxiv.org/list/cs.IT/new': listing(),
            'https://arxiv.org/list/eess.SP/new': listing().replace('Monday, 28 September', 'Sunday, 27 September')}))
        runner.collect_daily('2026-09-28')
        self.assertFalse(runner.day('2026-09-28')['listing_complete'])

    def test_92_manifest_count_dates_and_reconciliation(self):
        manifest = Path(__file__).parents[1] / 'recovery/2026-09-24-28.json'
        runner = self.runner(dry_run=True)
        days, entries = runner.seed_backfill(manifest)
        self.assertEqual(days, ['2026-09-24', '2026-09-25', '2026-09-28'])
        self.assertEqual([len(runner.day(d)['candidates']) for d in days], [39, 25, 28])
        self.assertEqual(len(entries), 92)

    def test_backfill_id_failure_returns_nonzero_and_accounts_for_every_id(self):
        manifest = Path(self.temp.name) / 'manifest.json'
        manifest.write_text(json.dumps({'candidate_count': 1, 'papers': [{'id': '2609.00001', 'failed_dates': ['2026-09-24', '2026-09-25']}]}))
        runner = self.runner(client=FakeClient({'https://arxiv.org/abs/2609.00001': FetchError('HTTP 406')}))
        self.assertFalse(runner.run('backfill', manifest=manifest))
        report = json.loads((self.reports / 'report.json').read_text())
        self.assertEqual(report['reconciliation'], [{'id': '2609.00001', 'status': 'pending', 'date': '2026-09-24'}])
        self.assertFalse(report['ok'])
        self.assertTrue((self.root / 'state/crawl.json').exists())

    def test_index_retains_all_dates_and_excludes_state(self):
        for day in ('2026-09-23', '2026-09-28'):
            write_jsonl(self.root / f'data/{day}.jsonl', [paper()])
            write_jsonl(self.root / f'data/{day}_AI_enhanced_Chinese.jsonl', [enriched(paper())])
        (self.root / 'data/checkpoint.jsonl').write_text('not json')
        names = reindex(self.root)
        self.assertEqual(len(names), 4)
        self.assertIn('2026-09-23_AI_enhanced_Chinese.jsonl', names)
        self.assertEqual((self.root / 'assets/file-list.txt').read_text().splitlines(), names)

    def test_publish_revalidates_bundle_and_only_lists_explicit_paths(self):
        runner = self.runner(enhancer=lambda rows, m, l, callback: [callback(i['id'], enriched(i)) for i in rows])
        self.seed(runner)
        runner.process_day('2026-09-28')
        reindex(self.root)
        runner.report['publication_ready'] = True
        paths = publication_paths(self.root, runner.report, 'Chinese')
        self.assertEqual(set(paths), {'data/2026-09-28.jsonl', 'data/2026-09-28_AI_enhanced_Chinese.jsonl',
            'data/2026-09-28.md', 'assets/file-list.txt', 'state/crawl.json'})
        (self.root / 'data/2026-09-28.md').write_text('truncated')
        with self.assertRaises(ValueError):
            publication_paths(self.root, runner.report, 'Chinese')

    def test_cache_context_change_invalidates_saved_ai(self):
        a = cache_key(paper(), {'model': 'a', 'profile': 'old'})
        for context in ({'model': 'b', 'profile': 'old'}, {'model': 'a', 'profile': 'new'}):
            self.assertNotEqual(a, cache_key(paper(), context))
        self.assertNotEqual(a, cache_key({**paper(), 'summary': 'changed'}, {'model': 'a', 'profile': 'old'}))

    def test_markdown_and_ai_validator_reject_partial_fallback(self):
        item = enriched(paper())
        item['AI']['method'] = 'Method extraction failed'
        self.assertFalse(successful_ai(item))
        with self.assertRaises(ValueError):
            render([item])

    def test_rolled_over_papers_keep_their_earliest_pending_date(self):
        runner = self.runner(enhancer=lambda rows, m, l, callback: [callback(i['id'], enriched(i)) for i in rows])
        self.seed(runner, day='2026-09-25')
        self.seed(runner, day='2026-09-28')
        # Even if the later date is processed first, the old date owns the paper.
        self.assertTrue(runner.process_day('2026-09-28'))
        self.assertFalse((self.root / 'data/2026-09-28.jsonl').exists())
        self.assertEqual(runner.report['days']['2026-09-28']['reserved_for_earlier_day'], {'2609.00001': '2026-09-25'})
        self.assertTrue(runner.process_day('2026-09-25'))
        self.assertTrue(runner.process_day('2026-09-28'))
        self.assertFalse((self.root / 'data/2026-09-28.jsonl').exists())

    def test_pipeline_never_fetches_details(self):
        from daily_arxiv.daily_arxiv.pipelines import DailyArxivPipeline
        with patch('requests.Session.request', side_effect=AssertionError('Unexpected network call')):
            self.assertEqual(DailyArxivPipeline().process_item(paper(), None), paper())

    def test_existing_ai_cli_output_survives_all_none_or_partial_results(self):
        from ai import enhance as ai
        raw = self.root / 'data/2026-09-28.jsonl'
        output = self.root / 'data/2026-09-28_AI_enhanced_Chinese.jsonl'
        write_jsonl(raw, [paper(), paper('2609.00002')])
        output.write_text('existing published content')
        for result in ([None, None], [enriched(paper()), None]):
            with patch.object(ai, 'parse_args', return_value=type('Args', (), {'data': str(raw), 'max_workers': 1})()), \
                 patch.object(ai, 'process_all_items', return_value=result):
                with self.assertRaises(SystemExit):
                    ai.main()
            self.assertEqual(output.read_text(), 'existing published content')


if __name__ == '__main__':
    unittest.main()
