"""REACS v1.3 QA risk rule regression tests (V1-V10).

Named after the article or incident that proved each rule, per the standard's
own instruction. These are reported separately from the REACS pillar score and
never folded into it: "Structure and truth are orthogonal."

Severity: everything WARNs except V7c (named_individual), which FAILS. That is
the one rule the standard says ships blocking immediately. The rest warn only
because knowledge-base coverage is still partial — promote them as the data
lands, and expect these tests to need their assertions updated when you do.

V3 (author identity) has no tests here: it is REACS pillar M7, switched off by
decision. See test_reacs_pillars.py.
"""
import pytest

from synthesis.qa_rules import _sentences
from synthesis.validate import (KEYWORD_REPEAT_MAX, extract_version_tokens,
                                _subject_certifications)


class TestV1VersionVerification:
    """V1 BLOCKS as of 25 Aug 2026 — status is "fail", not "warn"."""

    def test_flags_r82_when_the_kb_records_r81_for_ccme(self, qa):
        # CASE 1: the generator reasoned "Check Point ships R82, therefore the
        # CCME tests R82". It does not — Check Point publishes the CCME as R81
        # (exam 156-836, per sk163417).
        c = qa("The CCME exam covers R82 gateways. R82 is current.",
               title="The Complete CCME Certification Guide 2026")
        assert c["version_strings"].status == "fail"
        assert "R82 (x2)" in c["version_strings"].detail

    def test_passes_the_vendors_own_published_version(self, qa):
        # THE STANDARD DOCUMENT IS WRONG HERE, and the gate believed it. The
        # v1.3 pack says to seed CCME as "R81.X"; Check Point's own
        # certification matrix says "Check Point Certified Maestro Expert -
        # R81". V1 matches tokens exactly, so with R81.X recorded an article
        # stating the vendor's OWN version was flagged unverified while the
        # string Check Point does not publish passed. Backwards, on the single
        # row CASE 1's remediation rests on.
        assert qa("The CCME is built on R81.",
                  title="CCME Guide")["version_strings"].status == "pass"

    def test_does_not_accept_a_version_the_vendor_does_not_publish(self, qa):
        assert qa("The CCME is built on R81.X.",
                  title="CCME Guide")["version_strings"].status == "fail"

    def test_passes_an_unrecorded_version_backed_by_a_verbatim_quote(self, qa):
        c = qa("The CCME lab uses R83.", title="CCME Guide",
               quotes=["Check Point confirmed the CCME lab ships on R83."])
        assert c["version_strings"].status == "pass"

    def test_collapses_repeat_occurrences_into_one_finding_with_a_count(self, qa):
        c = qa("R82 " * 48, title="CCME Guide")
        assert "R82 (x48)" in c["version_strings"].detail
        assert c["version_strings"].detail.count("R82") == 1

    def test_ignores_version_like_strings_in_code_blocks_and_urls(self):
        body = "`R82` ```\nR82\n``` [d](https://x.com/R82/) <a href='/r82/'>x</a>"
        assert extract_version_tokens(body, "Check Point") == {}

    def test_does_not_match_dates_prices_or_cve_identifiers(self):
        body = "On 2026-08-19 it cost $8,200 (CVE-2026-8214). Release 82 shipped."
        assert extract_version_tokens(body, "Check Point") == {}

    def test_keying_is_per_certification_not_per_vendor(self, qa):
        # CCSE really is at R82. That must NOT clear a CCME article — this is
        # the entire reason certification_versions is keyed on certification.
        c = qa("CCME now tests R82.", title="CCME Guide")
        assert c["version_strings"].status == "fail"

    def test_ccte_r82_verifies_because_ccte_runs_both(self, qa):
        # Parallel versions during a transition: CCTE runs R81.20 and R82
        # concurrently, so an article citing either is correct.
        c = qa("The CCTE R82 exam covers R82 troubleshooting.",
               title="The Complete CCTE R82 Certification Guide 2026")
        assert c["version_strings"].status == "pass"

    def test_subject_certification_is_taken_from_the_title_first(self, cert_versions):
        assert _subject_certifications("A CCME guide", "mentions CCSE too",
                                       cert_versions) == ["CCME"]

    def test_cannot_verify_a_certification_with_no_recorded_versions(self, qa):
        # THE GAP THIS RULE SHIPPED WITH. An article about a certification we
        # hold no versions for used to fall back to "any version this vendor
        # has" and PASS — which is precisely the vendor-level inference V1
        # exists to refuse. It silently cleared 11 of Check Point's 14
        # certifications: "CCSA tests R82" passed only because CCSE does.
        c = qa("The CCSA tests R82.", title="CCSA Certification Guide",
               all_cert_codes=["CCME", "CCSE", "CCTE", "CCSA"])
        assert c["version_strings"].status == "fail"
        assert "no versions are recorded for CCSA" in c["version_strings"].detail
        assert "does NOT automatically track" in c["version_strings"].detail

    def test_an_uncovered_certification_clears_on_a_verbatim_quote(self, qa):
        c = qa("The CCSA tests R82.", title="CCSA Certification Guide",
               all_cert_codes=["CCME", "CCSE", "CCTE", "CCSA"],
               quotes=["Check Point confirmed the CCSA exam moved to R82."])
        assert c["version_strings"].status == "pass"

    def test_a_release_statement_naming_no_certification_is_not_a_claim(self, qa):
        # "Check Point shipped R82" is a product fact, not a certification
        # claim. Failing product prose would make the rule noise, and noise is
        # how a rule gets switched off.
        c = qa("Check Point shipped R82 this year.", title="Check Point news 2026",
               all_cert_codes=["CCME", "CCSE", "CCTE", "CCSA"])
        assert c["version_strings"].status == "pass"

    def test_an_unrecorded_release_naming_no_certification_still_warns(self, qa):
        c = qa("Check Point shipped R89 this year.", title="Check Point news 2026",
               all_cert_codes=["CCME", "CCSE", "CCTE", "CCSA"])
        assert c["version_strings"].status == "fail"
        assert "read as a product statement" in c["version_strings"].detail

    def test_a_course_release_is_a_verifiable_version(self, qa):
        # CPDA has no exam — it is a course, absent from Check Point's
        # certification matrix. It still carries a version row, because the
        # question the table answers is "what release does this thing target"
        # and a course has a real answer. Without it, an article correctly
        # stating the CPDA course covers R82 was flagged unverifiable: a false
        # positive on true prose.
        c = qa("The CPDA course covers R82.", title="CPDA Guide",
               cert_versions={"CPDA": ["R82"]}, all_cert_codes=["CPDA", "CCME"])
        assert c["version_strings"].status == "pass"

    def test_does_not_run_for_a_vendor_with_no_pattern(self, qa):
        assert "version_strings" not in qa("R82 everywhere", vendor="Avaya")

    @pytest.mark.parametrize("vendor,token", [
        ("Palo Alto Networks", "PAN-OS 11.1"),
        ("Cisco", "IOS-XE 17.9"),
        ("Nutanix", "AOS 6.8"),
        ("F5", "BIG-IP 17.1"),
    ])
    def test_v1_is_check_point_only_by_decision(self, qa, vendor, token):
        # These four version their PRODUCTS, not their exams, and
        # certification_versions has nowhere to put a release that is not tied
        # to a certification. They previously had live patterns and zero rows,
        # so the check never ran while appearing to cover them.
        #
        # If this test fails because someone re-added a pattern: a pattern
        # alone will be silent again. Read the note above VERSION_PATTERNS —
        # vendor-level release rows are what that needs.
        assert "version_strings" not in qa(f"Runs on {token}.", vendor=vendor)

    def test_does_not_run_with_no_recorded_versions(self, qa):
        assert "version_strings" not in qa("R82 everywhere", cert_versions={})


class TestV2ExamCodePresence:
    CODES = {"CCME": ["156-836"]}

    def test_fails_a_certification_article_with_no_exam_code(self, qa):
        c = qa("The CCME is a Maestro expert exam.", title="CCME Guide",
               exam_codes=self.CODES, is_certification=True)
        assert c["exam_code_presence"].status == "warn"
        assert "156-836" in c["exam_code_presence"].detail

    @pytest.mark.parametrize("written", ["156-836", "156836", "156 836"])
    def test_passes_with_or_without_separators(self, qa, written):
        c = qa(f"Book exam {written} at Pearson VUE.", title="CCME Guide",
               exam_codes=self.CODES, is_certification=True)
        assert c["exam_code_presence"].status == "pass"

    def test_any_one_code_satisfies_a_certification_with_two_versions(self, qa):
        # CCSE runs R82 (156-315.82) and R81.20 (156-315.81.20) concurrently.
        # An article about one has no business printing the other's code;
        # requiring every code failed a correct article for omitting an exam
        # it was not about.
        c = qa("Book exam 156-315.82 at Pearson VUE.", title="CCSE R82 Guide",
               exam_codes={"CCSE": ["156-315.82", "156-315.81.20"]},
               is_certification=True)
        assert c["exam_code_presence"].status == "pass"

    def test_still_fails_when_none_of_the_codes_appear(self, qa):
        c = qa("The CCSE is the expert tier.", title="CCSE Guide",
               exam_codes={"CCSE": ["156-315.82", "156-315.81.20"]},
               is_certification=True)
        assert c["exam_code_presence"].status == "warn"
        assert "or" in c["exam_code_presence"].detail

    def test_skips_cleanly_for_non_certification_intent(self, qa):
        # A vendor overview must not fail a pillar that cannot apply.
        assert "exam_code_presence" not in qa(
            "no code here", exam_codes=self.CODES, is_certification=False)


