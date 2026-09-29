"""Render every validated paper; incomplete input is an error."""
import argparse
import os
from pathlib import Path
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from arxiv_daily.storage import atomic_text, read_jsonl, successful_ai


def render(items):
    preference = [v.strip() for v in os.environ.get("CATEGORIES", "cs.IT,eess.SP").split(",")]
    template = Path(__file__).with_name("paper_template.md").read_text(encoding="utf-8")
    by_category = {}
    seen = set()
    for item in items:
        if item["id"] in seen or not successful_ai(item, historical=True):
            raise ValueError(f"Duplicate or incomplete Markdown input: {item['id']}")
        seen.add(item["id"])
        by_category.setdefault(item["categories"][0], []).append(item)
    categories = sorted(by_category, key=lambda c: (preference.index(c) if c in preference else len(preference), c))
    markdown = "<div id=toc></div>\n\n# Table of Contents\n\n"
    for category in categories:
        markdown += f"- [{category}](#{category}) [Total: {len(by_category[category])}]\n"
    rendered = set()
    for category in categories:
        markdown += f"\n\n<div id='{category}'></div>\n\n# {category} [[Back]](#toc)\n\n"
        rows = sorted(by_category[category], key=lambda item: (-int(item['AI'].get('relevance_score', 0) or 0), item['id']))
        for item in rows:
            ai = item['AI']
            topics = ai.get('relevance_topics', [])
            markdown += template.format(title=item['title'], authors=",".join(item['authors']),
                summary=item['summary'], url=item['abs'], cate=category, idx=len(rendered) + 1,
                **{field: ai[field] for field in ('tldr', 'motivation', 'method', 'result', 'conclusion')},
                relevance_score=ai.get('relevance_score', 0), relevance_reason=ai.get('relevance_reason', ''),
                relevance_topics=", ".join(topics) if isinstance(topics, list) else topics) + "\n\n"
            rendered.add(item['id'])
    return markdown, rendered


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    markdown, _ = render(read_jsonl(args.data))
    output = args.output or args.data.with_name(args.data.stem.split('_AI_enhanced_')[0] + '.md')
    atomic_text(output, markdown)


if __name__ == "__main__":
    main()
