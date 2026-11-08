"""Rich Details (ported from the Mac's test_details_view, Windows additions marked).
(1) agent_runner._steps_from_message maps REAL SDK message objects → text / tool / result steps;
(2) details_view renders each action with its substance (previews, sizes, Show full, Open / Open in browser /
    moved or deleted, ✗ + reason, edit diffs, commands with ✓/✗ + last output lines, search, plan);
(3) deliverable / final reply;
(Windows) the page palette swaps the colours; PowerShell commands render like Bash; Consolas for code."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from claude_agent_sdk import AssistantMessage, UserMessage, TextBlock, ToolUseBlock, ToolResultBlock, ThinkingBlock  # noqa: E402
from app.core.agent_runner import _steps_from_message  # noqa: E402
from app.ui import details_view as dv                 # noqa: E402

ESSAY = "\n".join([f"Paragraph line {i} about habits and how they compound." for i in range(1, 21)])


def _f(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _entries(*steps):
    e = []
    for s in steps:
        dv.add_step(e, **s)
    return e


def _text(html):
    doc = dv.QTextDocument()
    doc.setHtml(html)
    return doc.toPlainText()


def test_steps_from_real_sdk_messages():
    msg = AssistantMessage(content=[ThinkingBlock("plan", "sig"), TextBlock("On it."),
                                    ToolUseBlock(id="t1", name="Write",
                                                 input={"file_path": r"C:\x\essay.md", "content": "hi there"})],
                           model="claude")
    st = _steps_from_message(msg)
    assert [s.kind for s in st] == ["text", "tool"], st                    # thinking skipped
    assert st[1].tool_id == "t1" and st[1].input["content"] == "hi there" and st[1].detail == "essay.md"
    res = _steps_from_message(UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="File created",
                                                                   is_error=False)]))
    assert len(res) == 1 and res[0].kind == "result" and res[0].tool_id == "t1" and res[0].text == "File created"
    err = _steps_from_message(UserMessage(content=[ToolResultBlock(
        tool_use_id="t2", content=[{"type": "text", "text": "Permission denied"}], is_error=True)]))
    assert err[0].is_error and err[0].text == "Permission denied"
    assert _steps_from_message(UserMessage(content="echoed prompt")) == []


def test_render_actions(tmp_path):
    essay = _f(tmp_path, "essay.md", ESSAY)
    site = _f(tmp_path, "index.html", "<html>\n<title>Filect</title>\n</html>")
    e = _entries(dict(kind="user", text="write me an essay"),
                 dict(kind="tool", tool="Write", input={"file_path": essay, "content": ESSAY}, tool_id="w1"),
                 dict(kind="result", tool_id="w1", text="ok"),
                 dict(kind="tool", tool="Write", input={"file_path": site, "content": "<html>\n<title>Filect</title>\n</html>"}, tool_id="w2"),
                 dict(kind="result", tool_id="w2", text="ok"),
                 dict(kind="tool", tool="Write", input={"file_path": r"C:\gone\old.md", "content": "x"}, tool_id="w3"),
                 dict(kind="result", tool_id="w3", text="ok"),
                 dict(kind="tool", tool="Write", input={"file_path": r"C:\x\README.md", "content": "y"}, tool_id="w4"),
                 dict(kind="result", tool_id="w4", text="<tool_use_error>Tool permission request failed: Stream closed</tool_use_error>", is_error=True),
                 dict(kind="tool", tool="Edit", input={"file_path": r"C:\x\style.css", "old_string": "a{}\nb{}", "new_string": "a{color:blue}"}, tool_id="e1"),
                 dict(kind="result", tool_id="e1", text="ok"),
                 dict(kind="tool", tool="Bash", input={"command": "npm test", "description": "Run the tests"}, tool_id="b1"),
                 dict(kind="result", tool_id="b1", text="out-1\nout-2\nout-3\nout-4\nFAIL: 2 tests", is_error=True),
                 dict(kind="tool", tool="WebSearch", input={"query": "best habit books"}, tool_id="s1"),
                 dict(kind="tool", tool="TodoWrite", input={"todos": [{"content": "Outline", "status": "completed"},
                                                                       {"content": "Draft", "status": "in_progress"},
                                                                       {"content": "Polish", "status": "pending"}]}, tool_id="p1"),
                 dict(kind="text", text="Here is a long reply. " * 60))
    h = dv.render_html(e)
    text = _text(h)
    assert "Wrote essay.md · 180 words" in text and "Paragraph line 1 " in text and "Paragraph line 15" not in text
    assert f'href="{dv.file_url(essay)}"' in h and "Open ↗" in text and "Show full ⌄" in text
    assert "Wrote index.html · 3 lines" in text and "Open in browser ↗" in text
    assert "old.md" in text and "moved or deleted" in text
    assert "Couldn't write README.md" in text and "Tool permission request failed: Stream closed" in text
    assert "tool_use_error" not in text
    assert "Edited style.css · +1 −2" in text and "− b{}" in text and "+ a{color:blue}" in text
    assert "✗" in text and "Run the tests" in text and "$ npm test" in text
    assert "FAIL: 2 tests" in text and "out-3" in text and "out-4" in text
    assert "out-1" not in text and "out-2" not in text, "only the LAST 3 output lines show"
    assert "Searched the web for “best habit books”" in text
    assert "☑ Outline" in text and "◐ Draft" in text and "☐ Polish" in text
    assert text.count("Show full ⌄") >= 3
    full = _text(dv.render_html(e, {e[1]["id"]}))
    assert "Paragraph line 20" in full and "Show less ⌃" in full


def test_deliverable(tmp_path):
    p = _f(tmp_path, "plan.md", "a b c")
    e = _entries(dict(kind="user", text="first"),
                 dict(kind="tool", tool="Write", input={"file_path": p, "content": "a b c"}, tool_id="w1"),
                 dict(kind="result", tool_id="w1", text="ok"),
                 dict(kind="user", text="now fix it"),
                 dict(kind="tool", tool="Write", input={"file_path": r"C:\x\bad.md", "content": "z"}, tool_id="w2"),
                 dict(kind="result", tool_id="w2", text="denied", is_error=True),
                 dict(kind="text", text="I **couldn't** save it.\n\nSecond paragraph."))
    assert dv.deliverable(e) is None, "only the LATEST turn's successful write counts"
    e2 = _entries(dict(kind="user", text="go"),
                  dict(kind="tool", tool="Write", input={"file_path": p, "content": "a b c"}, tool_id="w1"),
                  dict(kind="result", tool_id="w1", text="ok"))
    assert dv.deliverable(e2) == (p, "3 words")
    assert dv.final_reply(e) == "I **couldn't** save it.\n\nSecond paragraph."


def test_windows_powershell_tool_and_palette():
    """(Windows) Claude Code without Git Bash runs commands with a PowerShell tool — it renders like Bash.
    The page renders inside palette(...): its colours, then the card's are back."""
    e = _entries(dict(kind="tool", tool="PowerShell", input={"command": "Get-ChildItem", "description": "List files"},
                      tool_id="p1"),
                 dict(kind="result", tool_id="p1", text="a.txt\nb.txt"))
    with dv.palette(text="#111111", muted="#222222", accent="#333333", green="#444444", red="#555555"):
        h = dv.render_html(e)
    t = _text(h)
    assert "List files" in t and "$ Get-ChildItem" in t and "b.txt" in t and "✓" in t
    assert "#444444" in h and "#222222" in h, "palette colours weren't used"
    assert dv.TEXT == "#D6D6DE" and dv.GREEN == "#6FCF7F", "palette leaked out of its block"
    assert "Consolas" in dv.MONO