class TestV5BrandCasing:
    def test_flags_check_point_in_the_meta_description(self, qa, vendors):
        # CASE 1's meta description read "check point" — on an authorised
        # partner's SERP snippet.
        c = qa("Fine.", meta="A check point guide.", canonical_names=vendors)
        assert c["brand_casing"].status == "warn"
        assert "meta_description" in c["brand_casing"].detail

    def test_says_title_and_meta_are_what_the_buyer_sees(self, qa, vendors):
        c = qa("Fine.", meta="A check point guide.", canonical_names=vendors)
        assert "buyer and the vendor" in c["brand_casing"].detail

    def test_does_not_flag_correct_casing(self, qa, vendors):
        c = qa("Check Point and Palo Alto Networks.", meta="Check Point guide.",
               canonical_names=vendors)
        assert c["brand_casing"].status == "pass"


class TestV6LocaleSpelling:
    def test_flags_enrollment_and_specialized(self, qa, vendors):
        # Both slipped through a check that already flagged US spellings.
        c = qa("Enrollment is open and the course is specialized.",
               canonical_names=vendors)
        assert c["locale_spelling"].status == "warn"
        # Case-insensitive: the message names the replacement wearing the
        # original's capitalisation ("Enrollment" -> "Enrolment"), because
        # that is the exact substitution the fixer will make.
        assert "enrolment" in c["locale_spelling"].detail.lower()
        assert "specialised" in c["locale_spelling"].detail

    def test_does_not_flag_program(self, qa, vendors):
        # A computer program is a program in Australian English.
        assert qa("Run the program now.",
                  canonical_names=vendors)["locale_spelling"].status == "pass"

    def test_does_not_flag_exempted_vendor_product_names(self, qa, vendors):
        c = qa("Use SmartCenter with the Authorization Server.",
               vendor_terms=["SmartCenter", "Authorization Server"],
               canonical_names=vendors)
        assert c["locale_spelling"].status == "pass"

    def test_does_not_flag_a_us_spelling_inside_a_proper_noun(self, qa, vendors):
        # "AI Data Center Security Architecture" is a Check Point architecture
        # name and was flagged four times on article 1. The KB holds
        # certifications and courses, not architecture names, so a vendor-term
        # list can never be complete — the Title Case shape carries it.
        c = qa("The AI Data Center Security Architecture shipped.",
               canonical_names=vendors)
        assert c["locale_spelling"].status == "pass"

    def test_still_flags_the_same_word_in_ordinary_prose(self, qa, vendors):
        c = qa("Visit the enrollment center today.", canonical_names=vendors)
        assert c["locale_spelling"].status == "warn"

    def test_preserves_the_official_f5_bot_defense_label(self, qa, vendors):
        c = qa("Enable Bot Defense for unwanted automation.",
               vendor="F5", canonical_names=vendors)
        assert c["locale_spelling"].status == "pass"

    def test_defense_outside_the_official_label_is_still_flagged(self, qa, vendors):
        c = qa("Use layered defense for the application.",
               vendor="F5", canonical_names=vendors)
        assert c["locale_spelling"].status == "warn"

    def test_does_not_flag_size_prize_seize(self, qa, vendors):
        c = qa("The prize was seized; check the size.", canonical_names=vendors)
        assert c["locale_spelling"].status == "pass"


class TestV7aAttributionMatch:
    DOMAINS = {"Unit 42": ["paloaltonetworks.com"]}

    def test_flags_a_unit_42_claim_linking_to_a_non_palo_alto_domain(self, qa):
        # The XSOAR "Key facts" block: attributed to Unit 42, linked to a CSO
        # Online listicle. Nothing caught it.
        c = qa("According to Unit 42, attacks rose sharply [S1].",
               refs=[("S1", "https://csoonline.com/x", "listicle", "csoonline.com")],
               entity_domains=self.DOMAINS)
        assert c["attribution_match"].status == "warn"
        assert "csoonline.com" in c["attribution_match"].detail

    def test_passes_when_the_citation_is_the_entitys_own_domain(self, qa):
        c = qa("According to Unit 42, attacks rose [S1].",
               refs=[("S1", "https://unit42.paloaltonetworks.com/x", "t",
                      "unit42.paloaltonetworks.com")],
               entity_domains=self.DOMAINS)
        assert c["attribution_match"].status == "pass"

    def test_matches_a_sentence_initial_capital_lead_in(self, qa):
        # The lead-in regex needs a SCOPED (?i:) — with no flag "According to"
        # at a sentence start never matched; with a plain re.I the entity
        # capture goes case-insensitive and swallows lowercase words.
        c = qa("According to Unit 42, attacks rose [S1].",
               refs=[("S1", "https://csoonline.com/x", "t", "csoonline.com")],
               entity_domains=self.DOMAINS)
        assert c["attribution_match"].status == "warn"


class TestV7bVagueAttribution:
    def test_flags_vague_attribution_with_no_citation(self, qa):
        c = qa("Security professionals report that budgets are rising.")
        assert c["vague_attribution"].status == "warn"

    def test_does_not_flag_the_same_phrase_when_cited(self, qa):
        # Lazy writing, but checkable. QA is not a style guide.
        c = qa("Security professionals report that budgets are rising [S1].")
        assert c["vague_attribution"].status == "pass"


class TestV7cNamedPrivateIndividual:
    """The one rule that BLOCKS. Its false positives cost more than its
    misses, and it has already shipped one — see the regression tests."""

    def test_blocks_a_named_private_individual_with_career_claims(self, qa):
        c = qa("Jordan Fletcher previously worked at a bank and is the CTO of Acme.",
               known_people={"Dan Schultz"})
        assert c["named_individual"].status == "fail"
        assert "Jordan Fletcher" in c["named_individual"].detail

    def test_allows_the_byline_instructor_to_be_named(self, qa):
        c = qa("Dan Schultz previously worked at Check Point.",
               known_people={"Dan Schultz"})
        assert c["named_individual"].status == "pass"

    def test_a_name_with_no_career_claim_is_not_a_finding(self, qa):
        c = qa("Jordan Fletcher wrote the reference guide.", known_people=set())
        assert c["named_individual"].status == "pass"

    def test_does_not_pair_a_heading_with_the_next_paragraph(self, qa):
        # LIVE BLOCKING FALSE POSITIVE, article 2. "### ...Specialisations\n
        # For those focused..." was read as one sentence and "Specialisations
        # For" was reported as a person. The editor was told to remove a name
        # that does not exist — unfollowable feedback on a blocking rule.
        body = ("### Troubleshooting and Specialisations\n"
                "For those focused on resolution, the CCTA serves as a gateway.")
        assert qa(body, known_people=set())["named_individual"].status == "pass"

    def test_does_not_fire_on_the_approved_cta_phrase(self, qa):
        # "Advance your career with Red Education" is an APPROVED CTA present
        # in every article by design, and it matched "career with".
        body = "Advance your career with Red Education and get started today."
        assert qa(body, known_people=set())["named_individual"].status == "pass"

    def test_does_not_pair_a_name_with_a_distant_career_phrase(self, qa):
        body = ("Jordan Fletcher wrote the guide. " + "Filler text here. " * 12
                + "The CCTA serves as a gateway.")
        assert qa(body, known_people=set())["named_individual"].status == "pass"

    def test_does_not_flag_an_organisation_name_pair(self, qa):
        body = "Check Point previously worked with Cortex XSOAR on this."
        assert qa(body, known_people=set())["named_individual"].status == "pass"


