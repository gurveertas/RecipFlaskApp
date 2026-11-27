#!/usr/bin/env python3
"""Run the retrieval + verification pipeline on a topic and print a report.

Examples
--------
Offline demo (no API keys needed — uses bundled sample sources):
    python run.py --offline

Live (needs SERPER_API_KEY in the environment):
    python run.py --topic "Palo Alto NGFW Engineer certification 2026" --region Australia

Dump the machine-readable bundle for the next (synthesis) stage:
    python run.py --offline --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from retrieval.models import Source
from verification import verify


# ----------------------------- report rendering -----------------------------
C = {"grey": "\033[90m", "red": "\033[91m", "green": "\033[92m",
     "yellow": "\033[93m", "cyan": "\033[96m", "bold": "\033[1m", "end": "\033[0m"}


def _c(text, color):
    return f"{C[color]}{text}{C['end']}"


def print_report(bundle) -> None:
    print()
    print(_c("=" * 74, "grey"))
    print(_c(f" VERIFIED SOURCE BUNDLE", "bold"))
    print(f" topic : {bundle.topic}")
    print(f" region: {bundle.region or '(none / global)'}")
    print(_c("=" * 74, "grey"))

    print(_c(f"\nSOURCES KEPT ({len(bundle.sources)})", "bold"))
    for s in bundle.sources:
        tag = _c(f"vendor:{s.vendor}", "yellow") if s.is_vendor else _c(f"tier{s.tier}", "cyan")
        print(f"  • [{tag}] {s.title[:70]}")
        print(_c(f"      {s.url}  ({s.published or 'no date'})", "grey"))

    if bundle.dropped:
        print(_c(f"\nDROPPED ({len(bundle.dropped)})", "bold"))
        for d in bundle.dropped:
            print(_c(f"  – {d['url']}", "grey"))
            print(_c(f"    {d['reason']}", "grey"))

    print(_c(f"\nCLAIMS ({len(bundle.claims)})", "bold"))
    status_color = {"verified": "green", "unverified": "yellow", "flagged": "red"}
    for c in bundle.claims:
        badge = _c(c.status.upper(), status_color.get(c.status, "grey"))
        sec = _c(" [security]", "red") if c.is_security_claim else ""
        print(f"  {badge}{sec}  ({c.claim_type}) {c.text[:88]}")
        origin = c.source_domain
        if c.corroborated_by:
            origin += _c(f"  ✓ also: {', '.join(c.corroborated_by)}", "green")
        print(_c(f"      source: {origin}", "grey"))
        if c.note:
            print(_c(f"      ⚑ {c.note}", "red"))

    if bundle.warnings:
        print(_c(f"\nWARNINGS ({len(bundle.warnings)})", "bold"))
        for w in bundle.warnings:
            print(_c(f"  ! {w}", "yellow"))

    v, f = len(bundle.verified_claims), len(bundle.flagged_claims)
    print(_c("\n" + "-" * 74, "grey"))
    print(f" summary: {_c(str(v)+' verified', 'green')}, "
          f"{_c(str(f)+' flagged', 'red')}, "
          f"{len(bundle.claims)-v-f} unverified")
    print(_c("-" * 74, "grey"))


# ----------------------------- entrypoints ----------------------------------
def load_fixtures(path: str) -> tuple[str, str, list[Source], date]:
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
    # Anchor the demo to a fixed "today" so recency filtering is deterministic and
    # the bundled sources don't silently age out of the offline demo over time.
    as_of = data.get("as_of")
    today = date.fromisoformat(as_of) if as_of else date.today()
    return data["topic"], data.get("region"), sources, today


def main(argv=None):
    ap = argparse.ArgumentParser(description="Retrieval + verification pipeline")
    ap.add_argument("--topic", help="topic to research")
    ap.add_argument("--region", help="target region (per-post), e.g. Australia, India")
    ap.add_argument("--offline", action="store_true",
                    help="use bundled sample sources (no API key needed)")
    ap.add_argument("--fixtures", default="fixtures/sample_sources.json")
    ap.add_argument("--limit", type=int, default=8, help="max sources (live mode)")
    ap.add_argument("--use-llm", action="store_true",
                    help="use Anthropic for claim extraction if ANTHROPIC_API_KEY is set")
    ap.add_argument("--json", metavar="PATH", help="also write the bundle as JSON")
    args = ap.parse_args(argv)

    if args.offline:
        topic, region, sources, today = load_fixtures(args.fixtures)
        region = args.region or region
        bundle = verify(topic, region, sources, today=today, use_llm=args.use_llm)
    else:
        if not args.topic:
            ap.error("--topic is required in live mode (or use --offline)")
        from retrieval import retrieve   # imported lazily so offline needs no deps
        sources, dropped = retrieve(args.topic, limit=args.limit)
        bundle = verify(args.topic, args.region, sources,
                        dropped_in=dropped, use_llm=args.use_llm)

    print_report(bundle)
    if args.json:
        with open(args.json, "w") as f:
            f.write(bundle.to_json())
        print(_c(f"\n→ bundle written to {args.json}", "cyan"))


if __name__ == "__main__":
    sys.exit(main())
