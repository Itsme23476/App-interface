"""Filect "Agents" page — the engine (Windows port of the Mac app's assistant_page.py, Claude Code first).

Type a task and a local Claude Code agent carries it out — you watch each step stream in live, and
whenever it wants to do something risky (write a file, run a command) a card asks you to Approve or
Deny. Each task is a real, saved Claude Code session (resumable, and visible in Claude). The agent work
runs on worker threads; this file bridges it to the page (agents_view.AgentsView, a pure view).

Ported as-is from the Mac: AgentSession registry, AgentWorker (QThread + asyncio, one per turn, signals
connected to BOUND methods so every handler runs on the main thread), focus, approvals, Stop (interrupt,
then force-cancel after 2.5 s), setup errors + "your task runs by itself once you've signed in", past
chats, quit-safety (§7a). Not yet on Windows (later phases): Codex + hand-over, the voice chord + intent
router, the top-centre card, the screen rail, screenshots, notifications.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QThread, Signal, QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QCheckBox, QApplication

from app.ui import details_view

# Where the agent works by default. A dedicated folder keeps it out of the way and makes the saved
# sessions easy to find.
DEFAULT_WORKSPACE = str(Path(os.path.expanduser("~")) / "Filect Assistant")

_log = logging.getLogger("ai_file_organizer")
_TRACE = os.environ.get("FILECT_ASSISTANT_TRACE", "1") != "0"


def _trace(msg: str) -> None:
    """Every routing decision + session transition, so a test run reads back from the log ("[asst]")."""
    if _TRACE:
        _log.info("[asst] %s", msg)


def _play_chime(ok: bool = True) -> None:
    """A finished (or failed) task: a short system sound."""
    try:
        import sys
        if sys.platform.startswith("win"):
            import winsound
            winsound.MessageBeep(winsound.MB_OK if ok else winsound.MB_ICONHAND)
        else:
            QApplication.beep()
    except Exception:
        pass


def _notify(title: str, body: str) -> None:
    """A background chat finished / needs you. Windows toasts need a tray icon, which Filect doesn't
    have (X quits, no close-to-tray) — the chime is the cue for now; this is the hook if one is added."""
    try:
        from PySide6.QtWidgets import QSystemTrayIcon
        tray = getattr(QApplication.instance(), "_filect_tray", None)
        if tray is not None and QSystemTrayIcon.supportsMessages():
            tray.showMessage((title or "Filect")[:60], " ".join((body or "").split())[:140])
    except Exception:
        pass


def _app_is_active() -> bool:
    try:
        return QApplication.instance().applicationState() == Qt.ApplicationActive
    except Exception:
        return True


def _setting(name: str, default=None):
    try:
        from app.core.settings import settings as _s
        return getattr(_s, name, default)
    except Exception:
        return default


def _save_setting(**kw) -> None:
    try:
        from app.core.settings import settings as _s
        for k, v in kw.items():
            setattr(_s, k, v)
        _s._save_config()
    except Exception:
        pass


def _offscreen() -> bool:
    return os.environ.get("QT_QPA_PLATFORM", "") == "offscreen"


class AgentWorker(QObject):
    """Runs one AgentRunner turn inside an asyncio loop on a worker thread, and relays everything to the
    UI via Qt signals. Approvals: the agent's async permission callback emits `approvalRequested` and
    then *awaits* a future; the UI resolves it (from the main thread) with `resolve()`, which hands the
    answer back to the loop thread-safely."""

    # Signals carry the session KEY so handlers (bound methods → queued to the MAIN thread) know which
    # conversation they're for. CRITICAL: connect these to bound methods, never lambdas — a lambda has
    # no QObject receiver, so Qt runs it on the WORKER thread, and touching the GUI there crashes.
    step = Signal(str, object)               # key, agent_runner.Step
    approvalRequested = Signal(str, object)  # key, agent_runner.ApprovalRequest
    result = Signal(str, str, float)         # key, session_id, cost_usd
    error = Signal(str, str)                 # key, message
    done = Signal(str)                       # key

    def __init__(self, key: str, cwd: str, prompt: str, resume: str | None = None, bypass: bool = False,
                 model: str | None = None, effort: str | None = None, images: list | None = None,
                 agent: str = "claude"):
        super().__init__()
        self.key = key
        self._agent = agent
        self._cwd = cwd
        self._prompt = prompt
        self._images = list(images or [])
        self._resume = resume
        self._bypass = bypass
        self._model = model
        self._effort = effort
        self._loop: asyncio.AbstractEventLoop | None = None
        self._approval_fut: asyncio.Future | None = None
        self._task: asyncio.Task | None = None
        self._runner = None
        self._allow_all = False

    def run(self):  # executes on the worker thread
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._task = self._loop.create_task(self._go())
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass                        # user pressed Stop — not an error
        except Exception as e:          # surface any failure instead of dying silently
            self.error.emit(self.key, str(e) or type(e).__name__)
        finally:
            try:                        # let the SDK's subprocess transports close inside the loop
                pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            try:
                self._loop.close()
            except Exception:
                pass
            self.done.emit(self.key)

    def stop(self):
        """Stop on ONE click: interrupt the current turn (SDK-sanctioned). Called from the UI thread."""
        loop, runner = self._loop, self._runner
        if loop and runner and not loop.is_closed():
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(runner.interrupt()))

    def cancel(self):
        """Forceful fallback — cancel the task (exits the SDK client, kills the claude subprocess)."""
        loop, task = self._loop, self._task
        if loop and task and not task.done() and not loop.is_closed():
            loop.call_soon_threadsafe(task.cancel)

    def kill_process(self):
        """Quit only: end this turn's `claude` right away (the SDK would otherwise wait up to 5 s for it).
        Uses the SDK's live transport — guarded, since those are its internals."""
        try:
            proc = self._runner._client._transport._process
            pid = getattr(proc, "pid", None)
            if pid and getattr(proc, "returncode", 0) is None:
                import signal
                os.kill(pid, signal.SIGTERM)        # Windows: TerminateProcess
        except Exception:
            pass

    def set_allow_all(self, on: bool):
        """Full access switched on mid-turn: every later tool call in this turn runs without a card."""
        self._allow_all = bool(on)
        if self._runner is not None:
            self._runner.allow_all = bool(on)

    async def _on_approval(self, req) -> bool:
        self._approval_fut = self._loop.create_future()
        self.approvalRequested.emit(self.key, req)
        return await self._approval_fut

    def resolve(self, approved: bool):
        """Called from the UI thread when the user taps Approve/Deny."""
        loop, fut = self._loop, self._approval_fut
        if loop and fut and not fut.done() and not loop.is_closed():
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(approved))

    async def _go(self):
        from app.core.agent_runner import AgentRunner as Runner
        runner = Runner(
            self._cwd,
            on_step=lambda s: self.step.emit(self.key, s),
            on_approval=self._on_approval,
            on_result=lambda sid, cost: self.result.emit(self.key, sid or "", float(cost or 0.0)),
        )
        runner.allow_all = self._allow_all
        self._runner = runner            # exposed so stop() can interrupt the live turn
        await runner.run(self._prompt, resume=self._resume, bypass=self._bypass,
                         model=self._model, effort=self._effort, images=self._images)


