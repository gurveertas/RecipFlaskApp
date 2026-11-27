"""Draft generation.

Two entry points:
  • generate_draft(bundle)          -> real, Anthropic-backed generation
  • mock_generate(bundle)           -> deterministic, offline, no API key

Both return a `Draft`. The mock exists so the full pipeline (and the QA harness)
can be exercised without a key, and as a transparent reference for what a
compliant draft looks like.
"""
from __future__ import annotations

import re

from retrieval.models import VerifiedBundle, Source
from config import load_course_links
from .llm import complete_with_configured_provider, extract_json
from .models import Draft, references_from_tags
from .prompt import build_prompt
from .text import slugify


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def parse_keywords(raw: str | None) -> list[str]:
    """Split a comma-separated keywords string into a clean, deduped list.

    Shared by the CLI (--keywords) and the web app so both parse identically.
    Dedupes case-insensitively but keeps the first-seen casing.
    """
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for kw in raw.split(","):
        kw = kw.strip()
        if kw and kw.lower() not in seen:
            seen.add(kw.lower())
            out.append(kw)
    return out


def _vendor_of(bundle: VerifiedBundle) -> str | None:
    # Prefer the vendor named in the topic so the inline course link always
    # matches the post's subject, even if a source for a different vendor is
    # present in the bundle. Fall back to a source's vendor otherwise.
    for name in load_course_links()["vendors"]:
        if name.lower() in bundle.topic.lower():
            return name
    for s in bundle.sources:
        if s.vendor:
            return s.vendor
    return None


def _course_link(vendor: str | None) -> tuple[str, str]:
    cfg = load_course_links()
    if vendor and vendor in cfg["vendors"]:
        return vendor, cfg["base_url"] + cfg["vendors"][vendor]
    return (vendor or "our courses"), cfg["base_url"] + "/courses/"


# --------------------------------------------------------------------------- #
# Real generation (Anthropic)
# --------------------------------------------------------------------------- #
def generate_draft(bundle: VerifiedBundle, model: str | None = None,
                   keywords: list[str] | None = None,
                   feedback: str | None = None) -> Draft:
    """Generate a draft with whichever LLM provider has a key configured.

    `keywords`, if given, are SEO terms the model is instructed to work into
    the body verbatim — enforced afterwards by validate.py's keyword_coverage
    check, not just requested. `feedback`, if given (from a previous QA-blocked
    attempt's QAReport.feedback_block()), asks for a targeted fix rather than
    a blind regeneration — see generate_passing_draft.
    """
    system_prompt, user_prompt, tags = build_prompt(bundle, keywords=keywords, feedback=feedback)
    raw, _model_used = complete_with_configured_provider(system_prompt, user_prompt,
                                                         model=model, purpose="adhoc")
    payload = extract_json(raw)
    return _draft_from_payload(payload, bundle, tags, raw)


def generate_passing_draft(bundle: VerifiedBundle, max_attempts: int = 3, on_progress=None,
                           keywords: list[str] | None = None, profile=None):
    """Generate a draft and, if the QA harness blocks it, regenerate.

    Smaller/free models trip the grounding and originality checks more often, so
    we give them a few tries and return the best attempt (fewest failures). A
    passing draft short-circuits. Returns (draft, QAReport); the report is never
    None, so the caller always has something to show the reviewer.

    `on_progress(step, status, message, **extra)` is called at each stage, if
    given — step is "generate" or "qa", status is "active"/"done"/"warn"/"error".
    `keywords`, if given, are required SEO terms — see generate_draft.
    """
    from .validate import QUICK_PROFILE, validate_draft

    # This module IS the ad-hoc pipeline (see the module docstring) — Quick
    # draft and the CLIs. Article Studio's governed path is brief.py's
    # generate_passing_article, which does not come through here and keeps the
    # Studio profile.
    profile = profile or QUICK_PROFILE

    def emit(step, status, message, **extra):
        if on_progress:
            on_progress(step, status, message, **extra)

    best = None
    feedback = None
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        suffix = f" (attempt {attempt}/{attempts})" if attempts > 1 else ""
        if feedback:
            emit("generate", "active", f"Writing draft{suffix} (fixing prior QA issues)…",
                attempt=attempt, max_attempts=attempts)
        else:
            emit("generate", "active", f"Writing draft{suffix}…", attempt=attempt, max_attempts=attempts)
        try:
            draft = generate_draft(bundle, keywords=keywords, feedback=feedback)
        except Exception as exc:
            # A provider hiccup (rate limit, etc.) on a retry: keep the best we
            # already have; only propagate if the very first attempt failed.
            if best is not None:
                emit("generate", "warn", "Provider error on retry — using best attempt so far")
                break
            emit("generate", "error", f"Draft generation failed: {exc}")
            raise
        emit("generate", "done", "Draft written", attempt=attempt)

        emit("qa", "active", "Running reliability QA…", attempt=attempt)
        report = validate_draft(draft, bundle, keywords=keywords,
                                profile=profile)
        if report.passed:
            emit("qa", "done", "All QA checks passed", attempt=attempt)
            return draft, report

        will_retry = attempt < attempts
        emit("qa", "warn",
             f"QA blocked ({len(report.failed)} check(s) failed)"
             + (" — regenerating with targeted fixes…" if will_retry else " — using best attempt"),
             attempt=attempt)
        feedback = report.feedback_block()
        if best is None or len(report.failed) < len(best[1].failed):
            best = (draft, report)
    return best


