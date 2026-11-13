"""A space between a dictation and the text before the cursor. Found in the Windows live test (stage 32, 2026-11-13):
two dictations in a row were pasted as "This is a timing test.My email is…". Now a space goes first when the text
before the cursor could be read and ends right against a word or punctuation. Unreadable, or a field start / after a
space: unchanged. Only pasting changes (search and organize get the words as they are)."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.dictation_format import join_space  # noqa: E402


def test_the_rule():
    for before, text, want in [
        ("This is a timing test.", "My email is", " My email is"),
        ("Hello", "world", " world"),
        ("one, two,", "three", " three"),
        ("(see above)", "Then", " Then"),
        ("Total: 50%", "of it", " of it"),
        ("Hello ", "world", "world"),                  # a space already
        ("line one\n", "Line two", "Line two"),         # a new line
        ("", "Hello", "Hello"),                         # an empty field
        (None, "Hello", "Hello"),                       # couldn't read it: unchanged
        ("Hello", ", and more", ", and more"),          # it starts with punctuation
        ("(", "aside", "aside"),                        # an opening bracket
        ("name@", "gmail.com", "gmail.com"),            # inside an address
        ("https://", "filect.io", "filect.io"),
        ('He said "', "hi", "hi"),                      # a quote: could be either
        ("well-", "known", "known"),
    ]:
        assert join_space(before, text) == want, (before, text, join_space(before, text))


def _finish(before, text, mode="dictate"):
    from app.ui.dictation import DictationController
    from app.core.settings import settings
    out = {"paste": [], "search": [], "history": []}
    c = SimpleNamespace(_overlay=mock.MagicMock(), state_changed=mock.MagicMock(), transcript_ready=mock.MagicMock(),
                        _mode=mode, _recorder=object(), _custom_words=lambda: [],
                        _paste=lambda t: out["paste"].append(t), _route_search=lambda t: out["search"].append(t),
                        _save_history=lambda t, m: out["history"].append(t))
    with mock.patch.object(settings, "dictation_polish_level", "none"), \
            mock.patch.object(settings, "live_dictation_custom_terms", lambda: []), \
            mock.patch("app.ui.win_hotkey.text_before_caret", lambda: before), \
            mock.patch("app.core.supabase_client.track", lambda *a, **k: None):
        DictationController._on_finished(c, text)
    return out


def test_the_second_dictation_gets_its_space():
    out = _finish("This is a timing test.", "my email is john.")
    assert out["paste"] == [" My email is john."], out
    assert out["history"] == ["My email is john."], out          # (History keeps the words, not the space)


def test_unreadable_or_start_of_field_is_unchanged():
    assert _finish(None, "hello there.")["paste"] == ["hello there."]
    assert _finish("", "hello there.")["paste"] == ["Hello there."]


def test_search_gets_the_words_without_a_space():
    out = _finish("This is a timing test.", "find my invoice", mode="search")
    assert out["search"] and not out["search"][0].startswith(" "), out