class TestV8InterestedPartySourcing:
    def test_flags_a_claim_supported_only_by_a_competitor_domain(self, qa):
        # A claim about Palo Alto's XSOAR roadmap sourced only from D3
        # Security, who sell a competing SOAR product. A lead requiring
        # primary-source confirmation, not a citation.
        c = qa("XSOAR's roadmap is winding down [S1].",
               refs=[("S1", "https://d3security.com/x", "t", "d3security.com")],
               interested_domains={"d3security.com"})
        assert c["interested_party"].status == "warn"

    def test_does_not_flag_once_a_primary_vendor_source_is_attached(self, qa):
        c = qa("XSOAR's roadmap is winding down [S1][S2].",
               refs=[("S1", "https://d3security.com/x", "t", "d3security.com"),
                     ("S2", "https://paloaltonetworks.com/y", "t",
                      "paloaltonetworks.com")],
               interested_domains={"d3security.com"})
        assert c["interested_party"].status == "pass"


class TestV4PromiseGaps:
    def test_flags_a_weightings_heading_with_no_percentage(self, qa):
        # CASE 1: a heading promising domain weightings that the section never
        # gave. A release number is not a weighting — the reference
        # implementation's bare /\d/ test passed this because it said "R82".
        c = qa("## Exam Domains and Weighting\nManaged by the vendor on R82.\n")
        assert c["promise_gaps"].status == "warn"

    def test_passes_a_weightings_heading_with_a_percentage_table(self, qa):
        body = "## Exam Domains and Weighting\n| D | W |\n| --- | --- |\n| Policy | 35% |\n"
        assert qa(body)["promise_gaps"].status == "pass"

    def test_ignores_a_heading_that_promises_no_quantity(self, qa):
        assert qa("## What the exam covers\nNo numbers.\n")["promise_gaps"].status == "pass"


class TestV9KeywordRepetition:
    def test_flags_more_than_the_ceiling(self, qa):
        # Every article measured came in at 6-11, because the writer prompt
        # asked for "roughly 10-14 times". The prompt and this ceiling must
        # move together.
        c = qa("ccme certification " * 6, keywords=["ccme certification"])
        assert c["keyword_repetition"].status == "warn"

    def test_passes_exactly_the_ceiling(self, qa):
        c = qa("ccme certification " * KEYWORD_REPEAT_MAX,
               keywords=["ccme certification"])
        assert c["keyword_repetition"].status == "pass"

    def test_does_not_run_without_keywords(self, qa):
        assert "keyword_repetition" not in qa("x")


class TestV10CorpusContradiction:
    CORPUS = {"CCME": {"R81.X": "The 2026 CCME Study Guide"}}

    def test_flags_a_version_claim_another_article_contradicts(self, qa):
        c = qa("The CCME tests R82 throughout.", title="CCME Guide",
               corpus_versions=self.CORPUS)
        assert c["corpus_consistency"].status == "warn"
        assert "R81.X" in c["corpus_consistency"].detail

    def test_passes_when_the_estate_agrees(self, qa):
        c = qa("The CCME tests R81.X.", title="CCME Guide",
               corpus_versions=self.CORPUS)
        assert c["corpus_consistency"].status == "pass"


class TestTitleReadabilityNamesWhatItJudged:
    """It reads meta_title, which is NOT the article heading.

    Live confusion this fixes: an H1 reading "The Complete Prisma Access SSE
    Certification Guide 2026: A Valuable Roadmap" carries both a power word
    and a sentiment word, while the 52-character SEO title beneath it carried
    only "Expert". The finding said "no sentiment word", the reviewer looked
    at the heading, and it took a database query to establish which string had
    actually been checked.
    """

    H1 = "The Complete X Guide 2026: A Valuable Roadmap"
    SEO_MISSING_SENTIMENT = "Prisma Access SSE Certification 2026: Expert Roadmap"
    SEO_COMPLETE = "Prisma Access SSE Certification 2026: Best Expert Roadmap"

    def test_the_warning_quotes_the_seo_title(self, qa):
        c = qa("body", title=self.H1, seo=self.SEO_MISSING_SENTIMENT,
               keywords=["prisma access sse certification"])["title_readability"]
        assert c.status == "warn"
        assert self.SEO_MISSING_SENTIMENT in c.detail

    def test_the_warning_says_it_is_not_the_heading(self, qa):
        c = qa("body", title=self.H1, seo=self.SEO_MISSING_SENTIMENT,
               keywords=["prisma access sse certification"])["title_readability"]
        assert "not the article heading" in c.detail

    def test_the_pass_quotes_it_too(self, qa):
        c = qa("body", title=self.H1, seo=self.SEO_COMPLETE,
               keywords=["prisma access sse certification"])["title_readability"]
        assert c.status == "pass"
        assert self.SEO_COMPLETE in c.detail

    def test_one_failure_is_not_pluralised(self, qa):
        c = qa("body", title=self.H1, seo=self.SEO_MISSING_SENTIMENT,
               keywords=["prisma access sse certification"])["title_readability"]
        assert "fails 1 Rank Math check:" in c.detail


class TestNearMissLinksReadAsOneProblem:
    """Two findings, one defect, and they looked contradictory.

    Live on article 5: the brief asked for
    ".../palo-alto-networks-certification-restructure-2026/" and the writer
    produced ".../palo-alto-networks/certification-restructure-2026/" — a
    slash where a hyphen belongs. internal_links then said the requested link
    was MISSING while internal_link_targets said the one present was INVENTED.
    Both true; together they read as "add this" and "that is fake" about the
    same URL, and the editor is left hunting for something already in front of
    them.
    """

    WANT = "https://www.rededucation.com/palo-alto-networks-certification-restructure-2026/"
    GOT = "https://www.rededucation.com/palo-alto-networks/certification-restructure-2026/"

    def _checks(self, qa):
        body = f"Text [anchor]({self.GOT}) and [x](/palo-alto-networks/)."
        return qa(body, internal_links=[self.WANT],
                  allowed_link_urls={"https://www.rededucation.com/palo-alto-networks/"})

    def test_missing_link_says_the_body_has_a_near_variant(self, qa):
        d = self._checks(qa)["internal_links"].detail
        assert "almost the requested" in d
        assert "Correct the URL rather than adding a second link" in d

    def test_invented_target_points_at_the_requested_url(self, qa):
        d = self._checks(qa)["internal_link_targets"].detail
        assert "looks like a mistyped" in d
        assert "clears the internal_links finding as well" in d

    def test_a_genuinely_unrelated_url_gets_no_near_miss_hint(self, qa):
        body = "Text [anchor](https://www.rededucation.com/something-else/)."
        c = qa(body, internal_links=[self.WANT],
               allowed_link_urls={"https://www.rededucation.com/palo-alto-networks/"})
        assert "almost the requested" not in c["internal_links"].detail

    def test_slug_key_ignores_only_punctuation(self):
        from synthesis.validate import _slug_key
        assert _slug_key(self.WANT) == _slug_key(self.GOT)
        assert _slug_key("https://x.com/a-b/") != _slug_key("https://x.com/a-c/")

    # ---- the hint must not invent a finding -----------------------------
    # Article 5 again, one deploy later. The SAME brief also asked for
    # ".../palo-alto-networks/prisma-access-sse-configuration-and-deployment/"
    # and the writer linked it exactly right. The hint fired on it anyway,
    # because it looped over by_url — whose keys have had their trailing
    # slash stripped — so a correct link compared unequal to the requested
    # one and the editor was told to go and fix it.
    OK = ("https://www.rededucation.com/palo-alto-networks/"
          "prisma-access-sse-configuration-and-deployment/")

    def test_a_correctly_written_link_is_never_called_mistyped(self, qa):
        body = (f"Text [anchor]({self.GOT}) and [ok]({self.OK}).")
        d = qa(body, internal_links=[self.WANT, self.OK],
               allowed_link_urls={self.OK}
               )["internal_link_targets"].detail
        assert "prisma-access" not in d
        assert d.count("looks like a mistyped") == 1

    def test_no_hint_when_the_requested_link_is_present(self, qa):
        # Nothing is missing, so there is no internal_links finding to
        # clear — promising to clear one sends the editor looking for it.
        body = f"Text [ok]({self.OK}) and [root](/)."
        d = qa(body, internal_links=[self.OK],
               allowed_link_urls={self.OK})["internal_link_targets"].detail
        assert "clears the internal_links finding" not in d

    def test_a_bare_trailing_slash_difference_is_not_an_invention(self, qa):
        # Same page, written without its trailing slash. internal_links
        # explains that from its own side; this check must stay quiet.
        body = f"Text [ok]({self.OK.rstrip('/')}) and [root](/)."
        d = qa(body, internal_links=[self.OK],
               allowed_link_urls={self.OK})["internal_link_targets"].detail
        assert "looks like a mistyped" not in d


