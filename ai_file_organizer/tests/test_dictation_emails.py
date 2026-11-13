"""Dictated email addresses come out as addresses (Mac 2026-11-12: a dictation pasted "software agent official at
gmail dot com"; Windows stage 32, 2026-11-13: "john dot smith at gmail dot com" stayed words). Same steps as the
paste: tidy_text(format_spoken(text)), then your saved addresses (Custom Words) exactly. Mac's tests (97677a2,
c143812, e7edb14) + Windows' own sentence casing and the dictation's own path."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.dictation_format import format_spoken, tidy_text, fix_known_addresses  # noqa: E402

WRITTEN = [
    ("My email is software gent official at gmail dot com.", "My email is softwaregentofficial@gmail.com."),
    ("Send it to the correct email address, software gent official at gmail dot com.",
     "Send it to the correct email address, softwaregentofficial@gmail.com."),
    ("john dot smith at gmail dot com", "john.smith@gmail.com"),
    ("Email Maria underscore Lopez at Outlook dot com please", "Email maria_lopez@outlook.com please"),
    ("it's team at filect dot io", "it's team@filect.io"),
    ("reach me at anna at company dot co dot uk", "reach me at anna@company.co.uk"),
    ("John at Gmail.com", "john@gmail.com"),
    ("john at gmail dot com and mary at yahoo dot com", "john@gmail.com and mary@yahoo.com"),
    ("support plus filect at gmail dot com", "support+filect@gmail.com"),
    # his real dictation 2026-11-12: the speech service put an "at" INSIDE the name; no Custom Words needed
    ("Software at gent at gmail dot com.", "softwaregent@gmail.com."),
    ("My email is software at gent official at gmail dot com", "My email is softwaregentofficial@gmail.com"),
    ("Look at john at gmail dot com", "Look at john@gmail.com"),
    ("reach out at john at gmail dot com", "reach out at john@gmail.com"),
]
LEFT_ALONE = [
    "Look at google dot com for the details.",          # a website, not an address
    "Meet me at gmail dot com headquarters.",            # no name before "at"
    "I'm at home right now.",                            # no domain
    "We met at the company dot party.",                  # "party" is not a domain ending
    "I need ten thousand dollars, which is fifty percent of the budget.",
]


def test_spoken_addresses_are_written_as_addresses():
    bad = [(s, tidy_text(format_spoken(s)), want) for s, want in WRITTEN if tidy_text(format_spoken(s)) != want]
    assert not bad, "\n".join(f"{s!r}\n   -> {got!r}\n want {w!r}" for s, got, w in bad)
    print(f"{len(WRITTEN)} spoken addresses written as addresses  OK")


def test_other_text_is_left_alone():
    want = {LEFT_ALONE[-1]: "I need $10,000, which is 50% of the budget."}
    bad = [(s, tidy_text(format_spoken(s))) for s in LEFT_ALONE if tidy_text(format_spoken(s)) != want.get(s, s)]
    assert not bad, bad
    print(f"{len(LEFT_ALONE)} sentences with 'at' / 'dot' that aren't addresses left as they are  OK")


def test_an_address_keeps_its_lowercase_at_a_start():
    from app.core.dictation_format import apply_sentence_case
    first = apply_sentence_case(tidy_text(format_spoken("john at gmail dot com")), "")      # an empty To: field
    after = tidy_text(format_spoken("Thanks. john at gmail dot com"))                      # a new sentence
    normal = apply_sentence_case("thanks for that", "")
    assert (first, after, normal) == ("john@gmail.com", "Thanks. john@gmail.com", "Thanks for that"), (first, after, normal)
    print("an address typed first in a field or after a full stop stays lowercase (other text still capitalized)  OK")


def test_your_saved_address_is_written_exactly_however_it_was_heard():
    """His real dictation (2026-11-12): the speech service heard "Software at gent at gmail dot com." — an extra
    "at" inside the name, so no rule can know where the name starts. An address saved in Custom Words can."""
    run = lambda raw, terms: fix_known_addresses(tidy_text(format_spoken(raw)), terms)
    mine = ["Supabase", "softwaregent@gmail.com", "softwaregentofficial@gmail.com"]
    cases = [
        ("Software at gent at gmail dot com.", "softwaregent@gmail.com."),
        ("Send it to software at gent official at gmail dot com please", "Send it to softwaregentofficial@gmail.com please"),
        ("My email is software gent official at gmail dot com.", "My email is softwaregentofficial@gmail.com."),
        ("softwaregent@gmail.com and software gent at gmail dot com", "softwaregent@gmail.com and softwaregent@gmail.com"),
        ("Ask the software agent about it.", "Ask the software agent about it."),           # not an address: untouched
        ("john at gmail dot com", "john@gmail.com"),                                          # not saved: the normal rule
    ]
    bad = [(r, run(r, mine), w) for r, w in cases if run(r, mine) != w]
    assert not bad, "\n".join(f"{r!r}\n   -> {g!r}\n want {w!r}" for r, g, w in bad)
    print(f"{len(cases)} cases: a saved address is written exactly however the words came apart; nothing else changes  OK")


# ---- Windows: its own sentence casing (internal boundaries too) and the dictation's own path --------------------
def test_windows_casing_leaves_an_address_alone():
    from app.core.dictation_format import apply_sentence_case
    assert apply_sentence_case("Thanks. john@gmail.com", "", []) == "Thanks. john@gmail.com"
    assert apply_sentence_case("john@gmail.com is mine", "Done. ", []) == "john@gmail.com is mine"
    assert apply_sentence_case("thanks. bye", "", []) == "Thanks. Bye"                     # (other text: as before)


def test_the_dictation_writes_your_saved_address():
    """The dictation's own path: format, tidy, your saved addresses (Custom Words, read fresh), casing - then pasted."""
    from types import SimpleNamespace
    from unittest import mock
    from app.ui.dictation import DictationController
    pasted = []
    c = SimpleNamespace(_overlay=mock.MagicMock(), state_changed=mock.MagicMock(), transcript_ready=mock.MagicMock(),
                        _mode="dictate", _recorder=object(), _custom_words=lambda: [],
                        _paste=lambda t: pasted.append(t), _save_history=lambda *a: None)
    from app.core.settings import settings
    with mock.patch.object(settings, "dictation_polish_level", "none"), \
            mock.patch.object(settings, "live_dictation_custom_terms", lambda: ["worklive@outlook.com"]), \
            mock.patch("app.ui.win_hotkey.text_before_caret", lambda: ""), \
            mock.patch("app.core.supabase_client.track", lambda *a, **k: None):
        # ("live" ends a name for the plain rule - only your saved address can write this one)
        DictationController._on_finished(c, "Send it to work live at outlook dot com.")
        DictationController._on_finished(c, "My email is john dot smith at gmail dot com.")
    assert pasted == ["Send it to worklive@outlook.com.", "My email is john.smith@gmail.com."], pasted
