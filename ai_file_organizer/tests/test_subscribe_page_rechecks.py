"""The "Unlock Filect" (subscribe) page re-checks the subscription while it's shown. Found on the Mac 2026-11-12 (Windows
stage 30, 2026-11-13): the check started only after the Subscribe/View plans button was clicked, so someone who
subscribed another way (the website on their phone, a card retry that went through) stayed locked out until they
clicked again or restarted Filect. Same double-confirmed check and 30-minute limit as after a click. Mac 59aab88.
The sign-in window is opened with its session restore switched off (it would refresh - or clear - the saved session),
the subscription answers are stand-ins, and nothing is written."""
import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
import app.ui.auth_dialog as ad  # noqa: E402


def dialog():
    with mock.patch.object(ad.AuthDialog, "_try_restore_session", lambda self: None), \
            mock.patch.object(ad.QTimer, "singleShot", lambda *a, **k: None):
        return ad.AuthDialog()


def answers(*subs):
    """check_subscription answers in turn (the last one repeats)."""
    it = list(subs)
    return lambda *a, **k: it.pop(0) if len(it) > 1 else it[0]


def test_the_page_starts_checking_when_it_shows():
    d = dialog()
    assert not d._poll_timer.isActive()
    d._show_subscribe_page()
    assert d.stack.currentIndex() == 2
    assert d._poll_timer.isActive() and d._poll_timer.interval() == 3000
    d._poll_timer.stop()


def test_subscribed_elsewhere_lets_you_in_with_no_click():
    d = dialog()
    d._show_subscribe_page()
    got = []
    d.auth_successful.connect(lambda: got.append(1))
    with mock.patch.object(ad.supabase_auth, "check_subscription",
                           answers({"has_subscription": False}, {"has_subscription": True})), \
            mock.patch.object(ad.QTimer, "singleShot", lambda ms, fn: fn()):
        d._poll_subscription()                                 # not yet
        assert d.result() == 0 and d._poll_timer.isActive()
        d._poll_subscription()                                 # subscribed - once: "Confirming…"
        assert d.result() == 0 and got == []
        d._poll_subscription()                                 # twice in a row: in
    assert got == [1] and d.result() == 1 and not d._poll_timer.isActive()


def test_a_blocked_trial_still_doesnt_get_in():
    d = dialog()
    d._show_subscribe_page(blocked=True)
    with mock.patch.object(ad.supabase_auth, "check_subscription",
                           answers({"has_subscription": True, "trial_blocked_reason": "duplicate_trial_card"})), \
            mock.patch.object(ad.QTimer, "singleShot", lambda ms, fn: fn()):
        for _ in range(4):
            d._poll_subscription()
    assert d.result() == 0 and d._poll_timer.isActive()
    d._poll_timer.stop()


def test_showing_it_again_doesnt_restart_a_running_check():
    d = dialog()
    d._show_subscribe_page()
    d._poll_count = 40
    d._show_subscribe_page()                                   # (the 30-minute limit isn't reset by a re-show)
    assert d._poll_count == 40 and d._poll_timer.isActive()
    d._poll_timer.stop()


def test_signing_out_stops_the_check():
    d = dialog()
    d._show_subscribe_page()
    d._poll_timer.start(3000)                                  # (running, as after the Subscribe button too)
    with mock.patch.object(ad.supabase_auth, "sign_out", lambda *a, **k: {}), \
            mock.patch.object(ad.settings, "clear_auth_tokens", lambda *a, **k: None):
        d._do_logout()
    assert not d._poll_timer.isActive() and d.stack.currentIndex() == 0