class TestUnresolvedFixesAreRemembered:
    """Warn before the spend, not after it.

    The flash naming a check that did not clear only helps the person who has
    already paid for the call. Three consecutive attempts on one check went
    unnoticed because nothing on the panel said the previous one had failed.
    """

    def test_fix_issues_records_and_clears_per_check(self):
        # Structural: the counter must go UP for a check still failing and be
        # REMOVED for one that cleared, or a stale warning outlives the problem.
        import inspect
        from webapp.pipeline import fix_issues
        src = inspect.getsource(fix_issues)
        assert "history[name] = int(history.get(name, 0)) + 1" in src
        assert "history.pop(name, None)" in src
        assert "article.unresolved_fixes = history or None" in src

    def test_a_full_rewrite_clears_the_history(self):
        # The text is new, so old attempts describe prose that no longer
        # exists — warning about it would be worse than saying nothing.
        import inspect
        from webapp.pipeline import write_article
        assert "article.unresolved_fixes = None" in inspect.getsource(write_article)


class TestEveryWritableFieldIsVisibleToTheWriter:
    """THE INVARIANT THAT WOULD HAVE CAUGHT THE TITLE BUG.

    fix_issues() writes several fields back from the model's reply. Any field
    it can WRITE, the revision prompt must SHOW — otherwise a fix aimed at
    that field asks the model to correct something it has never seen, and it
    regenerates blind. That is exactly how title_readability survived three
    consecutive "Fix selected" attempts, and the same trap was sitting unsprung
    for the FAQ and the CTA.

    If this test fails, someone added a writable field without adding it to
    build_article_prompt's revision block. Add it there rather than relaxing
    the test.
    """

    WRITABLE = ["meta_title", "meta_description", "excerpt", "cta", "faq"]

    def _prompt(self, fields):
        from retrieval.models import VerifiedBundle
        from synthesis.brief import BriefFaq, ContentBrief, build_article_prompt
        brief = ContentBrief(title="T", audience="a", keyword="k",
                             format="explainer", outline=["A", "B", "C"],
                             key_points=["p"], internal_links=[],
                             faq=[BriefFaq(question="q?", answer_hint="h")],
                             cta="c", image_brief="i")
        _s, user, _t = build_article_prompt(
            brief, VerifiedBundle(topic="t", region=None), "Vendor",
            feedback="- [WARN] something: ...", previous_content="BODY",
            previous_fields=fields)
        return user

    def test_fix_issues_writes_only_fields_this_test_knows_about(self):
        # Guards the guard: if fix_issues starts writing a new field, this
        # list has to grow with it, and the test below then enforces it.
        import inspect, re
        from webapp.pipeline import fix_issues
        written = set(re.findall(r"article\.([a-z_]+) = result\.",
                                 inspect.getsource(fix_issues)))
        unknown = written - set(self.WRITABLE) - {"content", "word_count"}
        assert not unknown, (
            f"fix_issues writes {sorted(unknown)} but the revision prompt is "
            "not asserted to show it — add it to WRITABLE and to "
            "build_article_prompt's revision block")

    def test_every_writable_field_appears_in_the_revision_prompt(self):
        markers = {"meta_title": "MT-MARKER", "meta_description": "MD-MARKER",
                   "excerpt": "EX-MARKER", "cta": "CTA-MARKER"}
        fields = dict(markers)
        fields["faq"] = [{"question": "FAQQ-MARKER", "answer": "FAQA-MARKER"}]
        user = self._prompt(fields)
        for field, marker in markers.items():
            assert marker in user, f"{field} is writable but never shown"
        assert "FAQQ-MARKER" in user and "FAQA-MARKER" in user, "faq not shown"

    def test_the_block_is_omitted_when_there_is_nothing_to_show(self):
        assert "YOUR PREVIOUS NON-BODY FIELDS" not in self._prompt(None)
        assert "YOUR PREVIOUS NON-BODY FIELDS" not in self._prompt({})


class TestTitleFixIsActuallyActionable:
    """Fix selected on title_readability failed three times in a row, live.

    Two causes, both "the model cannot act on this":
      1. build_article_prompt handed back only the BODY, so a fix aimed at the
         SEO title asked the model to correct a field it was never shown. It
         regenerated meta_title blind each time and landed somewhere similar.
      2. The feedback said only that a Rank Math check failed, leaving the
         model to guess which of four elements to add inside a 60-character
         cap it also could not see.
    """

    def test_the_revision_prompt_shows_the_current_meta_fields(self):
        from retrieval.models import VerifiedBundle
        from synthesis.brief import BriefFaq, ContentBrief, build_article_prompt
        brief = ContentBrief(title="T", audience="a", keyword="k",
                             format="explainer", outline=["A", "B", "C"],
                             key_points=["p"], internal_links=[],
                             faq=[BriefFaq(question="q?", answer_hint="h")],
                             cta="c", image_brief="i")
        _s, user, _t = build_article_prompt(
            brief, VerifiedBundle(topic="t", region=None), "Palo Alto Networks",
            feedback="- [WARN] title_readability: ...",
            previous_content="## Body",
            previous_fields={"meta_title": "SEO title here",
                             "meta_description": "Meta description here"})
        assert "SEO title here" in user
        assert "Meta description here" in user

    def test_the_meta_block_is_omitted_when_nothing_is_supplied(self):
        from retrieval.models import VerifiedBundle
        from synthesis.brief import BriefFaq, ContentBrief, build_article_prompt
        brief = ContentBrief(title="T", audience="a", keyword="k",
                             format="explainer", outline=["A", "B", "C"],
                             key_points=["p"], internal_links=[],
                             faq=[BriefFaq(question="q?", answer_hint="h")],
                             cta="c", image_brief="i")
        _s, user, _t = build_article_prompt(
            brief, VerifiedBundle(topic="t", region=None), "Palo Alto Networks",
            feedback="x", previous_content="## Body")
        assert "YOUR PREVIOUS NON-BODY FIELDS" not in user

    def test_the_retry_feedback_names_the_field_and_the_cap(self):
        from synthesis.models import QAReport
        r = QAReport()
        r.add("title_readability", "warn", "no sentiment word")
        line = r.feedback_block()
        assert "meta_title field" in line
        assert "60 characters" in line
        assert "TWO SEPARATE checks" in line


class TestSentenceSplitting:
    def test_newlines_are_sentence_boundaries(self):
        # Splitting on punctuation alone glued a heading to the paragraph
        # under it, which is what produced V7c's blocking false positive.
        assert _sentences("### A heading\nFor those focused on it.") == [
            "### A heading", "For those focused on it."]


class TestVendorGrounding:
    """V11. A certification guide that never read the vendor's own site.

    The article that proved it was headlined "The Complete Prisma Access SSE
    Certification Guide 2026" and was written from a Fortinet page comparing
    Fortinet's firewall to Palo Alto's, plus a NIST zero-trust PDF. It scored
    REACS 100 and QA 94 while inventing the credential: there is no "Prisma
    Access SSE certification" — it is the Palo Alto Networks Certified
    Security Service Edge Engineer, and "Prisma Access SSE: Configuration and
    Deployment" is the course. Both are on Palo Alto's own site, on the page
    that ranks first for the article's own keyword.
    """

    PAN = {"paloaltonetworks.com", "unit42.paloaltonetworks.com"}

    def test_a_cert_guide_with_no_vendor_source_fails(self, qa):
        c = qa("Body about the exam.", is_certification=True,
               vendor="Palo Alto Networks", vendor_domains=self.PAN,
               refs=[("S1", "https://www.fortinet.com/x", "NGFW compare",
                      "www.fortinet.com")])
        assert c["vendor_grounding"].status == "fail"

    def test_the_failure_names_what_it_was_written_from_instead(self, qa):
        # "Add a vendor source" is not actionable; "you used Fortinet" is.
        d = qa("Body.", is_certification=True, vendor="Palo Alto Networks",
               vendor_domains=self.PAN,
               refs=[("S1", "https://www.fortinet.com/x", "NGFW compare",
                      "www.fortinet.com")]
               )["vendor_grounding"].detail
        assert "fortinet.com" in d
        assert "paloaltonetworks.com" in d

    def test_a_vendor_source_clears_it(self, qa):
        c = qa("Body.", is_certification=True, vendor="Palo Alto Networks",
               vendor_domains=self.PAN,
               refs=[("S1", "https://www.paloaltonetworks.com/c", "SSE Engineer",
                      "www.paloaltonetworks.com")])
        assert c["vendor_grounding"].status == "pass"

    def test_a_subdomain_of_the_vendor_counts(self, qa):
        c = qa("Body.", is_certification=True, vendor="Palo Alto Networks",
               vendor_domains={"paloaltonetworks.com"},
               refs=[("S1", "https://unit42.paloaltonetworks.com/x", "Unit 42",
                      "unit42.paloaltonetworks.com")])
        assert c["vendor_grounding"].status == "pass"

    def test_a_non_certification_article_is_not_graded(self, qa):
        # A salary guide legitimately rests on aggregators, not the vendor.
        c = qa("Body.", is_certification=False, vendor="Palo Alto Networks",
               vendor_domains=self.PAN,
               refs=[("S1", "https://www.fortinet.com/x", "NGFW compare",
                      "www.fortinet.com")])
        assert "vendor_grounding" not in c

    def test_a_vendor_with_no_domains_on_record_switches_the_rule_off(self, qa):
        # The QaKnowledge contract: an empty field means the rule cannot
        # grade, not that everything fails.
        c = qa("Body.", is_certification=True, vendor="Obscure Vendor",
               vendor_domains=set(),
               refs=[("S1", "https://www.fortinet.com/x", "NGFW compare",
                      "www.fortinet.com")])
        assert "vendor_grounding" not in c


