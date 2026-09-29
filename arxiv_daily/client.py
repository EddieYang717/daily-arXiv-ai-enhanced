"""One serial, robots-aware transport with durable server cooldowns."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

USER_AGENT = "daily-arxiv-enhanced/1.0 (+https://github.com/EddieYang717/daily-arXiv-ai-enhanced)"
SAFE_HEADERS = {"date", "retry-after", "content-type", "server", "via", "x-cache",
                "x-request-id", "x-cloud-trace-context", "cf-ray"}


class FetchError(RuntimeError):
    pass


class Cooldown(FetchError):
    pass


def retry_after(value, now):
    if not value:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return 0


class ArxivClient:
    def __init__(self, state=None, save=lambda: None, log=lambda event: None,
                 session=None, clock=time.time, sleep=time.sleep):
        self.state = state if state is not None else {}
        self.save, self.log = save, log
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html, text/plain;q=0.9"})
        self.clock, self.sleep = clock, sleep
        self.delay = 15  # arxiv.org currently asks generic robots for 15 seconds.
        self.robots = None
        self.stopped = False
        self.consecutive_rejections = 0

    def wait(self, seconds):
        # Short sleeps allow termination to persist the latest checkpoint.
        until = self.clock() + seconds
        while self.clock() < until:
            self.sleep(min(30, until - self.clock()))

    def get(self, url):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "arxiv.org" or not parsed.path.startswith(("/list/", "/abs/")):
            raise FetchError("Only arxiv.org listing and abstract URLs are supported")
        if self.robots is None:
            try:
                robots_text = self._request("https://arxiv.org/robots.txt")
            except FetchError:
                # Do not retry a failed robots fetch for each queued paper.
                self.stopped = True
                raise
            self.robots = RobotFileParser()
            self.robots.parse(robots_text.splitlines())
            self.delay = max(5, self.robots.crawl_delay(USER_AGENT) or 15)
        if not self.robots.can_fetch(USER_AGENT, url):
            raise FetchError(f"robots.txt disallows {url}")
        return self._request(url)

    def _request(self, url):
        if self.stopped:
            raise Cooldown("arXiv requests paused for this run")
        for attempt in range(3):
            now = self.clock()
            wait = max(self.state.get("next_allowed_at", 0), self.state.get("last_response_at", 0) + self.delay) - now
            if wait > 300:
                self.stopped = True
                raise Cooldown("Server cooldown exceeds this run's waiting budget")
            self.wait(max(0, wait))
            status, headers, body = None, {}, ""
            try:
                response = self.session.get(url, timeout=30, allow_redirects=False)
                status, headers = response.status_code, response.headers
                response.encoding = "utf-8"
                body = response.text
                error = f"HTTP {status}"
            except requests.RequestException as exc:
                error = type(exc).__name__
            now = self.clock()
            self.state["last_response_at"] = now
            self.log({"time": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                      "url": url, "attempt": attempt + 1, "status": status,
                      "headers": {k: v for k, v in headers.items() if k.lower() in SAFE_HEADERS},
                      "body_excerpt": body[:1024] if status != 200 else "", "error": error if status != 200 else None})
            if status == 200:
                self.consecutive_rejections = 0
                self.state["next_allowed_at"] = now + self.delay
                self.save()
                return body
            self.consecutive_rejections = self.consecutive_rejections + 1 if status in (406, 429) else 0
            delay = max(30 * 2**attempt, retry_after(headers.get("Retry-After"), now))
            self.state["next_allowed_at"] = now + delay
            if self.consecutive_rejections >= 3:
                self.state["next_allowed_at"] = max(now + 900, self.state["next_allowed_at"])
                self.stopped = True
            self.save()
            if self.stopped:
                raise Cooldown("Three consecutive HTTP 406/429 responses; crawl paused")
            if status is not None and status not in (406, 429, 500, 502, 503, 504):
                break
        raise FetchError(f"{url}: {error} after {attempt + 1} attempts")
