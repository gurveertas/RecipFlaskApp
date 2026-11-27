"""A one-word brand with an interior capital must be cased like the KB spells it.

"Fortimanager" reached a finished draft nine times, plus the title and the meta
description, with brand_casing and writing_mechanics both passing. The phrase
rule needs two words — "Panorama" alone is an ordinary noun as often as a
product — and known_acronyms only harvests ALL-CAPS runs, so a single CamelCase
word fell between them.
"""
from synthesis.text import (_brand_tokens, fix_writing_mechanics,
                            writing_mechanics_issues)

FORTINET = ["FortiManager Administrator 7.6", "FortiAnalyzer Analyst 7.6",
            "FortiGate Administrator 7.4", "Fortios Administrator 7.6"]


class TestWhichWordsCount:
    def test_an_interior_capital_makes_it_a_brand(self):
        assert "FortiManager" in _brand_tokens(FORTINET)
        assert "FortiAnalyzer" in _brand_tokens(FORTINET)

    def test_a_leading_capital_alone_does_not(self):
        # "Panorama" is an ordinary noun as often as a product; correcting it
        # mid sentence is worse than leaving it.
        assert _brand_tokens(["Panorama: Centralized Network Security"]) == []

    def test_an_ordinary_course_word_is_not_a_brand(self):
        assert "Administrator" not in _brand_tokens(FORTINET)

    def test_the_kbs_own_miscased_row_is_not_taught_as_truth(self):
        # The KB holds "Fortios Administrator 7.6". Harvesting that would make
        # the fixer WRITE the error it exists to catch.
        assert not any(t.lower() == "fortios" for t in _brand_tokens(FORTINET))

    def test_an_all_caps_acronym_is_left_to_the_acronym_rule(self):
        assert "XSIAM" not in _brand_tokens(["Cortex XSIAM: Investigation"])


class TestTheFixer:
    def test_it_corrects_the_case_that_shipped(self):
        out = fix_writing_mechanics(
            "Fortimanager and fortianalyzer integration provides a unified "
            "architecture.", vendor_terms=FORTINET)
        assert "FortiManager and FortiAnalyzer integration" in out

    def test_it_leaves_a_correct_spelling_alone(self):
        text = "FortiManager manages FortiAnalyzer units."
        assert fix_writing_mechanics(text, vendor_terms=FORTINET) == text

    def test_it_does_not_touch_urls_or_code(self):
        text = "See `fortimanager.conf` at https://example.com/fortimanager/x"
        assert fix_writing_mechanics(text, vendor_terms=FORTINET) == text

    def test_a_word_that_merely_contains_the_brand_is_untouched(self):
        text = "The fortimanagers directory is unrelated."
        assert fix_writing_mechanics(text, vendor_terms=FORTINET) == text


class TestTheReporterAgrees:
    def test_it_names_what_the_fixer_would_change(self):
        issues = writing_mechanics_issues("Fortimanager integration",
                                          vendor_terms=FORTINET)
        assert any('"Fortimanager" should be "FortiManager"' == i for i in issues)

    def test_nothing_to_report_once_corrected(self):
        assert writing_mechanics_issues("FortiManager integration",
                                        vendor_terms=FORTINET) == []
