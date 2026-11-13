"""Billing buttons act as the signed-in user, never with a user id in a link (the server's 2026-11-12 security fix:
anyone holding someone's id could open THEIR billing portal, switch or cancel their plan). Windows stage 31,
2026-11-13: Manage subscription opened create-portal-session?user_id=…&email=…, Cancel opened
filect.io/cancel?uid=…&email=…. Now: Manage = POST with the sign-in → the returned link (or the account page);
Cancel and a plan switch carry the sign-in in the link's #part. Mac 8bb560c.
Nothing is sent or opened: requests.post / webbrowser.open are stand-ins; the sign-in is a fake one."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
import app.ui.main_window as mwm  # noqa: E402
from app.core.supabase_client import supabase_auth  # noqa: E402

USER = {"id": "uid-1234", "email": "someone@example.com"}
TOKEN = "tok-abc"


class _Resp:
    def __init__(self, status, body, ctype="application/json"):
        self.status_code, self._b, self.headers = status, body, {"content-type": ctype}

    def json(self):
        return self._b


class _NowThread:
    """threading.Thread stand-in that runs the target at start(), on this thread."""
    def __init__(self, target=None, daemon=None, name=None, **k):
        self._t = target

    def start(self):
        self._t()


def signed_in():
    return [mock.patch.object(supabase_auth, "_user", dict(USER)),
            mock.patch.object(supabase_auth, "_session", object()),
            mock.patch.object(supabase_auth, "get_access_token", lambda *a, **k: TOKEN)]


def run(fn, post=None):
    opened, posts = [], []

    def fake_post(url, *a, **k):
        posts.append((url, k.get("headers") or {}))
        if isinstance(post, Exception):
            raise post
        return post or _Resp(200, {"url": "https://billing.stripe.com/p/session/test_1"})
    w = SimpleNamespace(status_bar=mock.MagicMock())
    ps = signed_in() + [mock.patch("webbrowser.open", lambda url, *a, **k: opened.append(url) or True),
                        mock.patch("requests.post", fake_post),
                        mock.patch("threading.Thread", _NowThread),
                        mock.patch("app.ui.organize_page.ModernInfoDialog.show_warning")]
    for p in ps:
        p.start()
    try:
        fn(w)
    finally:
        for p in reversed(ps):
            p.stop()
    return opened, posts


def test_manage_subscription_asks_the_server_with_the_sign_in():
    opened, posts = run(mwm.MainWindow._open_billing_portal)
    assert len(posts) == 1, posts
    url, headers = posts[0]
    assert url.endswith("/functions/v1/create-portal-session"), url
    assert headers.get("Authorization") == f"Bearer {TOKEN}", headers
    assert opened == ["https://billing.stripe.com/p/session/test_1"], opened
    for u in [url] + opened:
        assert USER["id"] not in u and "email" not in u and "user_id" not in u, u


def test_manage_subscription_falls_back_to_the_account_page():
    opened, _ = run(mwm.MainWindow._open_billing_portal, _Resp(401, {"error": "signin"}))
    assert opened == ["https://filect.io/account"], opened
    opened, _ = run(mwm.MainWindow._open_billing_portal, ConnectionError("offline"))
    assert opened == ["https://filect.io/account"], opened


def test_cancel_carries_the_sign_in_not_the_id():
    opened, posts = run(mwm.MainWindow._open_cancel_flow)
    assert opened == [f"https://filect.io/cancel?source=app#at={TOKEN}"], opened
    assert posts == []


def test_switching_plans_carries_the_sign_in_checkout_unchanged():
    opened = []
    sub = {"status": "active", "price_id": "price_pro_1"}
    with mock.patch.object(supabase_auth, "_user", dict(USER)), \
            mock.patch.object(supabase_auth, "_subscription", sub), \
            mock.patch.object(supabase_auth, "get_access_token", lambda *a, **k: TOKEN), \
            mock.patch.object(supabase_auth, "check_subscription", lambda *a, **k: {}), \
            mock.patch("webbrowser.open", lambda url, *a, **k: opened.append(url) or True):
        assert supabase_auth.open_web_pricing()
    base = "https://filect.io/pricing.html?uid=uid-1234&email=someone%40example.com&source=app&current=price_pro_1"
    assert opened == [f"{base}#at={TOKEN}"], opened


def test_a_new_subscriber_link_is_unchanged():
    opened = []
    with mock.patch.object(supabase_auth, "_user", dict(USER)), \
            mock.patch.object(supabase_auth, "_subscription", None), \
            mock.patch.object(supabase_auth, "get_access_token", lambda *a, **k: TOKEN), \
            mock.patch.object(supabase_auth, "check_subscription", lambda *a, **k: {}), \
            mock.patch("webbrowser.open", lambda url, *a, **k: opened.append(url) or True):
        assert supabase_auth.open_web_pricing()
    assert opened == ["https://filect.io/pricing.html?uid=uid-1234&email=someone%40example.com&source=app"], opened
