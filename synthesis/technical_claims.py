"""Evidence coverage and scope warnings for technical product claims.

Matching source terms is not semantic fact verification. These checks detect
missing evidence and known scope mistakes; a pass does not replace expert review.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import markdown


_CITATION = re.compile(r"\[S\d+(?:\s*,\s*S\d+)*\]")
_NUMBER = re.compile(r"\b\d[\d,]*(?:\.\d+)*\b")
_LICENCE = re.compile(r"\blicen[cs](?:e\w*|ing)\b", re.I)
_CAPACITY = re.compile(
    r"\b(?:up to|maximum(?: of)?|limit(?: of| is)?|capacity(?: of| is)?)\s+"
    r"(?P<count>\d[\d,]*)\s+(?:managed\s+)?(?P<unit>firewalls?|devices?|nodes?|users?|"
    r"appliances?|tenants?|instances?|connections?|sessions?)\b", re.I)
_OVERWRITE = re.compile(r"\boverwr(?:it\w*|ote|itten)\b", re.I)
_RELEASE = re.compile(r"\b\d+(?:\.\d+){1,3}\b")
_BENEFIT = re.compile(
    r"\b(?:upgrad\w*|simplif\w*|automat\w*|improv\w*|introduc\w*|"
    r"support\w*|reduc\w*|prioritis\w*|prioritiz\w*)\b", re.I)
_CONDITION = re.compile(
    r"\b(?:if|when|depending|depends|may|can|could|selected|conflicting|"
    r"scoped|force template values)\b", re.I)


def _url_key(url: str) -> str:
    parts = urlsplit(url)
    return parts.netloc.lower() + parts.path.rstrip("/")


class _ClaimText(HTMLParser):
    """Keep table rows together and recover source tags from cited link URLs."""

    def __init__(self, url_tags: dict[str, str]):
        super().__init__()
        self.parts: list[str] = []
        self.url_tags = url_tags
        self.link_tags: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"pre", "code", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.skip += 1
        if tag in {"p", "li", "tr", "br"}:
            self.parts.append("\n")
        if tag in {"td", "th"}:
            self.parts.append(" ")
        if tag == "a":
            self.link_tags.append(self.url_tags.get(
                _url_key(dict(attrs).get("href") or ""), ""))

    def handle_endtag(self, tag):
        if tag == "a" and self.link_tags:
            source_tag = self.link_tags.pop()
            if source_tag and not self.skip:
                self.parts.append(f" [{source_tag}]")
        if tag in {"pre", "code", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.skip = max(0, self.skip - 1)
        if tag in {"p", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(re.sub(r"\s+", " ", data))


def claim_sentences(body: str, references: list[dict]) -> list[str]:
    parser = _ClaimText({_url_key(r.get("url", "")): r.get("tag", "")
                         for r in references})
    parser.feed(markdown.markdown(body or "", extensions=["tables"]))
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+",
                                      "".join(parser.parts)) if s.strip()]


def technical_claim_findings(body, references, bundle, kb):
    """Return (blocking evidence gaps, advisory scope/source findings)."""
    refs = {r.get("tag", ""): r for r in references}
    sources = {_url_key(s.url): s for s in bundle.sources}
    gaps, warnings = [], []
    for sentence in claim_sentences(body, references):
        if sentence.endswith("?"):
            continue
        categories = []
        if (_LICENCE.search(sentence) and re.search(
                r"\b(?:requires?|needs?|determines?|limits?|enforces?|entitles?|"
                r"includes?|licensed|based on|per[- ](?:device|user|core))\b",
                sentence, re.I)):
            categories.append(("licensing", _LICENCE))
        capacity = _CAPACITY.search(sentence)
        if capacity:
            categories.append(("capacity", re.compile(
                r"\b(?:firewall|device|node|user|appliance|tenant|instance|"
                r"connection|session)s?\b", re.I)))
            if not re.search(r"\b(?:licen[cs]\w*|model|platform|mode|"
                             r"configuration|resource|sizing|depending)\b",
                             sentence, re.I):
                warnings.append("Qualify capacity by platform, operating mode, "
                                f"resources and licence: {sentence[:150]}")
        if _OVERWRITE.search(sentence):
            categories.append(("configuration overwrite", _OVERWRITE))
            if not _CONDITION.search(sentence):
                warnings.append("State which migration/push operation overwrites "
                                f"which settings: {sentence[:150]}")
        versions = _RELEASE.findall(sentence)
        if (versions and _BENEFIT.search(sentence) and re.search(
                r"\b(?:release|version|update|PAN-OS|BIG-IP|AOS|FortiOS|Junos|IOS-XE)\b",
                sentence, re.I)):
            categories.append(("release capability", _BENEFIT))
        if re.search(r"\b(?:essential|mandatory|required)\b.{0,80}"
                     r"\bregardless of\b|\b(?:the|industry) standard for "
                     r"(?:enterprise resilience|all organisations|all organizations)",
                     sentence, re.I):
            warnings.append("Replace universal product recommendations with "
                            f"deployment-specific criteria: {sentence[:150]}")
        if not categories:
            continue
        tags = {tag for group in _CITATION.findall(sentence)
                for tag in re.findall(r"S\d+", group)}
        cited = [sources.get(_url_key(refs[tag].get("url", "")))
                 for tag in tags if tag in refs]
        cited = [s for s in cited if s and s.text.strip()]
        if kb.vendor_domains:
            cited = [s for s in cited if any(
                (urlsplit(s.url).hostname or "").lower() == d.lower()
                or (urlsplit(s.url).hostname or "").lower().endswith("." + d.lower())
                for d in kb.vendor_domains)]
        for label, concept in categories:
            numbers = (_NUMBER.findall(capacity.group()) if label == "capacity"
                       else versions if label == "release capability" else [])
            supported = [s for s in cited if concept.search(s.text)
                         and all(n.replace(",", "") in {
                             x.replace(",", "") for x in _NUMBER.findall(s.text)}
                             for n in numbers)]
            if label == "capacity":
                count = re.escape(capacity.group("count").replace(",", ""))
                unit = re.escape(capacity.group("unit").lower().rstrip("s"))
                supported = [s for s in supported if re.search(
                    rf"\b{count}\b[^.!?]{{0,100}}\b{unit}s?\b",
                    s.text.replace(",", ""), re.I)]
            if not supported:
                gaps.append(f"{label}: cite readable evidence in the same sentence "
                            f"for this claim and its figures: {sentence[:150]}")
            elif all((urlsplit(s.url).hostname or "").startswith(("docs.", "techdocs."))
                     and len([p for p in urlsplit(s.url).path.split("/") if p]) <= 1
                     for s in supported):
                warnings.append("Replace the documentation index citation with "
                                f"the exact technical page: {sentence[:150]}")
    return list(dict.fromkeys(gaps)), list(dict.fromkeys(warnings))


def panorama_scope_problems(body: str, references: list[dict]) -> list[str]:
    """Known Panorama errors, independent of whether a citation was attached."""
    problems = []
    for paragraph in re.split(r"\n\s*\n", body or ""):
        sentences = claim_sentences(paragraph, references)
        plain = " ".join(sentences)
        if not re.search(r"\bPanorama\b", plain, re.I):
            continue
        for sentence in sentences:
            if (re.search(r"\bPanorama\b", sentence, re.I)
                    and re.search(r"\brequires?\b.{0,50}management licen[cs]es"
                                  r".{0,25}\bfor\b.{0,25}\b(?:appliances?|firewalls?)"
                                  r"\b.{0,20}\b(?:monitors?|manages?)\b", sentence, re.I)
                    and not re.search(r"\b(?:not|never)\b", sentence, re.I)):
                problems.append("Panorama device-management licensing limits managed "
                                "firewall serial numbers per instance; do not describe "
                                "a separate management licence installed on every firewall")
        if (re.search(r"\bPrisma Access\b", plain, re.I)
                and re.search(r"\b(?:manage\w*|integration)\b", plain, re.I)
                and not re.search(r"Panorama[- ]managed|managed (?:by|through|with) "
                                  r"Panorama|Cloud Services plugin|Strata Cloud Manager",
                                  plain, re.I)):
            problems.append("Qualify Prisma Access as a Panorama-managed deployment "
                            "using the Cloud Services plugin; Strata Cloud Manager "
                            "is a separate management option")
    return list(dict.fromkeys(problems))