class TestExamCodeNoLongerFailsOpen:
    def test_a_cert_guide_warns_when_the_kb_holds_no_code_at_all(self, qa):
        # Previously silent, which meant the LESS the KB knew, the cleaner the
        # article scored. Absence of data is not evidence of correctness.
        c = qa("Body with no codes.", is_certification=True,
               vendor="Palo Alto Networks", exam_codes={})
        assert c["exam_code_presence"].status == "warn"
        assert "could not be checked" in c["exam_code_presence"].detail


class TestSpellingIsFixedNotJustReported:
    """locale_spelling burned three targeted fixes on one word.

    "organizations" for "organisations", in a paragraph the model had no other
    reason to touch — so the patch never carried that block and the check
    never cleared. Three LLM calls on a string replacement. The correction is
    deterministic and now happens before the prompt is built.

    The invariant that makes it safe: the fixer and the checker read their
    exemptions from ONE function. A fixer with looser rules would "correct"
    SmartCenter into SmartCentre, which is far worse than the spelling it set
    out to fix.
    """
    VENDOR_TERMS = ["SmartCenter", "Authorization Server", "Prisma Access"]

    def _round_trip(self, text):
        # Module functions, not class attributes: assigning a function to a
        # class attribute makes it a bound method and passes self as the
        # first argument.
        from synthesis.qa_rules import us_spellings
        from synthesis.text import fix_writing_mechanics
        fixed = fix_writing_mechanics(text, None, self.VENDOR_TERMS)
        return fixed, us_spellings(fixed, self.VENDOR_TERMS)

    def test_anything_the_checker_flags_the_fixer_fixes(self):
        fixed, remaining = self._round_trip(
            "This helps organizations optimize and analyze their enrollment.")
        assert remaining == [], f"fixer left {remaining} behind"
        assert "organisations" in fixed

    def test_the_fixer_leaves_alone_what_the_checker_exempts(self):
        # The dangerous direction. Every one of these is a product name.
        text = "SmartCenter and the Authorization Server run Prisma Access."
        fixed, _ = self._round_trip(text)
        assert fixed == text

    def test_capitalisation_is_preserved(self):
        # _au_form lowercases; harmless in a report, corrupting in a fixer.
        fixed, _ = self._round_trip("Organizations matter.")
        assert fixed.startswith("Organisations")

    def test_code_and_urls_are_untouched(self):
        text = "See `optimize_flag` and https://x.com/optimize-organizations here."
        fixed, _ = self._round_trip(text)
        assert fixed == text

    def test_every_occurrence_is_corrected_not_only_the_first(self):
        # The check reports one finding per distinct word; the fixer must
        # still replace all of them, or the check fires again next run.
        fixed, remaining = self._round_trip(
            "Organizations differ. Other organizations agree. All organizations.")
        assert "organiz" not in fixed.lower()
        assert remaining == []


class TestProductAndAcronymCasing:
    """"The cortex xsiam vs siem migration is..." shipped as an opening line.

    Structural, not careless: the target keyword is stored in lower case, and
    both REACS's direct_answer and QA's keyword_coverage require it VERBATIM
    near the start, so the writer pastes back exactly what it was handed.

    Nothing caught it. known_acronyms only read parenthesised acronyms, whole
    names and the exam_code column, so XSIAM — which appears mid-name in
    "Cortex XSIAM: Security Operations" — was not in the set, and SIEM cannot
    come from a vendor catalogue at all. The LLM proofread missed it too, and
    always would: it is handed the body and a vendor name, never the KB, so it
    has no ground truth for how these are spelled.
    """

    ENTITIES = ["Cortex XSIAM: Security Operations, Integration, and Automation",
                "Prisma Access SSE: Configuration and Deployment",
                "Panorama: Centralized Network Security Administration"]
    ACRONYMS = {"XSIAM", "SIEM", "SSE", "CCSE"}

    def _fix(self, text):
        from synthesis.text import fix_writing_mechanics
        return fix_writing_mechanics(text, self.ACRONYMS, self.ENTITIES)

    def test_the_opening_line_that_shipped_is_corrected(self):
        out = self._fix("The cortex xsiam vs siem migration is the transition")
        assert out.startswith("The Cortex XSIAM vs SIEM migration")

    def test_a_multi_word_product_name_is_cased_from_the_kb(self):
        assert "Prisma Access SSE" in self._fix("prisma access sse is useful")

    def test_a_single_word_entity_is_left_alone(self):
        # "Panorama" is an ordinary noun as often as a product. Case-correcting
        # one mid-sentence is a worse error than leaving it.
        assert self._fix("a panorama of options") == "a panorama of options"

    def test_an_ordinary_word_is_never_uppercased(self):
        # "BIG" reached the acronym set from "Configuring BIG IP Access Policy
        # Manager" — BIG-IP typed without its hyphen — and would have
        # published "a BIG improvement".
        from synthesis.text import fix_writing_mechanics
        assert "a big win" in fix_writing_mechanics(
            "a big win", {"BIG", "XSIAM"}, [])

    def test_the_reporter_names_what_the_fixer_would_change(self):
        # Symmetry: anything the fixer repairs, the check must be able to
        # report, or a residual it could not reach goes unmentioned.
        from synthesis.text import writing_mechanics_issues
        found = writing_mechanics_issues("the cortex xsiam platform",
                                         self.ACRONYMS, self.ENTITIES)
        assert any("Cortex XSIAM" in f for f in found)

    def test_nothing_is_reported_once_the_fixer_has_run(self):
        from synthesis.text import writing_mechanics_issues
        fixed = self._fix("the cortex xsiam vs siem migration")
        assert writing_mechanics_issues(fixed, self.ACRONYMS, self.ENTITIES) == []


class TestAcronymHarvesting:
    def test_an_acronym_inside_a_mixed_case_name_is_found(self):
        from config.db import get_session
        from synthesis.gaps import known_acronyms
        with get_session() as s:
            acr = known_acronyms(s)
        # These are the ones the article needed and never had.
        for a in ("SIEM", "SSE"):
            assert a in acr, f"{a} missing"

    def test_an_all_caps_name_does_not_donate_its_words(self):
        # add()'s own docstring warns about this: an all-caps catalogue entry
        # would otherwise make ESSENTIALS an acronym and we would publish
        # "the ESSENTIALS of".
        from synthesis.gaps import _NEVER_AN_ACRONYM
        assert "BIG" in _NEVER_AN_ACRONYM
        assert "IT" in _NEVER_AN_ACRONYM


class TestHeadingLength:
    """A 500-character H2 shipped at QA 97 / REACS 100.

    The model wrote the heading and its paragraph on ONE line, and markdown
    runs a heading to the end of the line, so the whole paragraph became the
    heading. On the live site it renders as a wall of giant capitals.

    Nothing looked at heading length: structure_length counts H2s, and a
    500-character H2 still counts as one. Measured before choosing a bar:
    across the estate's 115 headings the median is 42 characters, the 90th
    percentile 57, and exactly one is above 80 — this one.
    """

    WRECK = ("Key architectural benefits of Panorama provide the core "
             "functionality needed to manage high-volume security environments "
             "where manual device-by-device configuration is no longer "
             "feasible. By using device groups and templates, administrators "
             "can maintain a hierarchical approach.")

    def test_a_paragraph_masquerading_as_a_heading_blocks(self, qa):
        c = qa(f"## {self.WRECK}\n\nBody text follows here.")
        assert c["heading_length"].status == "fail"

    def test_an_ordinary_heading_passes(self, qa):
        c = qa("## Key architectural benefits of Panorama\n\nBody text.")
        assert c["heading_length"].status == "pass"

    def test_the_longest_real_heading_on_the_estate_still_passes(self, qa):
        # 71 characters — the longest anyone actually wrote.
        h = "How does centralized security administration change network management?"
        assert qa(f"## {h}\n\nBody.")["heading_length"].status == "pass"

    def test_it_names_the_outline_item_as_the_replacement(self, qa):
        # The model expands an outline item into a sentence, so the outline
        # item IS the fix. Saying so turns a judgement into a substitution.
        d = qa(f"## {self.WRECK}\n\nBody.",
               outline=["Key architectural benefits of Panorama",
                        "Certification roadmap and exam logistics"]
               )["heading_length"].detail
        assert '"Key architectural benefits of Panorama"' in d
        assert "move the rest into a paragraph" in d

    def test_without_an_outline_it_still_says_what_to_do(self, qa):
        d = qa(f"## {self.WRECK}\n\nBody.")["heading_length"].detail
        assert "Cut it to a short label" in d

    def test_it_explains_the_cause_not_just_the_symptom(self, qa):
        # "Heading too long" sends someone hunting for a style rule. The real
        # cause is a missing newline, and knowing that makes the fix obvious.
        d = qa(f"## {self.WRECK}\n\nBody.")["heading_length"].detail
        assert "same line" in d

    def test_an_article_with_no_headings_is_not_graded(self, qa):
        assert "heading_length" not in qa("Just prose, no headings at all.")

    def test_a_long_h3_is_caught_too(self, qa):
        assert qa(f"### {self.WRECK}\n\nBody.")["heading_length"].status == "fail"


