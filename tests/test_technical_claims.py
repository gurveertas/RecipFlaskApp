"""Panorama regressions and shared evidence checks across vendor articles."""
import pytest

from retrieval.models import Source, VerifiedBundle
from synthesis.models import Draft, Reference
from synthesis.qa_rules import QaKnowledge
from synthesis.technical_claims import technical_claim_findings, panorama_scope_problems
from synthesis.validate import validate_draft


URL = "https://docs.vendor.example/product/licensing"
REFS = [{"tag": "S1", "url": URL, "title": "Product licensing",
         "domain": "docs.vendor.example"}]


def findings(body, evidence="", *, url=URL, vendor="Example"):
    refs = [dict(REFS[0], url=url)]
    bundle = VerifiedBundle(topic="Product management", region=None, sources=[
        Source(url, "docs.vendor.example", "Product documentation", evidence)])
    return technical_claim_findings(
        body, refs, bundle, QaKnowledge(vendor_name=vendor,
                                       vendor_domains={"vendor.example"}))


@pytest.mark.parametrize("vendor", ["Palo Alto Networks", "F5", "Nutanix", "Fortinet"])
def test_licensing_evidence_required_for_every_vendor(vendor):
    gaps, _ = findings("The controller requires a device-management licence.",
                       "A device management license is required.", vendor=vendor)
    assert gaps and "licensing" in gaps[0]


def test_valid_tag_on_unrelated_source_does_not_clear_licensing():
    gaps, _ = findings("The controller requires a management licence [S1].",
                       "The product monitors network activity.")
    assert gaps


def test_same_sentence_readable_licensing_source_passes():
    assert findings("The controller requires a management licence [S1].",
                    "Every controller requires a device management license.") == ([], [])


def test_neighbouring_citation_does_not_clear_claim():
    gaps, _ = findings("The controller requires a licence. Read the docs [S1].",
                       "A licence is required.")
    assert gaps


def test_inline_link_can_supply_evidence():
    assert findings(f"The controller requires a [management licence]({URL}).",
                    "A management license is required.") == ([], [])


@pytest.mark.parametrize("url", ["https://vendor.example.attacker.test/license",
                                 "https://competitor.example/license"])
def test_source_hostname_must_match_subject_vendor(url):
    gaps, _ = findings("The controller requires a licence [S1].",
                       "A license is required.", url=url)
    assert gaps


def test_capacity_is_checked_in_table_and_must_match_unit():
    body = "| Mode | Capacity |\n|---|---|\n| Licensed controller | Up to 5,000 firewalls [S1] |"
    assert findings(body, "The licensed controller supports 5000 firewalls.") == ([], [])
    assert findings(body, "The controller supports 5000 users.")[0]
    assert findings(body, "The controller supports 500 firewalls.")[0]


def test_vendor_supported_ha_pair_claim_warns_for_scope_but_does_not_fail():
    body = "A single HA pair can manage up to 5,000 firewalls [S1]."
    gaps, warnings = findings(body, "A single HA pair supports 5000 firewalls.")
    assert not gaps
    assert warnings and "Qualify capacity" in warnings[0]


def test_qualified_migration_is_supported_by_the_push_procedure():
    body = "When Force Template Values is enabled, conflicting local settings are overwritten [S1]."
    assert findings(body, "Force Template Values overwrites conflicting settings.") == ([], [])
    assert findings(body, "Import the configuration into the controller.")[0]


def test_release_claim_requires_its_version_in_evidence():
    body = "Version 12.1.2 introduces automated upgrades [S1]."
    assert findings(body, "Version 12.1.2 supports automated upgrades.") == ([], [])
    assert findings(body, "Version 11.0 introduced automated upgrades.")[0]


def test_supported_homepage_claim_warns_to_use_exact_page():
    gaps, warnings = findings("Version 12.1 simplifies upgrades [S1].",
                               "Version 12.1 simplifies upgrades.",
                               url="https://docs.vendor.example/product")
    assert not gaps
    assert warnings and "documentation index" in warnings[0]


def test_headings_code_questions_and_ordinary_advice_are_not_claims():
    body = ("## Does the product require a licence?\n\n"
            "Compare licensing costs before deciding.\n\n"
            "`The product requires a licence.`\n\n"
            "```text\nThe product requires a licence.\n```\n\n"
            "Do upgrades overwrite settings?\n\n"
            "Improvements reduced overhead by 3.5 percent.")
    assert findings(body) == ([], [])


@pytest.mark.parametrize("text", [
    "It becomes an essential tool regardless of company size.",
    "Panorama is the standard for enterprise resilience.",
])
def test_universal_recommendations_warn(text):
    assert findings(text)[1]


@pytest.mark.parametrize("text", [
    "Panorama requires dedicated management licenses for every appliance it monitors [S1].",
    "Panorama requires additional, dedicated management licenses for the appliances it monitors within its hierarchy [S1].",
])
def test_known_licensing_error_blocks_even_with_citation(text):
    assert panorama_scope_problems(text, REFS)


def test_correct_licensing_and_negated_error_are_allowed():
    assert not panorama_scope_problems(
        "Panorama requires a device-management licence whose capacity limits the number of managed firewalls.", REFS)
    assert not panorama_scope_problems(
        "Panorama does not require separate management licenses for every appliance it monitors.", REFS)


def test_prisma_management_needs_a_qualified_deployment():
    assert panorama_scope_problems(
        "Panorama offers integration for unified management including Prisma Access [S1].", REFS)
    assert not panorama_scope_problems(
        "Prisma Access can use Panorama in a Panorama-managed deployment with the Cloud Services plugin. "
        "Strata Cloud Manager is another management option.", REFS)


def test_stored_faq_is_checked_independently_of_correct_body():
    bundle = VerifiedBundle(topic="Panorama", region=None, sources=[
        Source(URL, "docs.vendor.example", "Licensing", "A management licence is required.")])
    draft = Draft(topic="Panorama", region=None, title="Panorama", slug="panorama",
                  meta_description="", body_markdown="Panorama can manage Prisma Access in Panorama-managed mode.",
                  faq=[{"question": "How is it licensed?", "answer":
                        "Panorama requires dedicated management licenses for every appliance it monitors."},
                       {"question": "Can I manage Prisma Access?", "answer":
                        "Panorama supports unified management including Prisma Access."}],
                  references=[Reference(**REFS[0])])
    report = validate_draft(draft, bundle, kb=QaKnowledge(vendor_name="Palo Alto Networks"))
    checks = {c.name: c for c in report.checks}
    assert checks["technical_claim_evidence"].status == "fail"
    assert checks["product_scope"].status == "fail"
    assert "Prisma Access" in checks["product_scope"].detail


def test_contract_reaches_brief_writer_and_section_rewrite():
    from synthesis import style
    from synthesis.brief import (ContentBrief, build_brief_prompt,
                                 build_article_prompt, build_section_rewrite_prompt)
    from synthesis.prompt import build_prompt
    brief = ContentBrief(title="Management", keyword="management", audience="Engineers",
                         format="data_guide", outline=["Licensing"])
    bundle = VerifiedBundle(topic="Management", region=None)
    assert style.TECHNICAL_SCOPE_RULES in build_brief_prompt(
        "Management", "management", "data_guide", "informational", "F5", [], [], [])
    for prompts in [build_article_prompt(brief, bundle, "F5"),
                    build_section_rewrite_prompt("Text", "Licensing", brief, bundle, "F5"),
                    build_prompt(bundle)]:
        assert style.TECHNICAL_SCOPE_RULES in prompts[0]