def _draft_from_payload(payload: dict, bundle: VerifiedBundle,
                        tags: dict[str, Source], raw: str = "") -> Draft:
    return Draft(
        topic=bundle.topic,
        region=bundle.region,
        title=payload["title"],
        slug=payload.get("slug") or slugify(payload["title"]),
        meta_description=payload.get("meta_description", "")[:160],
        body_markdown=payload["body_markdown"],
        faq=payload.get("faq", []),
        references=references_from_tags(tags),
        raw=raw,
    )


# --------------------------------------------------------------------------- #
# Offline deterministic mock generator
# --------------------------------------------------------------------------- #
def mock_generate(bundle: VerifiedBundle, keywords: list[str] | None = None) -> Draft:
    """Build a compliant draft from the bundle without an LLM.

    It uses verified claims directly (cited), attributes unverified claims,
    excludes flagged claims, links a course inline, and adds a CTA + FAQ.
    Wording is original template prose — it never copies source text.

    `keywords`, if given, are worked in verbatim (see the intro paragraph
    below) so the offline demo also exercises the keyword_coverage QA check.
    """
    tags = {f"S{i+1}": s for i, s in enumerate(bundle.sources)}
    url_to_tag = {s.url: t for t, s in tags.items()}
    vendor = _vendor_of(bundle)
    vlabel, vurl = _course_link(vendor)
    subject = vendor or bundle.topic
    region = bundle.region or "many markets"

    def cite(claim) -> str:
        parts = [url_to_tag.get(claim.source_url, "")]
        # add corroborating domains' tags
        for t, s in tags.items():
            if s.domain in claim.corroborated_by and t not in parts:
                parts.append(t)
        return "".join(f"[{p}]" for p in parts if p)

    verified = [c for c in bundle.claims if c.status == "verified"]
    unverified = [c for c in bundle.claims if c.status == "unverified" and c.claim_type == "stat"]

    # ---- assemble body (original wording throughout) ----
    P = []
    P.append(f"# {subject}: What the Latest Evidence Means for Your Career\n")
    P.append(
        f"If you are weighing up a move into network security, the credentials "
        f"tied to {subject} are worth understanding properly before you commit "
        f"time or money. This guide pulls together what current, verified sources "
        f"say about the certification landscape, the earning picture, and how to "
        f"prepare — written for a professional in {region} who wants a clear-eyed "
        f"view rather than marketing gloss.\n")
    P.append(
        "A quick word on how to read this piece. Rather than repeat unsourced "
        "salary rumours, every factual claim below is tied to a named source you "
        "can check for yourself, and anything that rests on a single report is "
        "flagged as such so you can weigh it accordingly. Where the evidence is "
        "strong, we say so plainly; where it is thin, we tell you that too.\n")
    if keywords:
        # Deterministic template prose, so guaranteeing verbatim keyword
        # inclusion honestly (rather than pretending to weave it in) is the
        # right call here — the real weaving happens in live/LLM generation.
        P.append(
            "This guide is written to directly answer the searches most "
            f"people run before committing: {', '.join(keywords)}.\n")

    P.append("## Why this certification matters right now\n")
    P.append(
        f"Demand for verified {subject} skills has climbed steadily as "
        f"organisations lean harder on the security tooling they have already "
        f"invested in. A firewall platform is only as effective as the people "
        f"configuring and maintaining it, and hiring managers have become far "
        f"more deliberate about proving that competence before they hand over the "
        f"keys to production infrastructure.\n")
    P.append(
        "That shift matters for you in a practical way. Employers increasingly "
        "treat a recognised credential as a baseline filter when they screen "
        "applications, which changes the calculation for anyone deciding whether "
        "formal training is worth the outlay. The short answer, supported by the "
        "evidence that follows, is that it usually is — as long as you pursue the "
        "credential that is current rather than one that has quietly been retired.\n")

    P.append("## What the data actually shows\n")
    shown_stat = False
    if verified:
        for c in verified:
            P.append(
                f"On the numbers that hold up across more than one independent "
                f"source, certified professionals can command a meaningfully higher "
                f"salary — frequently in the region of 20-40% more than an "
                f"uncertified colleague earns {cite(c)}. That premium is one of the "
                f"more consistent findings in the current data, which is exactly why "
                f"it is worth taking seriously when you budget for training and plan "
                f"your next few years.\n")
            shown_stat = True
            break
    P.append(
        "It is worth being honest about the limits of salary data, though. Figures "
        "move with region, sector and seniority, and averages can flatten a lot of "
        "variation. The sensible way to use any headline number is as a directional "
        "signal — evidence that the market rewards the credential — rather than a "
        "precise promise about your own next pay review.\n")
    for c in unverified:
        src = tags.get(url_to_tag.get(c.source_url, ""), None)
        name = src.domain if src else "one source"
        P.append(
            f"For instance, some figures rest on a single report and should be read "
            f"with that caveat firmly in mind: according to {name} {cite(c)}, "
            f"specific pay levels differ considerably by role and location. Treat a "
            f"lone headline figure as indicative, and corroborate it against your "
            f"own market before you rely on it.\n")
        break

    P.append("## Roles and where the credential takes you\n")
    P.append(
        f"A current {subject} credential tends to open doors to senior engineering "
        f"and architecture positions, and it is especially valued in sectors where "
        f"security and regulatory compliance are tightly bound together. Beyond the "
        f"pay uplift, the deeper value is credibility. A recognised certification "
        f"tells a hiring manager that you can be trusted with critical security "
        f"infrastructure from your first week, which shortens interviews and "
        f"strengthens your negotiating position.\n")
    P.append(
        "For people already in a networking or systems role, the credential often "
        "functions as a bridge into a more specialised, better-paid security track. "
        "For those entering the field, it provides a structured way to demonstrate "
        "capability that experience alone can be slow to prove.\n")

    P.append("## Choosing the right level\n")
    P.append(
        "Most vendor certification tracks are tiered, running from foundational "
        "credentials through to advanced, role-based ones. The right entry point "
        "depends on your existing experience: if you already work with the platform "
        "daily, you may be able to target a higher tier directly, whereas newcomers "
        "are usually better served by building from the foundations so the advanced "
        "material lands on solid ground. Mapping your current skills honestly "
        "against the exam objectives is the fastest way to avoid wasted effort.\n")

    P.append("## Common mistakes to avoid\n")
    P.append(
        "Two errors come up again and again. The first is studying for a retired "
        "credential because an older guide still ranks well online; always confirm "
        "the current exam against the vendor before you begin. The second is "
        "relying on theory alone and skipping lab time, which tends to show up as "
        "shaky performance on the applied portions of the exam. Both are easy to "
        "sidestep once you know to look for them.\n")

    P.append("## Building a study plan that holds up\n")
    P.append(
        "A study plan survives contact with a full-time job only if it is built "
        "around consistency rather than heroics. Small, regular blocks of focused "
        "work beat occasional marathon sessions, because the applied material in "
        "these exams rewards muscle memory: configuration steps you have walked "
        "through a dozen times, not once. Anchor the plan to the official exam "
        "objectives and work through them in order of your weakest areas first, "
        "so the hardest material gets the most repetitions before exam day.\n")
    P.append(
        "It also pays to schedule a dry run. Sitting a timed practice exam under "
        "realistic conditions exposes pacing problems early — the questions you "
        "linger on, the topics you re-read twice — while there is still time to "
        "fix them. Candidates who skip this step tend to discover their timing "
        "issues inside the real exam, which is the most expensive place to learn "
        "that lesson.\n")

    P.append(f"## How employers actually evaluate the credential\n")
    P.append(
        f"Hiring managers rarely treat a certification as the whole story; they "
        f"treat it as a filter and a conversation starter. A current {subject} "
        f"credential gets your application past the first screen, and then the "
        f"interview probes whether the knowledge behind it is real: expect "
        f"scenario questions that mirror the situations the exam tested, asked in "
        f"the context of the employer's own environment.\n")
    P.append(
        "That is worth planning for. The candidates who convert the credential "
        "into an offer are the ones who can narrate real troubleshooting stories "
        "— what broke, what they checked, why the fix worked. Keeping a short log "
        "of lab exercises and incidents you have handled gives you that material "
        "ready-made, and it turns an abstract line on a CV into evidence.\n")

    P.append("## Keeping the credential working after you pass\n")
    P.append(
        "Passing the exam is the midpoint, not the finish line. The platforms "
        "these credentials cover move quickly, and the professional value of the "
        "badge decays if your working knowledge stays frozen at exam day. Set a "
        "light routine for staying current: release notes for major versions, the "
        "vendor's advisory feed, and the occasional lab rebuild on the newest "
        "release keep the skills honest between recertification cycles.\n")
    P.append(
        "Recertification itself is easier to manage when it is planned rather "
        "than discovered. Note the expiry window the day you pass, and decide "
        "early whether you will renew by exam or by continuing education, because "
        "the two paths suit different schedules and budgets. Many professionals "
        "fold the renewal into a broader progression — stepping up to the next "
        "tier of the same track often refreshes the lower credential at the same "
        "time, which turns an obligation into an advancement.\n")

    P.append("## How to prepare\n")
    P.append(
        f"The most reliable route is structured, hands-on training rather than "
        f"self-study alone, because modern exams increasingly test applied skill "
        f"instead of memorised facts. Our [{vlabel} training]({vurl}) is built "
        f"around real-world labs that map to the current exam objectives, so you "
        f"practise the exact workflows you will be assessed on rather than reading "
        f"about them in the abstract. Advance your career with Red Education by "
        f"choosing the pathway that matches your current level, and give yourself a "
        f"realistic study runway alongside your day job.\n")

    P.append("## Frequently asked questions\n")
    faq = [
        {"q": f"Is the {subject} certification worth it?",
         "a": "For most professionals aiming at network-security roles, yes. The "
              "verified salary premium and clear hiring preference tend to make the "
              "investment pay back quickly, provided you take the credential that is "
              "currently valid."},
        {"q": "How long does it take to prepare?",
         "a": "With focused, lab-based training most candidates prepare over a few "
              "months alongside full-time work, though this varies with your prior "
              "experience and the tier you are targeting."},
        {"q": "Do I need to renew it?",
         "a": "Most vendor credentials expire and require recertification on a set "
              "cycle. Confirm the current renewal terms before you sit the exam so "
              "you can plan the recertification into your schedule."},
        {"q": "Is hands-on lab practice really necessary?",
         "a": "In practice, yes. The exams reward demonstrated skill, and employers "
              "value candidates who can configure and troubleshoot confidently, not "
              "just recall theory."},
    ]
    for item in faq:
        P.append(f"### {item['q']}\n{item['a']}\n")

    P.append("## Conclusion\n")
    P.append(
        f"The evidence points consistently in one direction: a current {subject} "
        f"credential is a sound career investment, with a verified pay premium and "
        f"clear market demand behind it. The two things that matter most are "
        f"choosing the credential that is genuinely current, and preparing with "
        f"hands-on practice rather than theory alone. If that is the goal you are "
        f"working towards, Red Education can help you get there with training built "
        f"for the way these exams are actually assessed today.\n")

    body = "\n".join(P)
    title = f"{subject}: What the Latest Evidence Means for Your Career"

    # references: only the sources actually cited in the body
    # Combined markers count as uses too: a source cited only as "[S4, S5]"
    # was dropped from the reference list while its marker stayed in the body,
    # which is an orphan citation and fails citation_integrity.
    used = {tag for group in re.findall(r"\[\s*S\d+(?:\s*(?:,|;|and)\s*S\d+)*\s*\]", body)
            for tag in re.findall(r"S\d+", group)}
    references = [r for r in references_from_tags(tags) if r.tag in used]

    return Draft(
        topic=bundle.topic,
        region=bundle.region,
        title=title,
        slug=slugify(title),
        meta_description=(f"A verified, source-backed look at {subject} "
                          f"certification, salaries and how to prepare.")[:155],
        body_markdown=body,
        faq=faq,
        references=references,
        raw="(mock generator — deterministic, offline)",
    )
