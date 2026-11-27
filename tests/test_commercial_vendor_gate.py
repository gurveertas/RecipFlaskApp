"""Research-only vendors remain sources, never commercial workflow targets."""
import inspect
from types import SimpleNamespace

import pytest

from webapp.pipeline import (PipelineError, _create_brief_for_topic,
                             _require_commercial_vendor,
                             generate_topics_for_vendor, push_to_wordpress,
                             write_article)


def test_research_only_vendor_is_blocked_with_an_actionable_message():
    vendor = SimpleNamespace(name="Former Partner", priority=None)
    with pytest.raises(PipelineError) as exc:
        _require_commercial_vendor(vendor, "publish articles")
    assert "research-only" in str(exc.value)
    assert "currently authorised" in str(exc.value)


def test_a_current_commercial_vendor_passes_the_gate():
    _require_commercial_vendor(SimpleNamespace(name="Current", priority=3),
                               "publish articles")


def test_topic_generation_blocks_before_spending_or_querying():
    class NoDatabaseCalls:
        def __getattr__(self, name):
            raise AssertionError(f"database was touched through {name}")

    with pytest.raises(PipelineError):
        generate_topics_for_vendor(
            NoDatabaseCalls(), SimpleNamespace(name="Cisco", priority=None),
            5, None)


@pytest.mark.parametrize("boundary", [
    generate_topics_for_vendor,
    _create_brief_for_topic,
    write_article,
    push_to_wordpress,
])
def test_every_commercial_boundary_enforces_vendor_status(boundary):
    assert "_require_commercial_vendor" in inspect.getsource(boundary)


def test_cisco_stays_on_the_source_allowlist():
    from pathlib import Path
    sources = Path("config/sources.yaml").read_text()
    assert "domain: cisco.com" in sources
    assert "Primary-source access does not imply" in sources
