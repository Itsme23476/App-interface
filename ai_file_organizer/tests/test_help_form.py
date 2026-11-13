"""The Need help? form sends the user's sign-in. The server (send-contact) asks for the website's captcha unless the
request carries a valid Filect sign-in, and the app can't show a captcha: without the header every Windows message was
refused (403 "verification_failed"; live test 2026-11-13). Mac: 97677a2. Nothing is sent: urlopen is a stand-in."""
import json
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


class _Resp:
    def __init__(self, body):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _send(token):
    seen = {}

    def fake(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = json.loads(req.data.decode())
        return _Resp(b'{"ok": true}')
    w = mwm.HelpSendWorker("https://x.test/functions/v1/send-contact",
                           {"name": "A", "email": "a@b.co", "message": "hello there, help"}, token=token)
    got = []
    w.finished_ok.connect(lambda p: got.append(p))
    with mock.patch("urllib.request.urlopen", fake):
        w.run()                                                # (on this thread: nothing to wait for)
    return seen, got


def test_the_sign_in_goes_with_the_message():
    seen, got = _send("tok-123")
    assert seen["auth"] == "Bearer tok-123" and got == [{"ok": True}]
    assert seen["body"] == {"name": "A", "email": "a@b.co", "message": "hello there, help"}


def test_signed_out_sends_no_header():
    seen, _ = _send(None)
    assert seen["auth"] is None


def test_the_dialog_passes_the_users_sign_in(monkeypatch):
    made = []

    class Rec:
        def __init__(self, url, payload, token=None):
            made.append((url, payload, token))
            self.finished_ok = SimpleNamespace(connect=lambda f: None)
            self.failed = SimpleNamespace(connect=lambda f: None)

        def start(self):
            pass
    monkeypatch.setattr(mwm, "HelpSendWorker", Rec)
    monkeypatch.setattr(mwm, "supabase_auth", SimpleNamespace(is_authenticated=True, get_access_token=lambda: "live-tok",
                                                              user_email="a@b.co"))
    d = mwm.HelpDialog(None, default_email="a@b.co")
    d.message_input.setPlainText("The search doesn't find my file.")
    d._on_send_clicked()
    assert made and made[-1][2] == "live-tok" and made[-1][0].endswith("/functions/v1/send-contact")
    monkeypatch.setattr(mwm, "supabase_auth", SimpleNamespace(is_authenticated=False, get_access_token=lambda: "x",
                                                              user_email=""))
    d2 = mwm.HelpDialog(None, default_email="a@b.co")
    d2.message_input.setPlainText("The search doesn't find my file.")
    d2._on_send_clicked()
    assert made[-1][2] is None, "signed out: no sign-in to send"
    d.deleteLater()
    d2.deleteLater()