class TestSplittingAnOverlongHeadingDeterministically:
    """The model would not do it. It does not need to.

    Asked three times to split a 500-character heading, it returned the same
    500-character heading with "provide" changed to "provides". Where the
    brief's outline names the heading, the repair is exactly the blank line
    that should have been there — a substitution, not a judgement.
    """
    OUTLINE = ["Key architectural benefits of Panorama",
               "Certification roadmap and exam logistics"]
    WRECK = ("## Key architectural benefits of Panorama provide the core "
             "functionality needed to manage high-volume security environments "
             "where manual device-by-device configuration is no longer "
             "feasible. By using device groups and templates, administrators "
             "can maintain a hierarchical approach that ensures global "
             "policies are applied.")

    _UNSET = object()

    def _split(self, content, outline=_UNSET):
        # `is _UNSET`, not `or`: an EMPTY outline is a case under test and
        # must not fall back to the default one.
        from synthesis.validate import split_overlong_headings
        return split_overlong_headings(
            content, self.OUTLINE if outline is self._UNSET else outline)

    def test_the_heading_becomes_the_outline_item(self):
        out = self._split(self.WRECK)
        assert out.splitlines()[0] == "## Key architectural benefits of Panorama"

    def test_the_prose_becomes_a_paragraph_beneath_it(self):
        out = self._split(self.WRECK)
        body = out.split("\n\n", 1)[1]
        assert body.startswith("provide the core functionality")
        assert not body.startswith("#")

    def test_the_result_passes_the_check_it_was_failing(self, qa):
        out = self._split(self.WRECK)
        assert qa(out + "\n\nMore body.", outline=self.OUTLINE
                  )["heading_length"].status == "pass"

    def test_a_normal_heading_is_untouched(self):
        text = "## Key architectural benefits of Panorama\n\nBody text here."
        assert self._split(text) == text

    def test_nothing_happens_without_an_outline(self):
        # Where to cut is a judgement without the outline, and a wrong guess
        # invents a heading — worse than leaving the finding for a human.
        assert self._split(self.WRECK, outline=[]) == self.WRECK

    def test_an_unrelated_outline_leaves_it_alone(self):
        assert self._split(self.WRECK, outline=["Something else entirely"]) \
            == self.WRECK

    def test_a_heading_that_is_exactly_the_outline_item_is_untouched(self):
        # Nothing to move: splitting would leave an empty paragraph.
        text = "## " + "Key architectural benefits of Panorama"
        assert self._split(text) == text

    def test_other_headings_in_the_document_survive(self):
        doc = ("# Title\n\n" + self.WRECK + "\n\n## Certification roadmap and "
               "exam logistics\n\nBody.")
        out = self._split(doc)
        assert "# Title" in out
        assert "## Certification roadmap and exam logistics" in out


class TestTableSubstance:
    """A table looks like reference data whether or not it holds any.

    This shipped under the heading "Panorama Certification and Exam Logistics"
    at QA 97, and a reader could book nothing against it — no exam code, no
    duration, no question count, no fee. Every check passed it, because a
    table with rows is a table with rows.
    """

    WRECK = (
        "| Certification/Course | Focus Area | Logistics |\n"
        "|---|---|---|\n"
        "| EDU-210 | Firewall Essentials | Single-device configuration |\n"
        "| Panorama Training | Multi-device Orchestration | Enterprise-wide policy |\n"
        "| Certification Exam | Device Groups/Templates | Proctored evaluation |\n")

    def test_the_table_that_shipped_is_flagged(self, qa):
        assert qa(self.WRECK)["table_substance"].status == "warn"

    def test_the_message_names_the_column_and_what_is_in_it(self, qa):
        d = qa(self.WRECK)["table_substance"].detail
        assert '"Logistics"' in d
        assert "Single-device configuration" in d

    def test_a_logistics_column_with_real_figures_passes(self, qa):
        good = ("| Exam | Logistics |\n|---|---|\n"
                "| PCNSE | 75 questions, 80 minutes, $175 |\n")
        assert qa(good)["table_substance"].status == "pass"

    def test_a_cost_column_of_prose_is_flagged(self, qa):
        t = ("| Course | Cost |\n|---|---|\n"
             "| EDU-210 | Contact us for pricing |\n")
        assert qa(t)["table_substance"].status == "warn"

    def test_a_column_promising_nothing_is_not_graded(self, qa):
        t = ("| Feature | Benefit |\n|---|---|\n"
             "| Device Groups | Logical grouping of firewalls |\n")
        assert qa(t)["table_substance"].status == "pass"

    def test_a_release_number_is_not_a_figure(self, qa):
        # Same rule V4 already applies: "R82" is not a duration or a fee.
        t = "| Exam | Duration |\n|---|---|\n| CCSE | R82 |\n"
        assert qa(t)["table_substance"].status == "warn"

    def test_an_article_with_no_table_is_not_graded(self, qa):
        assert "table_substance" not in qa("Just prose, no tables at all.")

    def test_the_delimiter_row_is_not_read_as_data(self, qa):
        # "|---|---|" in the Logistics column would otherwise look like
        # content and could mask an empty column.
        t = "| Exam | Duration |\n|---|---|\n| CCSE | 90 minutes |\n"
        assert qa(t)["table_substance"].status == "pass"


class TestLinkRelevance:
    """internal_link_targets asks whether a link points where its anchor says.
    That is correctness, not relevance.

    A firewall-troubleshooting article carried links to Cortex XSIAM, Cortex
    XDR and Panorama and passed every link check. Measured on it: XSIAM
    appeared 4 times and XDR twice, NOT ONCE outside a link anchor, while
    EDU-330 appeared 5 times in the prose and "packet" 19 times. A link whose
    subject the prose never names was inserted, not earned.
    """
    ENTITIES = ["Cortex XSIAM: Security Operations, Integration, and Automation",
                "EDU-330 Firewall : Troubleshooting"]

    def test_a_link_to_something_never_discussed_is_flagged(self, qa):
        body = ("This guide covers packet flow on the firewall. "
                "See [Palo Alto Networks Cortex XSIAM: Security Operations]"
                "(https://www.rededucation.com/x/) for more.")
        c = qa(body, entities=self.ENTITIES)
        assert c["link_relevance"].status == "warn"
        assert "Cortex XSIAM" in c["link_relevance"].detail

    def test_a_link_the_prose_earns_passes(self, qa):
        body = ("EDU-330 Firewall Troubleshooting teaches packet flow. The "
                "EDU-330 syllabus covers session tables. "
                "[EDU-330 Firewall : Troubleshooting]"
                "(https://www.rededucation.com/y/)")
        assert qa(body, entities=self.ENTITIES)["link_relevance"].status == "pass"

    def test_a_mention_inside_another_link_does_not_count(self, qa):
        # The whole defect: the subject appears ONLY in link anchors, which is
        # what makes it an insertion rather than a topic.
        body = ("Firewall basics here. "
                "[Cortex XSIAM: Security Operations](https://www.rededucation.com/a/) "
                "and [Cortex XSIAM again](https://www.rededucation.com/b/)")
        assert qa(body, entities=self.ENTITIES)["link_relevance"].status == "warn"

    def test_a_generic_vendor_link_is_not_graded(self, qa):
        # "Palo Alto Networks training" names no KB entity, so there is no
        # subject to require. Generic vendor links are legitimate.
        body = ("Packet flow matters. [Palo Alto Networks training]"
                "(https://www.rededucation.com/palo-alto-networks/)")
        assert qa(body, entities=self.ENTITIES)["link_relevance"].status == "pass"

    def test_an_article_with_no_internal_links_is_not_graded(self, qa):
        assert "link_relevance" not in qa("Prose with no links at all.",
                                          entities=self.ENTITIES)


