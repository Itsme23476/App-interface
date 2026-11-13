"""A line that shortens itself shows a COMPLETE shorter sentence when the whole one doesn't fit - the Assistant page's
privacy line was cut mid-word at the smallest window ("Only your speech is sent — never th…", 2026-11-13). Without a
short form (History rows, chat names) it still ends in "…" as before. Off-screen; nothing is shown."""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.ui.assistant_history_view import _Elided  # noqa: E402

FULL, SHORT = "Only your speech is sent — never the silence.", "Only your speech is sent."


def _shown(label, width):
    label.show()                                   # (a hidden widget gets no resize events)
    label.resize(width, 20)
    app.processEvents()
    try:
        return label.text()
    finally:
        label.hide()


def test_the_whole_line_when_it_fits():
    lab = _Elided(FULL, short=SHORT)
    assert _shown(lab, lab.fontMetrics().horizontalAdvance(FULL) + 10) == FULL


def test_the_short_sentence_instead_of_a_cut_word():
    lab = _Elided(FULL, short=SHORT)
    w = lab.fontMetrics().horizontalAdvance(SHORT) + 10            # (room for the short one, not the whole)
    assert _shown(lab, w) == SHORT


def test_still_ends_in_dots_when_even_the_short_one_doesnt_fit():
    lab = _Elided(FULL, short=SHORT)
    got = _shown(lab, 60)
    assert got.endswith("…") and got != SHORT


def test_without_a_short_form_nothing_changes():
    lab = _Elided(FULL)
    w = lab.fontMetrics().horizontalAdvance(SHORT) + 10
    got = _shown(lab, w)
    assert got.endswith("…") and got.startswith("Only your speech")


def test_the_assistant_page_uses_it():
    src = (Path(__file__).resolve().parents[1] / "app" / "ui" / "live_page.py").read_text(encoding="utf-8")
    assert '_Elided("Only your speech is sent — never the silence.", short="Only your speech is sent.")' in src
