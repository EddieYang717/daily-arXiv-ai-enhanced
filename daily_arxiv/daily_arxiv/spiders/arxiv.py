"""Legacy Scrapy export adapter. Publishing uses arxiv_daily.runner."""
import os
from pathlib import Path
import sys

import scrapy

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from arxiv_daily.metadata import parse_listing


class ArxivSpider(scrapy.Spider):
    name = 'arxiv'
    allowed_domains = ['arxiv.org']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_categories = {c.strip() for c in (os.environ.get('CATEGORIES') or 'cs.IT,eess.SP').split(',')}
        self.start_urls = [f'https://arxiv.org/list/{cat}/new' for cat in sorted(self.target_categories)]
        self.seen = set()

    def parse(self, response):
        page = parse_listing(response.text, response.url, self.target_categories)
        if page.errors:
            self.crawler.stats.inc_value('metadata_failures', len(page.errors))
            raise ValueError(f'Incomplete metadata: {page.errors}')
        for identifier, item in page.items.items():
            if identifier not in self.seen:
                self.seen.add(identifier)
                yield item
        if page.next_url:
            yield response.follow(page.next_url, self.parse)
