"""Internal source IDs must not escape into reader-facing article output."""

from publish.payload import to_wp_html
from webapp.render import render_site_preview, strip_citation_markers


def test_strips_single_and_grouped_source_markers():
    text = "Alpha [S1]. Beta [S4, S5]. Gamma [S2; S7] and delta [S8 and S9]."

    cleaned = strip_citation_markers(text)

    assert cleaned == "Alpha. Beta. Gamma and delta."


def test_preview_hides_grouped_source_markers():
    html = render_site_preview(
        "This credential supports infrastructure reliability [S4, S5].")

    assert "[S" not in html
    assert "reliability.</p>" in html


def test_wordpress_payload_hides_grouped_source_markers():
    html = to_wp_html(
        "This credential supports infrastructure reliability [S4, S5].")

    assert "[S" not in html
    assert "reliability.</p>" in html


def test_strips_a_tag_used_as_a_links_visible_text():
    # "[S1](url)" renders as an anchor, which the bracket rule cannot see: it
    # published as a link labelled S1 in a pushed draft (post 182934).
    html = render_site_preview(
        "According to [S1](https://vendor.example/page), the release matters.")

    assert ">S1<" not in html
    assert "vendor.example" not in html


def test_wordpress_payload_drops_a_tag_used_as_a_links_visible_text():
    html = to_wp_html("The release matters [S1](https://vendor.example/page).")

    assert ">S1<" not in html
    assert "vendor.example" not in html


def test_a_real_link_next_to_a_tag_survives():
    # Only the tag goes. A link whose text is real words is content.
    html = render_site_preview(
        "See the [migration procedure](https://vendor.example/migrate) [S3].")

    assert "vendor.example/migrate" in html
    assert "[S" not in html


def test_faq_schema_uses_the_same_reader_boundary_without_mutating_evidence():
    import json
    from publish.payload import build_schema

    faq = [{"question": "How is it managed?", "answer":
            "Use the [documented mode](https://vendor.example/manage) [S4, S5]."}]
    schema = json.loads(build_schema(title="Management", description="Guide",
                                     keyword="management", faq=faq))
    answer = schema["@graph"][1]["mainEntity"][0]["acceptedAnswer"]["text"]
    assert "[S" not in answer
    assert 'href="https://vendor.example/manage"' in answer
    assert "[S4, S5]" in faq[0]["answer"]