_session_keys = itertools.count(1)


class AgentSession:
    """One live (or just-finished, or picked-up) conversation: its worker thread plus all the state the
    page renders from. `AssistantPage` keeps a registry of these and shows ("focuses") one at a time;
    the rest keep running in the background."""

    def __init__(self, key, cwd, prompt, title, bypass, model, effort, resume=None, agent="claude"):
        self.key = key
        self.agent = agent
        self.cwd = cwd
        self.prompt = prompt                 # this turn's input
        self.title = title                   # short conversation name (list row + notification)
        self.bypass = bypass
        self.model = model
        self.effort = effort
        self.resume = resume                 # claude session id being resumed, or None for a new chat
        self.session_id = resume or ""       # real claude id (known for sure after the first result)
        self.status = "working"              # working | needs_you | done | error | stopped
        self.feed_lines: list[str] = []      # plain activity lines (log/tests)
        self.entries: list[dict] = []        # the rich conversation (details_view entries)
        self.last_action = ""                # latest step label ("Writing index.html")
        self.result_text = ""                # what Claude said this turn
        self.final_text = ""                 # …just the text after its last tool call: its answer
        self.error = ""                      # the error / setup message when status == "error"
        self.cost = 0.0
        self.approval_req = None             # pending ApprovalRequest while status == needs_you
        self.pending_followup = None         # a follow-up sent while this one was still running
        self.images: list[str] = []
        self.pending_images: list[str] = []
        self.stopping = False                # user hit Stop on THIS session
        self.thread = None
        self.worker = None
        self.started_at = time.time()        # this turn's start (a picked-up past chat: its last activity)
        self.duration = None
        self.preview = prompt                # for search

    @property
    def running(self) -> bool:
        return self.thread is not None


