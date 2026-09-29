"""Parse public arXiv metadata without one export API call per paper."""
from dataclasses import dataclass
from datetime import datetime
import re
from urllib.parse import urljoin

from parsel import Selector


class InvalidMetadata(ValueError):
    pass


def paper_id(value):
    value = re.sub(r"v\d+$", "", value.strip().removeprefix("arXiv:"))
    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})", value):
        raise InvalidMetadata(f"Invalid arXiv ID: {value!r}")
    return value


def clean(node):
    # Keep inline mathematical text; remove only arXiv's field labels.
    return " ".join("".join(node.xpath(
        ".//text()[not(ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' descriptor ')])]"
    ).getall()).split())


def categories(node):
    primary = re.findall(r"\(([^()]+)\)", clean(node.css(".primary-subject")))
    values = re.findall(r"\(([^()]+)\)", clean(node))
    if not primary or not values:
        raise InvalidMetadata("Missing primary subject")
    return list(dict.fromkeys(primary + values))


def validate_metadata(item):
    if not isinstance(item, dict):
        raise InvalidMetadata("Paper must be an object")
    identifier = paper_id(item.get("id", ""))
    for field in ("title", "summary", "abs", "pdf"):
        if not isinstance(item.get(field), str) or not item[field].strip():
            raise InvalidMetadata(f"{identifier}: missing {field}")
    for field in ("authors", "categories"):
        if not isinstance(item.get(field), list) or not item[field] or not all(
            isinstance(v, str) and v.strip() for v in item[field]
        ):
            raise InvalidMetadata(f"{identifier}: invalid {field}")
    if item.get("comment") is not None and not isinstance(item["comment"], str):
        raise InvalidMetadata(f"{identifier}: invalid comment")
    for field, route in (("abs", "abs"), ("pdf", "pdf")):
        if item[field] not in (f"https://arxiv.org/{route}/{identifier}",
                               f"http://arxiv.org/{route}/{identifier}"):
            # Historical files may retain a version suffix from the old API.
            if not re.fullmatch(rf"https?://arxiv.org/{route}/{re.escape(identifier)}v\d+", item[field]):
                raise InvalidMetadata(f"{identifier}: inconsistent {field} URL")
    return item


def make_item(identifier, title, authors, summary, subjects, comment=None):
    identifier = paper_id(identifier)
    return validate_metadata(dict(id=identifier, title=title, authors=authors,
        summary=summary, categories=subjects, comment=comment or None,
        abs=f"https://arxiv.org/abs/{identifier}", pdf=f"https://arxiv.org/pdf/{identifier}"))


@dataclass
class Listing:
    announcement: str
    total: int
    all_ids: list
    candidates: list
    items: dict
    errors: dict
    next_url: str | None


def parse_listing(html, url, targets):
    doc = Selector(text=html)
    text = clean(doc.css("#dlpage"))
    date_match = re.search(r"Showing new listings for (\w+, \d{1,2} \w+ \d{4})", text)
    total_match = re.search(r"Total of (\d+) entr(?:y|ies)", text)
    if not date_match or not total_match:
        raise InvalidMetadata("Unrecognized listing date/count; not a verified empty listing")
    announcement = datetime.strptime(date_match[1], "%A, %d %B %Y").date().isoformat()
    total = int(total_match[1])
    sections = doc.css("#dlpage dl h3")
    expected_shown = 0
    for section in sections:
        match = re.fullmatch(r"(New|Cross|Replacement) submissions \(showing (\d+) of (\d+) entries\)", clean(section))
        if not match or int(match[2]) > int(match[3]):
            raise InvalidMetadata(f"Unrecognized listing section: {clean(section)}")
        following = section.xpath("following-sibling::dt[preceding-sibling::h3[1] = $heading]", heading=clean(section))
        if len(following) != int(match[2]):
            raise InvalidMetadata("Listing section count mismatch")
        expected_shown += int(match[2])
    rows = doc.css("#dlpage dl dt")
    if len(rows) != expected_shown or (total and not sections):
        raise InvalidMetadata("Listing entries/sections missing")
    all_ids, candidates, items, errors = [], [], {}, {}
    for row in rows:
        link = row.css("a[title='Abstract']::attr(href)").get()
        if not link or "/abs/" not in link:
            raise InvalidMetadata("Listing row has no paper ID")
        identifier = paper_id(link.split("/abs/", 1)[1])
        all_ids.append(identifier)
        section = clean(row.xpath("preceding-sibling::h3[1]"))
        if section.startswith("Replacement submissions"):
            continue
        if not section.startswith(("New submissions", "Cross submissions")):
            raise InvalidMetadata("Paper outside a known section")
        dd = row.xpath("following-sibling::*[1][self::dd]")
        try:
            subjects = categories(dd.css(".list-subjects"))
            if subjects[0] not in targets:
                continue
            candidates.append(identifier)
            items[identifier] = make_item(identifier, clean(dd.css(".list-title")),
                [clean(a) for a in dd.css(".list-authors a")],
                clean(dd.css(".meta > p.mathjax")), subjects,
                clean(dd.css(".list-comments")))
        except InvalidMetadata as exc:
            if identifier not in candidates:
                candidates.append(identifier)
            errors[identifier] = str(exc)
    if len(all_ids) != len(set(all_ids)):
        raise InvalidMetadata("Duplicate IDs within listing page")
    next_links = doc.xpath("//a[contains(translate(normalize-space(.), 'NEXT', 'next'), 'next')]/@href").getall()
    next_url = urljoin(url, next_links[0]) if next_links else None
    if len(all_ids) > total:
        raise InvalidMetadata("Listing contains more entries than advertised")
    return Listing(announcement, total, all_ids, candidates, items, errors, next_url)


def parse_abstract(html, expected_id):
    doc = Selector(text=html)
    identifier = doc.css('meta[name="citation_arxiv_id"]::attr(content)').get()
    if not identifier or paper_id(identifier) != paper_id(expected_id):
        raise InvalidMetadata(f"Abstract page ID mismatch for {expected_id}")
    return make_item(identifier, clean(doc.css("h1.title")),
        [clean(a) for a in doc.css('.authors a')],
        clean(doc.css("blockquote.abstract")), categories(doc.css("td.subjects")),
        clean(doc.css("td.comments")))
