from types import SimpleNamespace

import synthesis.brief as brief_module
import synthesis.llm as llm_module
import synthesis.validate as validate_module
from retrieval.models import VerifiedBundle
from synthesis.brief import ArticlePackage, ContentBrief, generate_passing_article
from synthesis.models import QAReport


def test_generate_passing_article_forwards_outline_to_qa(monkeypatch):
    outline = ["Packet processing", "Troubleshooting tools", "Common mistakes"]
    brief = ContentBrief(
        title="Complete EDU-330 Firewall Troubleshooting Guide",
        audience="Firewall engineers",
        keyword="edu-330 firewall troubleshooting",
        format="explainer",
        outline=outline,
    )
    package = ArticlePackage(
        content="Article body.",
        meta_title="EDU-330 Firewall Troubleshooting",
        meta_description="A practical EDU-330 firewall troubleshooting guide.",
        excerpt="A practical guide.",
    )
    captured = {}

    monkeypatch.setattr(brief_module, "build_article_prompt",
                        lambda *args, **kwargs: ("system", "user", []))
    monkeypatch.setattr(llm_module, "complete_with_configured_provider",
                        lambda *args, **kwargs: ("{}", "test-model"))
    monkeypatch.setattr(brief_module, "parse_article_response",
                        lambda *args, **kwargs: (True, package))
    monkeypatch.setattr(brief_module, "references_from_tags", lambda tags: [])

    def validate(*args, **kwargs):
        captured.update(kwargs)
        return QAReport()

    monkeypatch.setattr(validate_module, "validate_draft", validate)
    monkeypatch.setattr(
        brief_module,
        "compute_reacs",
        lambda **kwargs: SimpleNamespace(
            passed=True, score=100, mandatory_failed=0),
    )

    result = generate_passing_article(
        brief,
        VerifiedBundle(topic="EDU-330", region=None),
        "Palo Alto Networks",
        max_attempts=1,
        outline=outline,
    )

    assert result is not None
    assert captured["outline"] == outline
