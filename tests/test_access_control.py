import os
from app.access_control import AccessController
from app.case_store import CaseStore


class FakeStore:
    def __init__(self):
        self.users = set()

    def is_user_authorized(self, u):
        return u in self.users

    def authorize_user(self, u, a):
        self.users.add(u)

    def unauthorize_user(self, u):
        existed = u in self.users
        self.users.discard(u)
        return existed

    def list_authorized_users(self):
        return [{"user_id": u, "added_by": "1"} for u in sorted(self.users)]


def test_owner_and_allowlist(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "123")
    c = AccessController(FakeStore())
    assert c.is_owner(123)
    assert c.is_authorized(123)
    assert not c.is_authorized(456)
    c.authorize(456, 123)
    assert c.is_authorized(456)
    assert c.unauthorize(456)
    assert not c.is_authorized(456)
    assert not c.unauthorize(123)


def test_trial_5_doc_limit_and_paid_plan_quota(monkeypatch, tmp_path):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "123")
    store = CaseStore(path=tmp_path / "test_quota.sqlite3")
    c = AccessController(store)

    # 1. Unauthorized user cannot generate
    ok, msg = c.can_generate(789)
    assert not ok
    assert "authorized नहीं हैं" in msg

    # 2. Owner/Sudo authorizes user 789 on Trial (default 5 documents)
    c.authorize_trial(789, 123)
    status = c.get_plan_status(789)
    assert status["authorized"] is True
    assert status["plan"] == "trial"
    assert status["quota_limit"] == 5
    assert status["used_documents"] == 0
    assert status["remaining"] == 5

    # Consume all 5 trial documents
    for i in range(1, 6):
        ok, _ = c.can_generate(789)
        assert ok is True
        st = c.consume_quota(789)
        assert st["used_documents"] == i
        assert st["remaining"] == 5 - i

    # 6th attempt is blocked because 5-document Trial limit is reached
    ok_6th, msg_6th = c.can_generate(789)
    assert ok_6th is False
    assert "Free Trial Limit Reached (5/5 Documents)" in msg_6th

    # 3. User pays Owner -> Owner runs /paid 789 20 (Paid Plan with 20 Dava/documents limit)
    c.authorize_paid(789, 123, limit=20)
    paid_status = c.get_plan_status(789)
    assert paid_status["plan"] == "paid"
    assert paid_status["quota_limit"] == 20
    assert paid_status["used_documents"] == 0
    assert paid_status["remaining"] == 20

    ok_paid, _ = c.can_generate(789)
    assert ok_paid is True
    after_1 = c.consume_quota(789)
    assert after_1["used_documents"] == 1
    assert after_1["remaining"] == 19

    # 4. Owner always has Unlimited quota
    ok_owner, _ = c.can_generate(123)
    assert ok_owner is True
    owner_status = c.get_plan_status(123)
    assert owner_status["remaining"] == "Unlimited"