class TestCrossVendorLinks:
    def test_an_unexplained_other_vendor_link_blocks(self, qa):
        body = ("Nutanix administration is the subject. "
                "See [Cisco DEVASC training]"
                "(https://www.rededucation.com/cisco/devasc/).")
        check = qa(body, vendor="Nutanix",
                   canonical_names=["Nutanix", "Cisco"])["cross_vendor_links"]
        assert check.status == "fail"
        assert "Cisco" in check.detail

    def test_a_substantive_comparison_can_link_the_other_vendor(self, qa):
        body = ("Cisco and Nutanix take different approaches to automation. "
                "The Cisco curriculum is compared here before the "
                "[Cisco DEVASC training]"
                "(https://www.rededucation.com/cisco/devasc/).")
        check = qa(body, vendor="Nutanix",
                   canonical_names=["Nutanix", "Cisco"])["cross_vendor_links"]
        assert check.status == "pass"


class TestVendorScope:
    NAMES = ["F5", "Check Point", "Cisco", "AWS"]

    def test_a_competitor_section_in_a_single_vendor_article_blocks(self, qa):
        body = ("## Check Point TEM integration\n"
                "Check Point TEM provides hardening guidance. The Check Point "
                "portal receives configuration data.")
        check = qa(body, title="F5 Advanced WAF Guide", vendor="F5",
                   canonical_names=self.NAMES)["vendor_scope"]
        assert check.status == "fail"
        assert "Check Point" in check.detail

    def test_one_incidental_interoperability_mention_is_allowed(self, qa):
        check = qa("The service can run in AWS.", title="F5 deployment guide",
                   vendor="F5", canonical_names=self.NAMES)["vendor_scope"]
        assert check.status == "pass"

    def test_an_explicit_comparison_is_allowed(self, qa):
        body = "F5 and Check Point use different policy models. Check Point differs."
        check = qa(body, title="F5 and Check Point policy comparison", vendor="F5",
                   canonical_names=self.NAMES)["vendor_scope"]
        assert check.status == "pass"


class TestSevereDefectClaims:
    F5_DOMAINS = {"f5.com"}
    F5_REF = ("S1", "https://techdocs.f5.com/advisory", "F5 advisory",
              "techdocs.f5.com")

    def test_a_competitor_source_cannot_support_an_f5_outage_claim(self, qa):
        body = ("Versions 17.1.2 through 17.5.0 contain a critical bug that "
                "renders the API completely unusable [S1].")
        check = qa(
            body, vendor="F5", vendor_domains=self.F5_DOMAINS,
            refs=[("S1", "https://checkpoint.com/tem", "TEM", "checkpoint.com")],
            sources=[("https://checkpoint.com/tem", "checkpoint.com", "TEM",
                      body)],
        )["severe_defect_claims"]
        assert check.status == "fail"
        assert "first-party" in check.detail

    def test_exact_first_party_version_and_impact_support_passes(self, qa):
        claim = ("Versions 17.1.2 through 17.5.0 contain a critical bug that "
                 "renders the API completely unusable [S1].")
        source_text = ("A critical bug affects 17.1.2 - 17.5.0 and renders the "
                       "API completely unusable.")
        check = qa(
            claim, vendor="F5", vendor_domains=self.F5_DOMAINS,
            refs=[self.F5_REF],
            sources=[(self.F5_REF[1], self.F5_REF[3], self.F5_REF[2], source_text)],
        )["severe_defect_claims"]
        assert check.status == "pass"

    def test_an_absolute_bug_claim_without_an_identifier_blocks(self, qa):
        claim = "A critical bug renders the API completely unusable [S1]."
        check = qa(
            claim, vendor="F5", vendor_domains=self.F5_DOMAINS,
            refs=[self.F5_REF],
            sources=[(self.F5_REF[1], self.F5_REF[3], self.F5_REF[2], claim)],
        )["severe_defect_claims"]
        assert check.status == "fail"
        assert "precise version" in check.detail


class TestF5ProductScope:
    def test_bigiq_roles_without_bigiq_context_block(self, qa):
        check = qa("Use the Web App Security Deployer role for changes.",
                   vendor="F5")["product_scope"]
        assert check.status == "fail"
        assert "BIG-IQ" in check.detail

    def test_bigiq_roles_with_explicit_scope_pass(self, qa):
        check = qa("In BIG-IQ Centralized Management, use the Web App Security "
                   "Deployer role for web application security deployments.",
                   vendor="F5")["product_scope"]
        assert check.status == "pass"

    def test_guided_configuration_is_not_made_mandatory(self, qa):
        check = qa("Effective implementation relies on Guided Configuration.",
                   vendor="F5")["product_scope"]
        assert check.status == "fail"

    def test_guided_configuration_can_be_one_workflow(self, qa):
        check = qa("Guided Configuration can provide one structured workflow.",
                   vendor="F5")["product_scope"]
        assert check.status == "pass"


class TestInternalLinkDensity:
    def test_more_than_four_internal_links_warns(self, qa):
        body = " ".join(
            f"[context {i}](https://www.rededucation.com/page-{i}/)"
            for i in range(5))
        assert qa(body)["internal_link_density"].status == "warn"

    def test_four_internal_links_passes(self, qa):
        body = " ".join(
            f"[context {i}](https://www.rededucation.com/page-{i}/)"
            for i in range(4))
        assert qa(body)["internal_link_density"].status == "pass"


class TestGenerationGuardrails:
    def test_entity_contract_carries_scope_and_defect_rules_for_every_vendor(self):
        from synthesis.brief import entity_contract_block

        block = entity_contract_block("Example Vendor", [], [])
        assert "single-vendor article" in block
        assert "subject vendor's own advisory or release note" in block
        assert "exact product or management console" in block
        assert "2-4 earned internal links" in block


class TestCourseCredentialIdentity:
    COURSE = "Nutanix Multicloud Automation Administration (NMCAA)"

    def test_a_course_called_a_certification_blocks(self, qa):
        check = qa(
            "NMCAA certification prepares administrators for automation [S1].",
            title="The NMCAA Certification Guide",
            vendor="Nutanix", course_names=[self.COURSE],
            certification_names=["Nutanix Certified Professional - Multicloud Automation"],
        )["course_credential_identity"]
        assert check.status == "fail"
        assert "training course" in check.detail

    def test_a_course_and_separate_credential_remain_distinct(self, qa):
        check = qa(
            "The NMCAA course prepares learners for the separately named "
            "NCP-MCA certification [S1].",
            vendor="Nutanix", course_names=[self.COURSE],
            certification_names=["NCP-MCA"],
        )["course_credential_identity"]
        assert check.status == "pass"

    def test_the_rule_is_not_specific_to_nutanix(self, qa):
        check = qa(
            "Implementing Widgets is a certification [S1].",
            vendor="Testvendor", course_names=["Testvendor Implementing Widgets"],
            certification_names=["Widget Expert"],
        )["course_credential_identity"]
        assert check.status == "fail"


class TestVolatileEditorialFacts:
    def test_an_uncited_prerequisite_blocks(self, qa):
        check = qa("Widget Basics is a prerequisite for Widget Advanced.")[
            "volatile_facts"]
        assert check.status == "fail"

    def test_a_cited_prerequisite_passes(self, qa):
        check = qa("Widget Basics is a prerequisite for Widget Advanced [S1].")[
            "volatile_facts"]
        assert check.status == "pass"

    def test_a_multi_source_marker_counts_as_support(self, qa):
        check = qa("Widget Basics was formerly known as Widget Start [S2, S4].")[
            "volatile_facts"]
        assert check.status == "pass"

    def test_a_table_header_is_not_mistaken_for_a_voucher_claim(self, qa):
        body = ("| Method | Exam Voucher Included | Audience |\n"
                "| :--- | :--- | :--- |\n"
                "| Virtual | Voucher included [S2] | Engineers |")
        assert qa(body)["volatile_facts"].status == "pass"

    def test_an_expired_promotion_blocks(self, qa):
        check = qa("The promotion is valid until 14 February 2020 [S1].")[
            "volatile_facts"]
        assert check.status == "fail"
        assert "expired" in check.detail

    def test_a_promotion_without_a_full_expiry_date_blocks(self, qa):
        check = qa("Occasional promotional discounts may be available [S1].")[
            "volatile_facts"]
        assert check.status == "fail"
        assert "future expiry date" in check.detail

    def test_a_historical_expired_offer_is_not_presented_as_current(self, qa):
        check = qa("A promotion expired on 14 February 2020 [S1].")[
            "volatile_facts"]
        assert check.status == "pass"


