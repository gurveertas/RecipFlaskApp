#!/usr/bin/env python3
"""Generate a draft from verified sources and run the reliability QA harness.

Offline demo (no API keys — builds a bundle from fixtures, mock-generates, validates):
    python generate.py --offline

Watch the QA harness catch a deliberately broken draft:
    python generate.py --offline --fault-demo

Live (needs SERPER_API_KEY for retrieval and ANTHROPIC_API_KEY for generation):
    python generate.py --topic "Palo Alto NGFW Engineer certification 2026" --region India
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from retrieval.models import Source
from verification import verify
from synthesis import mock_generate, validate_draft, parse_keywords
from synthesis.generate import generate_passing_draft

C = {"grey": "\033[90m", "red": "\033[91m", "green": "\033[92m",
     "yellow": "\033[93m", "cyan": "\033[96m", "bold": "\033[1m", "end": "\033[0m"}


def _c(t, col):
    return f"{C[col]}{t}{C['end']}"


def _load_fixture_bundle(path: str, region_override: str | None):
    with open(path) as f:
        data = json.load(f)
    sources = []
    for s in data["sources"]:
        pub = s.get("published")
        sources.append(Source(
            url=s["url"], domain=s["domain"], title=s["title"], text=s["text"],
            published=date.fromisoformat(pub) if pub else None,
            tier=s.get("tier", 2), is_vendor=s.get("is_vendor", False),
            vendor=s.get("vendor"),
        ))
    region = region_override or data.get("region")
    # Anchor to a fixed "today" so the offline demo's recency filtering stays
    # deterministic and the bundled sources don't age out over time.
    as_of = data.get("as_of")
    today = date.fromisoformat(as_of) if as_of else date.today()
    return verify(data["topic"], region, sources, today=today)


def _inject_faults(draft):
    """Break a good draft three ways to demonstrate the harness catching each."""
    body = draft.body_markdown
    body += (
        "\n\n## Extra section (intentionally broken for the demo)\n"
        # (a) hallucinated statistic not in any source
        "Industry surveys show a staggering 87% pass rate for first-time "
        "candidates. "
        # (b) flagged/vendor-only claim leaking in (the CVE from Unit 42)
        "A recent advisory noted that CVE-2026-01234 was exploited in the wild. "
        # (c) verbatim run copied from a source
        "Workforce data shows that Palo Alto Networks NGFW Engineer certified "
        "professionals earn 20-40% more than uncertified peers in equivalent "
        "network security roles.\n"
    )
    draft.body_markdown = body
    return draft


def print_draft(draft):
    print(_c("\n" + "=" * 74, "grey"))
    print(_c(" DRAFT", "bold"))
    print(_c("=" * 74, "grey"))
    print(_c(f"title : {draft.title}", "bold"))
    print(f"slug  : {draft.slug}")
    print(f"meta  : {draft.meta_description}  ({len(draft.meta_description)} chars)")
    print(f"words : {draft.word_count()}")
    print(_c("-" * 74, "grey"))
    print(draft.body_markdown)
    print(_c("-" * 74, "grey"))
    print(_c("References:", "bold"))
    for ref in draft.references:
        print(_c(f"  [{ref.tag}] {ref.url}", "grey"))


def print_qa(report):
    print(_c("\n" + "=" * 74, "grey"))
    print(_c(" RELIABILITY QA REPORT", "bold"))
    print(_c("=" * 74, "grey"))
    icon = {"pass": _c("PASS", "green"), "warn": _c("WARN", "yellow"),
            "fail": _c("FAIL", "red")}
    for c in report.checks:
        print(f"  {icon[c.status]}  {c.name}")
        if c.detail:
            print(_c(f"        {c.detail}", "grey"))
    verdict = (_c("PUBLISH CANDIDATE (no failures)", "green") if report.passed
               else _c(f"BLOCKED — {len(report.failed)} failure(s)", "red"))
    print(_c("-" * 74, "grey"))
    print(f"  verdict: {verdict}"
          + (f", {len(report.warned)} warning(s)" if report.warned else ""))
    # The quality score alongside the pass/fail gate — same number the web
    # UI's review panel shows. Coloured by band, not by the raw figure, so a
    # blocked draft never prints a green score (see qa_score_band).
    band_colour = {"pass": "green", "warn": "yellow", "fail": "red"}
    if report.score >= 0:
        print(f"  score:   {_c(str(report.score) + '/100', band_colour[report.score_band])}"
              + _c("   (integrity 60 · advisory 40; warn = half credit)", "grey"))
    print(_c("-" * 74, "grey"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate + QA a blog draft")
    ap.add_argument("--topic")
    ap.add_argument("--region")
    ap.add_argument("--keywords",
                    help="comma-separated SEO keywords the draft must contain "
                         "(checked by the QA harness's keyword_coverage check)")
    ap.add_argument("--offline", action="store_true",
                    help="build a bundle from fixtures and mock-generate (no keys)")
    ap.add_argument("--fault-demo", action="store_true",
                    help="inject faults into the draft to show the QA harness catching them")
    ap.add_argument("--fixtures", default="fixtures/sample_sources.json")
    ap.add_argument("--json", metavar="PATH", help="write the draft as JSON")
    args = ap.parse_args(argv)
    keywords = parse_keywords(args.keywords)

    if args.offline:
        bundle = _load_fixture_bundle(args.fixtures, args.region)
        draft = mock_generate(bundle, keywords=keywords)
    else:
        if not args.topic:
            ap.error("--topic is required in live mode (or use --offline)")
        from retrieval import retrieve
        sources, dropped = retrieve(args.topic, limit=8)
        bundle = verify(args.topic, args.region, sources, dropped_in=dropped)
        draft, _ = generate_passing_draft(bundle, keywords=keywords)   # regenerate on QA block

    if args.fault_demo:
        draft = _inject_faults(draft)

    print_draft(draft)
    report = validate_draft(draft, bundle, keywords=keywords)
    print_qa(report)

    if args.json:
        with open(args.json, "w") as f:
            f.write(draft.to_json())
        print(_c(f"\n-> draft written to {args.json}", "cyan"))

    # non-zero exit if blocked, so this can gate a CI/publish step later
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