class AssistantPage(QWidget):
    _recent_loaded = Signal(object)            # [SessionInfo] — Filect's chats (the workspace + continued ones)
    _all_loaded = Signal(object)               # [SessionInfo] — every Claude Code chat (search "other chats")
    _transcript_loaded = Signal(str, object)   # (session id, load_transcript() dict|None)
    _setup_checked = Signal(str, bool)         # (agent, ready)
    _models_loaded = Signal(object)            # [model dicts] from Claude Code

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._sessions: dict[str, AgentSession] = {}
        self._focused_key: str | None = None
        self._recorder = None
        self._recording = False
        self._resume_target = None
        self._recent = []            # Filect's past chats on disk
        self._all_sessions = []      # every Claude Code chat (search)
        self._last_notified_key = None
        self._last_sent_key = None
        self._setup_wait = None
        self._setup_timer = None
        self._setup_checking = False
        self._closing = False
        from app.core import agent_runner as _ar
        self._claude_models = _ar.cached_claude_models()
        self._model = _setting("assistant_model", "") or "opus"
        self._effort = _setting("assistant_effort", "") or "high"
        self._build()
        self._recent_loaded.connect(self._on_recent_loaded)
        self._all_loaded.connect(self._on_all_loaded)
        self._transcript_loaded.connect(self._on_transcript_loaded)
        self._setup_checked.connect(self._on_setup_checked)
        self._models_loaded.connect(self._on_models_loaded)
        self._page_timer = QTimer(self)
        self._page_timer.setSingleShot(True)
        self._page_timer.setInterval(60)
        self._page_timer.timeout.connect(self._refresh_page)
        self._first_shown = False     # past chats + the model list load the first time you open the tab
        try:
            from app.ui.theme_manager import theme_manager
            theme_manager.theme_changed.connect(self.apply_theme)
        except Exception:
            pass
        try:
            QApplication.instance().aboutToQuit.connect(self.shutdown)
        except Exception:
            pass

    # ---- UI ----
    def _build(self):
        from app.ui.agents_view import AgentsView
        theme = _setting("theme", "light") or "light"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        v = AgentsView(theme)
        self.view = v
        lay.addWidget(v)
        # The Mac engine's handles, so the ported code reads the same.
        self.task_input = v.composer.input
        self.run_btn = v.composer.send_btn
        self.mic_btn = v.composer.mic_btn
        self.status_label = QLabel(self)
        self.status_label.hide()
        self.bypass_check = QCheckBox(self)
        self.bypass_check.hide()
        self.bypass_check.setChecked(bool(_setting("assistant_bypass", False)))
        v.send_requested.connect(self._on_send)
        v.stop_requested.connect(self._on_stop_task)
        v.new_chat_requested.connect(self._new_chat)
        v.chat_picked.connect(self._on_chat_picked)
        v.approve.connect(self._decide)
        v.approve_always.connect(self._approve_always)
        v.access_changed.connect(self._on_access_changed)
        v.model_changed.connect(self._on_model_changed)
        v.open_in.connect(self._open_task)
        v.mic_clicked.connect(self._toggle_mic)
        v.list_collapsed_changed.connect(lambda col: _save_setting(agents_list_collapsed=bool(col)))
        v.tour_opened.connect(lambda: _save_setting(agents_tour_seen=True))
        v.setup_requested.connect(self._open_agent_setup)
        v.set_access(self.bypass_check.isChecked())
        v.set_models(self._claude_models, self._model, self._effort)
        self._effort = v._effort or ""
        v.set_list_collapsed(bool(_setting("agents_list_collapsed", True)))
        v.set_tour_seen(bool(_setting("agents_tour_seen", False)))

    def apply_theme(self, theme=None):
        try:
            self.view.apply_theme(theme or _setting("theme", "light") or "light")
        except Exception as e:
            _trace(f"theme failed: {e}")

    def showEvent(self, e):
        super().showEvent(e)
        if not self._first_shown:
            # Lazily, so users who never open Agents never pay for it: Filect's past chats + every Claude
            # Code chat (search) — both off the UI thread — and Claude Code's model list (it starts Claude
            # Code once, ~2-5 s, in the background; never under the offscreen test platform).
            self._first_shown = True
            self._load_recent()
            self._load_all_sessions()
            if not _offscreen():
                QTimer.singleShot(400, self._load_models)
        self._refresh_page()

    def _schedule_page(self):
        """Every engine event lands here: one repaint 60 ms later (bursts of steps → one paint)."""
        if not self._page_timer.isActive():
            self._page_timer.start()

    def _refresh_page(self):
        if self._closing:
            return
        v = self.view
        v.set_chats(self._rows(), self._other_rows())
        sess = self._focused
        if not self.isVisible():
            return
        if sess is None:
            v.show_empty()
            v.set_running(False)
            v.set_open_state(False, _setting("assistant_claude_open_in", "app") or "app", self._has_claude_app())
            return
        v.show_chat({
            "key": sess.key, "entries": sess.entries, "status": sess.status, "running": sess.running,
            "approval": sess.approval_req, "last_action": sess.last_action, "queued": sess.pending_followup,
            "bypass": self.bypass_check.isChecked(), "error": sess.error, "agent": sess.agent,
            "setup_kind": self._setup_kind(sess.error) if sess.status == "error" else "",
        })
        v.set_running(sess.running)
        v.set_open_state(bool(sess.session_id or sess.resume),
                         _setting("assistant_claude_open_in", "app") or "app", self._has_claude_app())

    _claude_app = None

    def _has_claude_app(self) -> bool:
        if AssistantPage._claude_app is None:
            try:
                from app.core.agent_runner import has_app_for
                AssistantPage._claude_app = bool(has_app_for("claude://resume"))
            except Exception:
                AssistantPage._claude_app = False
        return AssistantPage._claude_app

    @staticmethod
    def _setup_kind(msg):
        try:
            from app.core.agent_runner import setup_kind
            return setup_kind(msg or "")
        except Exception:
            return ""

    def _rows(self):
        rows = []
        live_ids = set()
        for s in self._sessions.values():
            sid = s.session_id or s.resume
            if sid:
                live_ids.add(sid)
            rows.append({"id": f"s:{s.key}", "title": s.title, "status": s.status, "agent": s.agent,
                         "ts": s.started_at, "focused": s.key == self._focused_key, "preview": s.preview,
                         "sub": Path(s.cwd).name})
        for r in self._recent:
            if r.id in live_ids:
                continue
            rows.append({"id": f"r:{r.id}", "title": self._display_title(r), "status": "done", "agent": "claude",
                         "ts": r.mtime, "focused": False, "preview": r.preview, "sub": r.project})
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return rows

    def _display_title(self, info) -> str:
        """A Filect chat's name: the same short title it had while it ran ("Build Coffee Website"), not
        its whole first message — unless Claude Code has a real title for it (renamed / summarized)."""
        t = (info.title or "").strip()
        if info.preview and t == info.preview[:60].strip() and \
                os.path.normcase(os.path.normpath(info.cwd or "")) == os.path.normcase(os.path.normpath(DEFAULT_WORKSPACE)):
            try:
                from app.core.agent_sessions import generate_title
                return generate_title(info.preview) or t
            except Exception:
                return t
        return t

    def _other_rows(self):
        mine = {r.id for r in self._recent} | {s.session_id or s.resume for s in self._sessions.values()}
        return [{"id": f"r:{r.id}", "title": r.title, "status": "done", "agent": "claude", "ts": r.mtime,
                 "focused": False, "preview": r.preview, "sub": r.project}
                for r in self._all_sessions if r.id not in mine]

    # ---- session registry helpers ----
    @property
    def _focused(self) -> "AgentSession | None":
        return self._sessions.get(self._focused_key) if self._focused_key else None

    def _session_by_claude_id(self, sid: str | None):
        if not sid:
            return None
        for s in self._sessions.values():
            if s.session_id == sid or s.resume == sid:
                return s
        return None

    def _any_running(self) -> bool:
        return any(s.running for s in self._sessions.values())

    def _model_for(self, agent: str = "claude"):
        return (self._model or None), (self._effort or None)

    def shutdown(self):
        """App quit: end every running agent and wait for its thread. Without this, quitting mid-task
        destroys a running QThread → Qt aborts with "QThread: Destroyed while thread is still running"."""
        if self._closing:
            return
        self._closing = True
        t0 = time.time()
        live = [s for s in self._sessions.values() if s.running]
        for sess in live:
            if sess.worker is not None:
                try:
                    sess.worker.cancel()            # exits the SDK client → kills the claude subprocess
                except Exception:
                    pass
        # One shared deadline for all of them (not 3 s EACH). The SDK gives Claude Code up to 5 s to save the
        # chat file before it kills it — at quit, after 1.5 s we end any claude still running ourselves, so
        # every thread finishes before Qt tears the page down (a QThread destroyed while running = a crash).
        def _wait_all(until):
            for s in live:
                t = s.thread
                if t is not None and t.isRunning():
                    t.quit()
                    t.wait(max(20, int((until - time.time()) * 1000)))
        _wait_all(t0 + 1.5)
        for s in live:
            if s.thread is not None and s.thread.isRunning() and s.worker is not None:
                s.worker.kill_process()
        _wait_all(time.time() + 3.0)
        self._stop_mic_quietly()
        if live:
            _trace(f"shutdown: stopped {len(live)} running agent(s) in {time.time() - t0:.2f}s")

    # ---- what you did on the page ----
    def _on_send(self, text: str):
        """Enter / ↑. Typing in an open chat continues it; with no chat open it's a new one."""
        text = (text or "").strip()
        if not text:
            return
        self.view.set_input_text("")
        sess = self._focused
        if sess is not None:
            if sess.running:                                # still working → a queued follow-up
                sess.pending_followup = text
                self._last_sent_key = sess.key
                _trace(f"queue follow-up on {sess.key} '{sess.title}' (running)")
                self._schedule_page()
                return
            sid = sess.session_id or sess.resume
            if sid:
                self.run_task(text, resume=sid, cwd=sess.cwd)
            else:                                           # its first turn failed before it got an id
                self._restart_session(sess, text, self.bypass_check.isChecked())
            return
        self.run_task(text)

    def _new_chat(self):
        """＋ New chat: the next task starts fresh. Running chats keep running (and stay in the list)."""
        self._resume_target = None
        self._focused_key = None
        _trace("new chat")
        self.view.set_input_text("")
        self.view.focus_input()
        self._refresh_page()

    def _on_chat_picked(self, rid: str):
        """A row in the list: a chat from this run opens right away; a saved one loads off the UI thread."""
        if rid.startswith("s:"):
            key = rid[2:]
            if key in self._sessions:
                self._focus(key)
            return
        if rid.startswith("r:"):
            self._on_recent_clicked(rid[2:])

    def _on_access_changed(self, full: bool):
        """Ask first ↔ Full access (saved). Turning Full access on mid-turn approves the card that's waiting
        and lets the rest of this turn run without asking."""
        self.bypass_check.setChecked(bool(full))
        _save_setting(assistant_bypass=bool(full))
        _trace(f"access -> {'Full access' if full else 'Ask first'}")
        sess = self._focused
        if sess is not None and sess.worker is not None:
            sess.worker.set_allow_all(bool(full))
            if full and sess.approval_req is not None:
                self._decide(True)
        self._schedule_page()

    def _approve_always(self):
        """"Approve & don't ask again": approve this one and switch to Full access (saved)."""
        self.view.set_access(True)
        self._on_access_changed(True)
        self._decide(True)

    def _on_model_changed(self, model: str, effort: str):
        """Model / thinking picked on the pill — saved; the next turn (of any chat) uses it."""
        self._model, self._effort = model or "", effort or ""
        _save_setting(assistant_model=self._model, assistant_effort=self._effort)
        _trace(f"model -> {self._model or '(default)'} / {self._effort or 'auto'}")

    def _tool_label(self, tool: str, detail: str) -> str:
        """'Writing index.html' from (tool, detail) — a readable action line."""
        verbs = {"Write": "Writing", "Edit": "Editing", "MultiEdit": "Editing", "NotebookEdit": "Editing",
                 "Read": "Reading", "Bash": "Running", "PowerShell": "Running", "Grep": "Searching",
                 "Glob": "Finding", "WebFetch": "Fetching", "WebSearch": "Searching the web",
                 "Task": "Delegating", "Agent": "Delegating", "TodoWrite": "Planning"}
        if str(tool).startswith("mcp__"):
            parts = str(tool).split("__")
            v = f"Using {parts[1]}" if len(parts) > 2 else f"Using {tool}"
        else:
            v = verbs.get(tool, f"Using {tool}")
        return f"{v} {detail}".strip() if detail else v

    def _notch(self, method: str, *args):
        """The top-centre card (Mac notch) isn't on Windows yet — the engine's calls to it land here."""
        return None

    def _open_task(self, where: str = "app"):
        """"Open in …" — open the ACTUAL conversation: "app" → the Claude app (claude://resume), "terminal"
        → Claude Code in a terminal, in the chat's folder, on the same chat. The pick becomes the default."""
        sess = self._focused
        sid = (sess.session_id or sess.resume or "") if sess else ""
        if where in ("app", "terminal") and _setting("assistant_claude_open_in", "app") != where:
            _save_setting(assistant_claude_open_in=where)
        if not sid:
            return
        try:
            from app.core.agent_runner import open_in_claude
            _trace(f"open {sess.key} in {where}")
            open_in_claude(sid, sess.cwd, where)
        except Exception as e:
            _trace(f"open failed: {e}")
        self._schedule_page()

    # ---- running tasks ----
    def run_task(self, prompt: str, bypass: bool | None = None, resume: str | None = None,
                 cwd: str | None = None, images=None, agent: str | None = None):
        """Start a NEW agent conversation, or continue an existing one. New tasks each get their own
        AgentSession and run CONCURRENTLY. Resuming REUSES that conversation's AgentSession: queued as a
        follow-up if it's still running, or a fresh turn on the same session if it's idle — so one
        conversation is one entry for its whole life."""
        prompt = (prompt or "").strip()
        if not prompt:
            return
        if bypass is None:
            bypass = self.bypass_check.isChecked()
        if resume is None and self._resume_target:
            resume = self._resume_target["id"]
            cwd = cwd or self._resume_target["cwd"]
        if resume:
            existing = self._session_by_claude_id(resume)
            if existing is not None and existing.running:
                existing.pending_followup, existing.pending_images = prompt, list(images or [])
                self._last_sent_key = existing.key
                _trace(f"run_task: queue follow-up on {existing.key} '{existing.title}' (running)")
                self._focus(existing.key)
                return
            if existing is not None:
                self._restart_session(existing, prompt, bypass, images)
                return
        workdir = cwd or DEFAULT_WORKSPACE
        if cwd is None:
            os.makedirs(workdir, exist_ok=True)       # only auto-create our OWN default workspace
        from app.core.agent_sessions import generate_title
        display = ((self._resume_target or {}).get("title") or generate_title(prompt)) if resume \
            else generate_title(prompt)
        key = f"s{next(_session_keys)}"
        sess = AgentSession(key, workdir, prompt, display, bypass, *self._model_for(), resume=resume)
        sess.images = list(images or [])
        self._sessions[key] = sess
        self._start_worker(sess, resume)

    def _restart_session(self, sess, prompt, bypass, images=None):
        """Continue a finished conversation with a new turn, REUSING its AgentSession."""
        sess.prompt, sess.bypass, sess.images = prompt, bypass, list(images or [])
        sess.status, sess.result_text, sess.cost, sess.approval_req = "working", "", 0.0, None
        sess.final_text, sess.error, sess.last_action = "", "", ""
        sess.started_at, sess.duration = time.time(), None
        _trace(f"restart {sess.key} '{sess.title}' (new turn, resume={sess.session_id or sess.resume})")
        self._start_worker(sess, sess.session_id or sess.resume or None)

    def _start_worker(self, sess, resume_id, seed: bool = True, send: str | None = None):
        """Build + start this session's worker thread, wire its signals (keyed by session), focus it.
        `seed=False`: the same turn re-run (after setup) — the page already shows it."""
        sess.model, sess.effort = self._model_for(sess.agent)     # the pill's choice applies to every next turn
        sess.thread = QThread(self)
        sess.worker = AgentWorker(sess.key, sess.cwd, send or sess.prompt, resume=resume_id,
                                  bypass=sess.bypass, model=sess.model, effort=sess.effort,
                                  images=sess.images, agent=sess.agent)
        sess.worker.moveToThread(sess.thread)
        sess.thread.started.connect(sess.worker.run)
        # BOUND methods, never lambdas: Qt queues a bound-method slot to its receiver's (main) thread.
        sess.worker.step.connect(self._on_step)
        sess.worker.approvalRequested.connect(self._on_approval)
        sess.worker.result.connect(self._on_result)
        sess.worker.error.connect(self._on_error)
        sess.worker.done.connect(self._on_done)
        _trace(f"spawn {sess.key} '{sess.title}' agent={sess.agent} cwd={sess.cwd} "
               f"resume={resume_id or '(new)'} model={sess.model} effort={sess.effort} bypass={sess.bypass} | "
               f"live now: {sorted(k for k, s in self._sessions.items() if s.running)}")
        sess.thread.start()
        self._last_sent_key = sess.key
        if seed:
            self._focus(sess.key, fresh=True)
        else:
            self._schedule_page()

    # ---- focus: which conversation the page shows (and typing continues) ----
    def _focus(self, key: str, fresh: bool = False):
        sess = self._sessions.get(key)
        if sess is None:
            return
        self._focused_key = key
        _trace(f"focus -> {key} '{sess.title}' fresh={fresh} status={sess.status}")
        if fresh:
            sess.feed_lines = [f"▸ {sess.prompt}"]
            details_view.add_step(sess.entries, "user", text=sess.prompt, images=sess.images)
            sess.last_action = "Starting…"
        self._refresh_page()
        self.view.focus_input()

    def _on_recent_clicked(self, sid):
        """A past chat from disk — load its transcript off the UI thread (_on_transcript_loaded shows it).
        Already picked up this run → just show it."""
        live = self._session_by_claude_id(sid)
        if live is not None:
            self._focus(live.key)
            return
        info = self._info_for(sid)
        if info is None:
            return

        def worker():
            try:
                from app.core.agent_sessions import load_transcript
                data = load_transcript(sid, info.cwd)
            except Exception:
                data = None
            try:
                self._transcript_loaded.emit(sid, data)
            except RuntimeError:                    # page torn down before the read finished (quit)
                pass
        threading.Thread(target=worker, daemon=True, name="filect-agents-transcript").start()

    def _info_for(self, sid):
        return next((r for r in list(self._recent) + list(self._all_sessions) if r.id == sid), None)

    def _on_transcript_loaded(self, sid, data):
        """Pick a past chat back up: it becomes a finished (not running) conversation with its real
        transcript, and the focus — so your next message continues THIS chat (resume). Its time stays its
        last activity, so just viewing it doesn't bump it to Today."""
        live = self._session_by_claude_id(sid)
        if live is not None:
            self._focus(live.key)
            return
        info = self._info_for(sid)
        if info is None:
            return
        lines, prompt, result, entries = self._transcript_view(data) if data else ([], "", "", [])
        prompt = prompt or info.preview or info.title
        sess = AgentSession(f"s{next(_session_keys)}", info.cwd, prompt, self._display_title(info),
                            self.bypass_check.isChecked(), *self._model_for(), resume=sid)
        sess.status, sess.result_text = "done", result
        sess.feed_lines = lines or [f"▸ {prompt}"]
        sess.entries = entries or [{"id": "0", "kind": "user", "text": prompt}]
        sess.started_at, sess.duration = info.mtime, (data or {}).get("duration")
        sess.preview = info.preview
        self._sessions[sess.key] = sess
        _trace(f"picked up past chat {sess.key} '{sess.title}' id={sid[:8]} ({len(sess.entries)} entries)")
        self._focus(sess.key)

    def _transcript_view(self, data):
        """(feed lines, last prompt, last turn's replies, rich entries) for a saved conversation, in the SAME
        format live tasks use — so a past chat reads exactly like one you watched run."""
        from app.core.agent_runner import _tool_detail
        from app.core.agent_sessions import strip_speak_note
        lines, prompt, result, entries = [], "", [], []
        for st in data.get("steps", []):
            if st[0] == "result":
                details_view.add_step(entries, "result", tool_id=st[1], text=st[2], is_error=st[3])
            elif st[0] == "user":
                said = strip_speak_note(st[1])
                prompt, result = said, []
                lines.append(f"▸ {said}")
                details_view.add_step(entries, "user", text=said)
            elif st[0] == "text":
                result.append(st[1])
                lines.append(st[1])
                details_view.add_step(entries, "text", text=st[1])
            else:
                label = self._tool_label(st[1], _tool_detail(st[1], st[2]))
                if not lines or lines[-1] != label:
                    lines.append(label)
                details_view.add_step(entries, "tool", tool=st[1], input=st[2],
                                      tool_id=st[3] if len(st) > 3 else "")
        return lines[-300:], prompt, " ".join(result), entries

    def _load_recent(self):
        """Filect's past chats (its workspace folder + chats it continued elsewhere), off the UI thread."""
        def worker():
            try:
                from app.core.agent_sessions import list_sessions, remembered_sessions
                found = list_sessions(limit=300, cwd=DEFAULT_WORKSPACE) + remembered_sessions(DEFAULT_WORKSPACE)
                seen, items = set(), []
                for s in found:
                    if s.id not in seen and not self._junk_session(s):
                        seen.add(s.id)
                        items.append(s)
                items.sort(key=lambda s: s.mtime, reverse=True)
            except Exception as e:
                _trace(f"past chats failed: {e}")
                items = []
            try:
                self._recent_loaded.emit(items)
            except RuntimeError:
                pass
        threading.Thread(target=worker, daemon=True, name="filect-agents-recent").start()

    def _load_all_sessions(self):
        """Every Claude Code chat on this PC (for search "Other Claude Code chats"), off the UI thread."""
        def worker():
            try:
                from app.core.agent_sessions import list_sessions
                items = [s for s in list_sessions(limit=300) if not self._junk_session(s)]
            except Exception:
                items = []
            try:
                self._all_loaded.emit(items)
            except RuntimeError:
                pass
        threading.Thread(target=worker, daemon=True, name="filect-agents-all").start()

    def _on_recent_loaded(self, items):
        self._recent = list(items or [])
        self._schedule_page()

    def _on_all_loaded(self, items):
        self._all_sessions = list(items or [])
        self._schedule_page()

    @staticmethod
    def _junk_session(ss) -> bool:
        """Saved 'sessions' that aren't real conversations (router queries, continued-context stubs,
        empty titles)."""
        tl = (getattr(ss, "title", "") or "").strip().lower()
        return (not tl or tl.startswith("current conversation:") or "spoken command:" in tl
                or tl.startswith("this session is being continued"))

    def _load_models(self):
        """Claude Code's own model list for this account (~2-5 s, no message sent) — never on the UI thread."""
        def worker():
            try:
                from app.core.agent_runner import claude_models
                models = claude_models()
            except Exception:
                models = []
            if models:
                try:
                    self._models_loaded.emit(models)
                except RuntimeError:
                    pass
        threading.Thread(target=worker, daemon=True, name="filect-agents-models").start()

    def _on_models_loaded(self, models):
        self._claude_models = list(models)
        if self._model not in {m["value"] for m in self._claude_models}:
            self._model = "opus" if any(m["value"] == "opus" for m in self._claude_models) else \
                self._claude_models[0]["value"]
        self.view.set_models(self._claude_models, self._model, self._effort)
        self._effort = self.view._effort or ""

    # ---- voice input (the composer's mic) ----
    def _toggle_mic(self):
        if self._recording:
            self._stop_mic()
        else:
            self._start_mic()

    def _start_mic(self):
        try:
            from app.core.transcription import StreamingTranscriber
            from app.core.settings import settings
        except Exception as e:
            self.view.set_hint(f"Voice unavailable: {e}")
            return
        try:
            self._recorder = StreamingTranscriber(language=(settings.dictation_language or None),
                                                  terms=settings.live_dictation_custom_terms())
            self._recorder.finished.connect(self._on_transcript)
            self._recorder.error.connect(self._on_mic_error)
            self._recording = True
            self.view.set_mic_state("recording")
            self.view.set_hint("Listening… tap the mic again when you're done")
            self._recorder.start()
        except Exception as e:
            self._on_mic_error(str(e))

    def _stop_mic(self):
        self._recording = False
        self.view.set_mic_state("transcribing")
        self.view.set_hint("Writing down what you said…")
        r = self._recorder
        if r is not None:
            try:
                r.stop_recording()
            except Exception as e:
                self._on_mic_error(str(e))

    def _stop_mic_quietly(self):
        r, self._recorder = self._recorder, None
        if r is not None:
            try:
                r.stop_recording()
            except Exception:
                pass

    def _on_transcript(self, text: str):
        text = (text or "").strip()
        if text:
            cur = self.view.input_text().strip()
            self.view.set_input_text((cur + " " + text).strip() if cur else text)
            self.view.focus_input()
        self._reset_mic()

    def _on_mic_error(self, msg: str):
        self._reset_mic()
        self.view.set_hint(f"Voice error: {msg}"[:160])

    def _reset_mic(self):
        self._recording = False
        self._recorder = None
        self.view.set_mic_state("idle")
        self.view.set_hint("")

    # ---- the agent's events (all on the main thread) ----
    def _on_step(self, key, step):
        sess = self._sessions.get(key)
        if sess is None:
            return
        if step.kind == "tool":
            label = self._tool_label(step.tool, getattr(step, "detail", ""))
            sess.last_action, sess.status = label, "working"
            sess.final_text = ""
            _trace(f"{key} · {label}")
            if not sess.feed_lines or sess.feed_lines[-1] != label:
                sess.feed_lines.append(label)
            details_view.add_step(sess.entries, "tool", tool=step.tool, input=getattr(step, "input", None),
                                  tool_id=getattr(step, "tool_id", ""))
        elif step.kind == "text" and step.text:
            sess.result_text = (sess.result_text + " " + step.text).strip()
            sess.final_text = f"{sess.final_text}\n\n{step.text}".strip()
            sess.status = "working"
            sess.last_action = "Writing the answer"
            if not sess.feed_lines or sess.feed_lines[-1] != step.text:
                sess.feed_lines.append(step.text)
            details_view.add_step(sess.entries, "text", text=step.text)
        elif step.kind == "result":
            details_view.add_step(sess.entries, "result", text=step.text, tool_id=step.tool_id,
                                  is_error=getattr(step, "is_error", False))
            if getattr(step, "is_error", False):
                _trace(f"{key} ✗ {step.text[:120]!r}")
        self._schedule_page()

    def _on_approval(self, key, req):
        sess = self._sessions.get(key)
        if sess is None:
            return
        sess.approval_req, sess.status = req, "needs_you"
        _trace(f"{key} NEEDS APPROVAL: {req.tool} (focused={key == self._focused_key})")
        self._schedule_page()
        if key != self._focused_key or not _app_is_active():
            self._notify_needs_you(sess)

    def _decide(self, approved: bool):
        """Approve/Deny on the page — answers the FOCUSED session's gate."""
        sess = self._focused
        if sess is None or sess.approval_req is None:
            return
        _trace(f"{sess.key} {'APPROVED' if approved else 'DENIED'}")
        sess.approval_req, sess.status = None, "working"
        sess.feed_lines.append("✓ approved" if approved else "✗ denied")
        if sess.worker:
            sess.worker.resolve(approved)
        self._schedule_page()

    def _on_result(self, key, session_id: str, cost: float):
        sess = self._sessions.get(key)
        if sess is None:
            return
        if session_id:
            sess.session_id = session_id
            if sess.cwd and os.path.normcase(os.path.normpath(sess.cwd)) != \
                    os.path.normcase(os.path.normpath(DEFAULT_WORKSPACE)):
                try:                                # a chat continued in another project: keep it listed
                    from app.core.agent_sessions import remember_session
                    remember_session(session_id, sess.cwd)
                except Exception:
                    pass
        sess.cost, sess.status = float(cost or 0.0), "done"
        sess.duration = time.time() - sess.started_at
        sess.last_action = ""
        _trace(f"{key} DONE '{sess.title}' sid={session_id or '(none)'} cost=${float(cost or 0):.4f} "
               f"focused={key == self._focused_key}")
        self._schedule_page()
        if not sess.stopping:
            _play_chime(True)
            if key != self._focused_key or not _app_is_active():
                self._last_notified_key = key
                _notify("Filect · task finished", sess.title or "Done")

    def _open_agent_setup(self, agent: str, kind: str):
        """The error card's "Set up / Sign in to Claude Code →": a terminal with the official setup. The task
        on this chat then waits for you and runs by itself once Claude Code is ready."""
        try:
            from app.core.agent_runner import open_claude_setup
            open_claude_setup(kind)
        except Exception as e:
            _trace(f"{agent} setup failed to open: {e}")
            return
        self._setup_wait = {"agent": agent, "key": self._focused_key, "until": time.time() + 600}
        if self._setup_timer is None:
            self._setup_timer = QTimer(self)
            self._setup_timer.setInterval(2000)
            self._setup_timer.timeout.connect(self._check_setup)
        self._setup_timer.start()
        _trace(f"waiting for {agent} setup to finish (task {self._focused_key})")

    def _check_setup(self):
        w = self._setup_wait
        if not w or time.time() > w["until"]:
            self._setup_timer.stop()
            self._setup_wait = None
            return
        if self._setup_checking:
            return
        self._setup_checking = True
        agent = w["agent"]

        def worker():                               # off the UI thread: it runs `claude auth status`
            try:
                from app.core.agent_runner import claude_ready
                ok = claude_ready()
            except Exception:
                ok = False
            try:
                self._setup_checked.emit(agent, ok)
            except RuntimeError:
                pass
        threading.Thread(target=worker, daemon=True, name="filect-setup-check").start()

    def _on_setup_checked(self, agent: str, ok: bool):
        self._setup_checking = False
        w = self._setup_wait
        if not ok or not w or w["agent"] != agent:
            return
        self._setup_timer.stop()
        self._setup_wait = None
        _trace(f"{agent} is ready — running the waiting task {w['key']}")
        self._run_after_setup(w["key"], agent)

    def _run_after_setup(self, key, agent: str):
        """You finished the setup: run the task that was waiting on it (same chat, same request). Not if
        you've since re-sent or stopped it yourself."""
        sess = self._sessions.get(key) if key else None
        if sess is None or sess.running or sess.status != "error":
            return
        sess.status, sess.result_text, sess.approval_req, sess.final_text, sess.error = "working", "", None, "", ""
        sess.started_at, sess.duration = time.time(), None
        sess.last_action = "Claude Code is ready — running your task"
        self._start_worker(sess, sess.session_id or sess.resume or None, seed=False)

    def _on_error(self, key, msg: str):
        sess = self._sessions.get(key)
        if sess is None:
            return
        if sess.stopping:                           # a Stop that cancelled the turn — not a failure
            return
        sess.status, sess.error = "error", msg
        sess.last_action = ""
        _trace(f"{key} ERROR '{sess.title}': {msg}")
        self._schedule_page()
        _play_chime(False)
        if key != self._focused_key or not _app_is_active():
            self._last_notified_key = key
            kind = self._setup_kind(msg)
            _notify("Filect · set up Claude Code" if kind in ("install", "signin") else "Filect · task failed", msg)

    def _on_stop_task(self):
        """Stop on ONE click: interrupt the FOCUSED session's turn now, and force-cancel after 2.5 s if
        the interrupt didn't end it (Claude Code on Windows can take ~1-3 s to honour it)."""
        sess = self._focused
        if sess is None or not sess.running:
            return
        _trace(f"{sess.key} STOP requested '{sess.title}'")
        sess.stopping = True
        sess.pending_followup = None
        sess.last_action = "Stopping…"
        if sess.approval_req is not None:           # waiting on your OK: Stop = "no", so the turn can end now
            sess.approval_req = None
            sess.worker.resolve(False)
        sess.worker.stop()
        self._schedule_page()
        QTimer.singleShot(2500, lambda k=sess.key: self._force_stop(k))

    def _force_stop(self, key):
        sess = self._sessions.get(key)
        if sess is not None and sess.running and sess.worker is not None:
            _trace(f"{key} force-stopping")
            sess.worker.cancel()

    def _on_done(self, key):
        sess = self._sessions.get(key)
        if sess is None:
            return
        _trace(f"{key} worker ended (stopping={sess.stopping}, pending_followup={bool(sess.pending_followup)})")
        if sess.thread:                             # tear down THIS session's worker thread
            sess.thread.quit()
            sess.thread.wait(3000)
            sess.thread.deleteLater()
        sess.thread = None
        if sess.worker is not None:
            sess.worker.deleteLater()
        sess.worker = None
        if sess.stopping:                           # user hit Stop — don't fire any queued follow-up
            sess.stopping = False
            sess.pending_followup = None
            sess.approval_req = None
            sess.status = "stopped"
            sess.last_action = ""
            self._schedule_page()
            return
        if sess.status == "working":                # ended without a result or an error
            sess.status = "done"
        if sess.pending_followup:                   # sent while this was running — continue the SAME chat
            text, sess.pending_followup = sess.pending_followup, None
            imgs, sess.pending_images = sess.pending_images, []
            sid = sess.session_id or sess.resume
            if sid:
                self.run_task(text, resume=sid, cwd=sess.cwd, images=imgs)
            else:
                self._restart_session(sess, text, self.bypass_check.isChecked(), imgs)
            return
        self._schedule_page()

    def _notify_needs_you(self, sess):
        """A background chat hit an approval gate — ping so it doesn't stall unseen."""
        _play_chime(False)
        tool = getattr(sess.approval_req, "tool", "") if sess.approval_req else ""
        self._last_notified_key = sess.key
        _trace(f"NOTIFY needs-you: {sess.title!r} ({tool})")
        _notify("Filect · needs your approval", f"{sess.title}: {tool}".strip(" :"))