class TestUnnamedInHouseAttribution:
    """Authority resting on nobody.

    One article carried three instances using three different verbs —
    "regularly see", "note that", "frequently observe". A phrase blocklist
    would have caught one and produced a fourth variant, so the rule keys on
    the SHAPE: an in-house group, a reporting verb, and no person named.
    """

    def test_all_three_variants_are_caught(self, qa):
        body = ("Red Education instructors regularly see that learners "
                "struggle. Red Education instructors note that rules drift. "
                "Red Education instructors frequently observe that policies "
                "are too broad.")
        c = qa(body, known_people={"Jerry Solomon"})
        assert c["unnamed_attribution"].status == "warn"
        assert "3 claim(s)" in c["unnamed_attribution"].detail

    def test_naming_a_real_person_passes(self, qa):
        # The fix is to attribute the observation, not to avoid the words.
        body = ("Jerry Solomon, a Red Education instructor, notes that rules "
                "drift over time.")
        assert qa(body, known_people={"Jerry Solomon"}
                  )["unnamed_attribution"].status == "pass"

    def test_a_group_without_a_reporting_verb_is_not_flagged(self, qa):
        body = "Our instructors are certified on the current release."
        assert qa(body, known_people=set())["unnamed_attribution"].status == "pass"

    def test_a_verb_not_yet_in_the_list_is_still_caught_by_shape(self, qa):
        # A fourth variant. The group and the reporting shape are what matter.
        body = "Red Education instructors consistently stress that this fails."
        assert qa(body, known_people=set())["unnamed_attribution"].status == "warn"

    def test_the_message_says_how_to_fix_it(self, qa):
        d = qa("Red Education instructors regularly see that this happens.",
               known_people=set())["unnamed_attribution"].detail
        assert "Name the instructor" in d


class TestEntityType:
    """A course described as a way of working.

    "EDU-330 Firewall Troubleshooting is a technical framework for isolating
    network and application-layer anomalies." Palo Alto's own page — which was
    [S5] in that article's reference list — calls it a three-day
    instructor-led course. The article cited the page and called it something
    else, so this is a fidelity failure, not a grounding one: vendor_grounding
    passes.

    Fourth article to get an entity's type wrong, and it lands in the opening
    sentence every time because direct_answer REQUIRES a definition there —
    the same block an AI assistant extracts and quotes.
    """
    ENTITIES = ["EDU-330 Firewall : Troubleshooting"]

    def test_a_course_called_a_framework_blocks(self, qa):
        c = qa("EDU-330 Firewall Troubleshooting is a technical framework for "
               "isolating anomalies.", title="The Complete EDU-330 Guide",
               entities=self.ENTITIES, vendor_terms=self.ENTITIES)
        assert c["entity_type"].status == "fail"
        assert "framework" in c["entity_type"].detail

    def test_a_course_called_a_course_passes(self, qa):
        c = qa("EDU-330 Firewall Troubleshooting is a three-day instructor-led "
               "course covering packet flow.", title="The Complete EDU-330 Guide",
               entities=self.ENTITIES, vendor_terms=self.ENTITIES)
        assert c["entity_type"].status == "pass"

    def test_words_between_the_alias_and_the_verb_are_allowed(self, qa):
        # The KB alias is "EDU-330"; the article writes the full course name
        # before the verb. Requiring the verb immediately after the alias
        # matched nothing on the article this rule was built for.
        c = qa("EDU-330 Firewall Troubleshooting is a discipline.",
               title="EDU-330 Guide", entities=self.ENTITIES,
               vendor_terms=self.ENTITIES)
        assert c["entity_type"].status == "fail"

    def test_a_sentence_that_defines_nothing_is_not_graded(self, qa):
        # Mentioning the subject is not defining it.
        c = qa("Engineers taking EDU-330 will learn packet flow analysis.",
               title="EDU-330 Guide", entities=self.ENTITIES,
               vendor_terms=self.ENTITIES)
        assert "entity_type" not in c

    def test_an_article_about_no_known_entity_is_not_graded(self, qa):
        c = qa("Zero trust is a security framework.", title="Zero Trust Guide",
               entities=self.ENTITIES, vendor_terms=self.ENTITIES)
        assert "entity_type" not in c


class TestUsSpellingCoversAdjectivalForms:
    def test_organizational_is_caught(self):
        # It ends in "-ational", not "-ation", so the old pattern matched
        # none of organizational / organizationally. An article's Key Facts
        # block said "organizational security controls" and locale_spelling
        # reported it clean.
        from synthesis.qa_rules import us_spellings
        found = us_spellings("guide the baseline for organizational controls", [])
        assert found and "organisational" in found[0][1]

    def test_the_fixer_corrects_it_too(self):
        from synthesis.text import fix_writing_mechanics
        assert "organisational" in fix_writing_mechanics(
            "organizational security controls", None, [])

    def test_capitalisation_survives_the_longer_suffix(self):
        from synthesis.text import fix_writing_mechanics
        assert fix_writing_mechanics("Organizationally speaking", None, []
                                     ).startswith("Organisationally")


class TestEntityTypeChecksEveryDefinition:
    """One fix to the opening must not make the check pass.

    The article defined its subject twice — "is a technical framework" in the
    opening and "is a specialised technical discipline" further down. Checking
    only the first meant a repair to the opening cleared the check while the
    second definition stayed wrong, and a definition further down the body is
    quoted just as readily as the first.
    """
    ENTITIES = ["EDU-330 Firewall : Troubleshooting"]
    BOTH = ("EDU-330 Firewall Troubleshooting is a three-day instructor-led "
            "course. Later on, EDU-330 Firewall Troubleshooting is a "
            "specialised technical discipline centred on diagnostics.")

    def _run(self, qa, body):
        return qa(body, title="The Complete EDU-330 Guide",
                  entities=self.ENTITIES, vendor_terms=self.ENTITIES)

    def test_a_second_wrong_definition_still_fails(self, qa):
        c = self._run(qa, self.BOTH)
        assert c["entity_type"].status == "fail"
        assert "discipline" in c["entity_type"].detail

    def test_the_message_says_how_many_of_how_many(self, qa):
        # "Fix the definition" is not actionable when there are two and only
        # one is wrong.
        d = self._run(qa, self.BOTH)["entity_type"].detail
        assert "1 of 2 sentence(s)" in d
        assert "FIX EVERY ONE" in d

    def test_all_definitions_correct_passes(self, qa):
        good = ("EDU-330 Firewall Troubleshooting is a three-day course. "
                "EDU-330 Firewall Troubleshooting is the course covering "
                "packet flow.")
        c = self._run(qa, good)
        assert c["entity_type"].status == "pass"
        assert "all 2 definition(s)" in c["entity_type"].detail

    def test_both_wrong_are_both_counted(self, qa):
        bad = ("EDU-330 Firewall Troubleshooting is a framework. "
               "EDU-330 Firewall Troubleshooting is a discipline.")
        assert "2 of 2 sentence(s)" in self._run(qa, bad)["entity_type"].detail

class TestIdiraParentBrand:
    NAMES = ["IDIRA", "Palo Alto Networks", "Cisco"]

    def test_parent_brand_link_is_allowed(self, qa):
        body = (
            "IDIRA provides privileged access management training. "
            "See [Palo Alto Networks IDIRA training]"
            "(https://www.rededucation.com/course/australasia/"
            "palo-alto-networks/idira-pam/)."
        )
        check = qa(
            body, vendor="IDIRA", canonical_names=self.NAMES
        )["cross_vendor_links"]
        assert check.status == "pass"

    def test_unrelated_vendor_link_still_blocks(self, qa):
        body = (
            "IDIRA privileged access management is the subject. "
            "See [Cisco training]"
            "(https://www.rededucation.com/cisco/training/)."
        )
        check = qa(
            body, vendor="IDIRA", canonical_names=self.NAMES
        )["cross_vendor_links"]
        assert check.status == "fail"

    def test_parent_brand_coverage_is_allowed(self, qa):
        body = (
            "## Palo Alto Networks and IDIRA\n"
            "Palo Alto Networks offers IDIRA privileged access management. "
            "Palo Alto Networks provides related training resources."
        )
        check = qa(
            body,
            title="IDIRA PAM Deployment Guide",
            vendor="IDIRA",
            canonical_names=self.NAMES,
        )["vendor_scope"]
        assert check.status == "pass"

    def test_unrelated_vendor_scope_still_blocks(self, qa):
        body = (
            "## Cisco Training\n"
            "Cisco provides networking courses. "
            "Cisco certification pathways are discussed here."
        )
        check = qa(
            body,
            title="IDIRA PAM Deployment Guide",
            vendor="IDIRA",
            canonical_names=self.NAMES,
        )["vendor_scope"]
        assert check.status == "fail"
