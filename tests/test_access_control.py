import os
from app.access_control import AccessController

class FakeStore:
    def __init__(self): self.users=set()
    def is_user_authorized(self,u): return u in self.users
    def authorize_user(self,u,a): self.users.add(u)
    def unauthorize_user(self,u):
        existed=u in self.users; self.users.discard(u); return existed
    def list_authorized_users(self): return [{"user_id":u,"added_by":"1"} for u in sorted(self.users)]

def test_owner_and_allowlist(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID","123")
    c=AccessController(FakeStore())
    assert c.is_owner(123)
    assert c.is_authorized(123)
    assert not c.is_authorized(456)
    c.authorize(456,123)
    assert c.is_authorized(456)
    assert c.unauthorize(456)
    assert not c.is_authorized(456)
    assert not c.unauthorize(123)
