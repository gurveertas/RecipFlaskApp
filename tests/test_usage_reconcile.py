"""The Serper credit count is derived, and it drifted by 97.

Serper has no balance API, so "credits left" is a baseline minus everything
this app tracked. Anything spent OUTSIDE this server — a diagnostic script run
from a laptop, the Serper playground — hits the same account and lands in a
different database, so the tool reported 187 while serper.dev said 90.

Correcting it meant editing .env and restarting the service, which is why it
had not been corrected.
"""
import json

import pytest

from config.db import ApiUsage, AppConfig, get_session


@pytest.fixture
def client():
    from webapp.app import create_app
    return create_app().test_client()


@pytest.fixture(autouse=True)
def _reset():
    yield
    with get_session() as s:
        cfg = s.query(AppConfig).first()
        if cfg is not None:
            cfg.serper_balance = None
            cfg.serper_balance_tracked = None
            cfg.serper_balance_at = None
            s.commit()


def summary(client):
    d = json.loads(client.get("/admin/usage.json").data)
    return d.get("summary", d)


def tracked_now():
    with get_session() as s:
        return sum(r.credits for r in s.query(ApiUsage).all()
                   if r.provider == "serper")


class TestReconciling:
    def test_the_reported_balance_becomes_what_was_entered(self, client):
        client.post("/admin/usage/reconcile", data={"balance": "90"})
        assert summary(client)["serper_left"] == 90

    def test_later_spend_comes_off_the_reconciled_figure(self, client):
        # The pair (balance, tracked-at-that-moment) is a fixed point, so a
        # reconciliation survives everything spent afterwards without needing
        # to be redone.
        client.post("/admin/usage/reconcile", data={"balance": "90"})
        with get_session() as s:
            s.add(ApiUsage(provider="serper", purpose="retrieval", credits=4))
            s.commit()
        assert summary(client)["serper_left"] == 86

    def test_the_date_is_recorded_so_a_stale_estimate_looks_stale(self, client):
        assert summary(client)["serper_reconciled_at"] is None
        client.post("/admin/usage/reconcile", data={"balance": "90"})
        assert summary(client)["serper_reconciled_at"] is not None

    def test_it_needs_no_restart(self):
        # The whole point: stored in the database, not the environment.
        cols = set(AppConfig.__table__.columns.keys())
        assert {"serper_balance", "serper_balance_at",
                "serper_balance_tracked"} <= cols


class TestBadInput:
    @pytest.mark.parametrize("value", ["", "  ", "ninety", "-5", "9.5"])
    def test_nonsense_is_rejected_without_changing_anything(self, client, value):
        before = summary(client)["serper_left"]
        r = client.post("/admin/usage/reconcile", data={"balance": value})
        assert r.status_code in (302, 303)
        assert summary(client)["serper_left"] == before

    def test_zero_is_a_legitimate_balance(self, client):
        # Running out is exactly when the number matters most.
        client.post("/admin/usage/reconcile", data={"balance": "0"})
        assert summary(client)["serper_left"] == 0
