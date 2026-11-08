"""Filect "Agents" page — the engine (Windows port of the Mac app's assistant_page.py, Claude Code first).

Type a task and a local Claude Code agent carries it out — you watch each step stream in live, and
whenever it wants to do something risky (write a file, run a command) a card asks you to Approve or
Deny. Each task is a real, saved Claude Code session (resumable, and visible in Claude). The agent work
runs on worker threads; this file bridges it to the page (agents_view.AgentsView, a pure view).

Ported as-is from the Mac: AgentSession registry, AgentWorker (QThread + asyncio, one per turn, signals
connected to BOUND methods so every handler runs on the main thread), focus, approvals, Stop (interrupt,
then force-cancel after 2.5 s), setup errors + "your task runs by itself once you've signed in", past
chats, quit-safety (§7a), and Codex as the second agent (§8l): the agent new chats use, each agent's model,
the automatic hand-over (not set up / out of usage → the same turn on the other agent, with a recap), a chat
the Codex app holds (close it and continue, or a new chat), Codex's own chats in the list and the drawer.
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


# After Filect closed the Codex app to continue a chat, it reopens it once you've been quiet on that chat this
# long. Right away raced your next follow-up: Codex takes ~10 s to load a chat, saw Filect writing it, and showed
# "This is open in another app — Retry" (Mac, 2026-11-05).
REOPEN_QUIET_MS = 15000


def _other_agent(agent: str) -> str:
    return "codex" if agent == "claude" else "claude"


def _agent_name(agent: str) -> str:
    return {"claude": "Claude Code", "codex": "Codex"}.get(agent, agent)


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
        Uses the SDK's live transport — guarded, since those are its internals. Codex: its process tree."""
        r = self._runner
        if r is not None and hasattr(r, "kill"):
            try:
                r.kill()
            except Exception:
                pass
            return
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
        if self._agent == "codex":
            from app.core.codex_runner import CodexRunner as Runner
        else:
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
        self.agent = agent                   # "claude" / "codex" — a chat stays with the agent running it
        self.fallback_to = None              # set when this agent can't run → _on_done hands the turn over
        self.fell_back = False               # this turn already switched agents once (never ping-pong)
        self.first_error = ""                # the first agent's reason, if the other one fails too
        self.before_switch = None            # (agent, session_id, resume, model, effort) before a hand-over
        self.codex_held = False              # this turn hit a chat the Codex app holds (_on_done decides)
        self.awaiting_codex = False          # the page / card is asking: close Codex, or a new chat
        self.codex_msg = ""                  # …and what it says ("This chat is open in the Codex app.")
        self.codex_checking = False          # finding out who holds it / closing it (off the UI thread)
        self.busy_in_app = None              # (chat id, title) the Codex app was working on when asked
        self.reopen_codex = None             # chat to reopen the Codex app on once this turn is done
        self.reopen_token = 0                # a newer turn cancels a pending reopen (_reopen_codex_when_quiet)
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
    _voice_decided = Signal(object)            # {intent, idx, task, candidates, focused_key, …} from the router
    _codex_info_ready = Signal(object)         # {models, update, msg} — Codex's live model list (background)
    _codex_released = Signal(str, str, str)    # (session key, what closed "app"/"terminal"/"", interrupted chat)
    _codex_hold_checked = Signal(str, str, object)   # (session key, who holds it, [(id, title)] the app is busy on)
    _codex_opened = Signal(str, str)           # (session key, "opened" | "terminal" | "") — Open in Codex
    _agents_checked = Signal(object)           # {"claude": ready|install|signin, "codex": …} (background)

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._sessions: dict[str, AgentSession] = {}
        self._focused_key: str | None = None      # the chat the PAGE shows (and typing / voice continues)
        # The chat the CARD shows. Usually the same chat, but not always (you picked another one on the page,
        # pressed + New chat, ✕'d the card…) — so every card update / Stop / Approve / Open goes by THIS key.
        # (Windows 2026-11-08: going by the focus left the card frozen on a chat it no longer heard from.)
        self._card_key: str | None = None
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
        self._overlay = None         # the top-centre card (lazy; see _ensure_notch)
        self._rail = None            # the left-edge chat drawer (lazy; see _ensure_rail)
        self._routing = {}           # voice/typed requests still being routed: id -> what you said (Stop cancels)
        self._route_seq = 0
        self._quiet_routes = set()   # routing ids you ✕'d the card during — their task starts with the card tucked
        self._quiet_start = False    # (set while one of those is being dispatched)
        self._from_page = False      # the turn about to start was sent from this page (the card stays tucked)
        from app.core import agent_runner as _ar
        self._claude_models = _ar.cached_claude_models()
        self._model = _setting("assistant_model", "") or "opus"           # Claude Code's pick
        self._effort = _setting("assistant_effort", "") or "high"
        # Codex (§8l): the agent new chats use, Codex's own pick, and its model list (loaded in the background).
        self._primary = _setting("assistant_agent", "claude") if _setting("assistant_agent", "claude") in \
            ("claude", "codex") else "claude"
        self._codex_model = _setting("assistant_codex_model", "") or ""
        self._codex_effort = _setting("assistant_codex_effort", "high") or ""
        self._codex_info = None
        try:
            from app.core.codex_runner import _FALLBACK_MODELS, _FALLBACK_EFFORTS
            self._codex_models = [{"value": v, "name": n, "efforts": list(_FALLBACK_EFFORTS)} for v, n in _FALLBACK_MODELS]
        except Exception:
            self._codex_models = []
        self._down = {}              # agent -> (why, until): found unusable lately (not set up / out of usage)
        self._agents_state = None    # {"claude"/"codex": ready|install|signin} — the background check (None = not yet)
        self._agents_checking = False
        # "Connect Claude Code or Codex" (Mac §8v): the agent whose setup you opened from that card, and until when
        # Filect keeps looking (every 2 s, 10 min) — the card then goes by itself. Neither ready: look again every
        # 5 min too (you may set one up outside Filect).
        self._agents_waiting = None
        self._agents_until = 0.0
        self._agents_poll = QTimer(self)
        self._agents_poll.setInterval(2000)
        self._agents_poll.timeout.connect(self.refresh_agents_setup)
        self._agents_recheck = QTimer(self)
        self._agents_recheck.setInterval(300000)
        self._agents_recheck.timeout.connect(self.refresh_agents_setup)
        self._card_agent = None      # the agent the card shows (its models on the pill, "Open in …")
        self._view_sig = None
        self._build()
        self._recent_loaded.connect(self._on_recent_loaded)
        self._all_loaded.connect(self._on_all_loaded)
        self._transcript_loaded.connect(self._on_transcript_loaded)
        self._setup_checked.connect(self._on_setup_checked)
        self._models_loaded.connect(self._on_models_loaded)
        self._voice_decided.connect(self._on_voice_decided)
        self._codex_info_ready.connect(self._on_codex_info)
        self._codex_released.connect(self._on_codex_released)
        self._codex_hold_checked.connect(self._on_codex_hold_checked)
        self._codex_opened.connect(self._on_codex_opened)
        self._agents_checked.connect(self._on_agents_checked)
        self._page_timer = QTimer(self)
        self._page_timer.setSingleShot(True)
        self._page_timer.setInterval(60)
        self._page_timer.timeout.connect(self._refresh_page)
        self._first_shown = False     # every Claude Code chat + the model list load the first time you open the tab
        self._load_recent()           # Filect's own chats (one folder, off the UI thread) — the drawer lists them
        if not _offscreen():
            # The card (top centre) + the drawer (left edge) are there from the start, summonable without a task.
            QTimer.singleShot(1500, self._show_idle_overlays)
            QTimer.singleShot(1200, self.refresh_agents_setup)   # which agents can run (decides new chats' agent)
            QTimer.singleShot(2000, self._load_codex_info)       # Codex's models (~0.2 s, off the UI thread)
            try:                                          # warm the voice router so the first request isn't cold
                from app.core.intent_router import warm_up
                QTimer.singleShot(4000, warm_up)
            except Exception:
                pass
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
        v.agent_changed.connect(self._set_primary)
        v.agent_setup_requested.connect(self.setup_agent)
        v.codex_choice.connect(self._on_page_codex_choice)
        v.set_access(self.bypass_check.isChecked())
        self._effort = self._coerce_effort("claude", self._model, self._effort)
        self._sync_view_agent()
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
            self._load_all_sessions()
            if not _offscreen():
                QTimer.singleShot(400, self._load_models)
        elif self._agents_state is not None and "ready" not in self._agents_state.values():
            self.refresh_agents_setup()          # neither ready: you may have set one up outside Filect
        self._refresh_page()

    def _schedule_page(self):
        """Every engine event lands here: one repaint 60 ms later (bursts of steps → one paint)."""
        if not self._page_timer.isActive():
            self._page_timer.start()

    def _refresh_page(self):
        if self._closing:
            return
        self._refresh_rail()
        v = self.view
        v.set_chats(self._rows(), self._other_rows())
        sess = self._focused
        if not self.isVisible():
            return
        self._sync_view_agent()
        if sess is None:
            v.show_empty()
            v.set_running(False)
            agent = self._pick_agent()
            v.set_open_state(False, self._open_in(agent), self._has_app(agent))
            return
        v.show_chat({
            "key": sess.key, "entries": sess.entries, "status": sess.status, "running": sess.running,
            "approval": sess.approval_req, "last_action": sess.last_action, "queued": sess.pending_followup,
            "bypass": self.bypass_check.isChecked(), "error": sess.error, "agent": sess.agent,
            "setup_kind": self._setup_kind(sess.error) if sess.status == "error" else "",
            "setup_agent": self._setup_agent(sess.error) if sess.status == "error" else "",
            "codex_choice": ({"msg": sess.codex_msg, "busy": sess.busy_in_app[1] if sess.busy_in_app else ""}
                             if sess.awaiting_codex else None),
        })
        v.set_running(sess.running)
        v.set_open_state(bool(sess.session_id or sess.resume), self._open_in(sess.agent), self._has_app(sess.agent))

    _claude_app = None
    _codex_app = None

    def _has_claude_app(self) -> bool:
        if AssistantPage._claude_app is None:
            try:
                from app.core.agent_runner import has_app_for
                AssistantPage._claude_app = bool(has_app_for("claude://resume"))
            except Exception:
                AssistantPage._claude_app = False
        return AssistantPage._claude_app

    def _has_app(self, agent: str) -> bool:
        """Is that agent's desktop app installed ("Open in Claude / Codex")?"""
        if agent != "codex":
            return self._has_claude_app()
        if AssistantPage._codex_app is None:
            try:
                from app.core.codex_runner import _desktop_app
                AssistantPage._codex_app = bool(_desktop_app())
            except Exception:
                AssistantPage._codex_app = False
        return AssistantPage._codex_app

    @staticmethod
    def _open_in(agent: str) -> str:
        return _setting(f"assistant_{agent}_open_in", "app") or "app"

    @staticmethod
    def _setup_kind(msg):
        try:
            from app.core.agent_runner import setup_kind
            return setup_kind(msg or "")
        except Exception:
            return ""

    @staticmethod
    def _setup_agent(msg):
        try:
            from app.core.agent_runner import setup_info
            info = setup_info(msg or "")
            return info[0] if info else ""
        except Exception:
            return ""

    # ---- two agents (§8l): which one a new chat uses, each one's model ----
    def _agent_ok(self, agent: str) -> bool:
        """False while `agent` is known unusable: not set up (the background check's saved answer — only READ
        here, so a request never waits for it) or out of usage / just failed (see _on_error)."""
        st = self._agents_state
        if st and st.get(agent) in ("install", "signin"):
            return False
        d = self._down.get(agent)
        return d is None or time.time() >= d[1]

    def _pick_agent(self) -> str:
        """A new chat's agent: your pick, unless it's known unusable right now and the other isn't."""
        other = _other_agent(self._primary)
        return other if not self._agent_ok(self._primary) and self._agent_ok(other) else self._primary

    def _view_agent(self) -> str:
        """The agent the page's composer shows: the open chat's, else the one a new chat would use."""
        sess = self._focused
        return sess.agent if sess is not None else self._pick_agent()

    def _models_of(self, agent: str) -> list:
        return self._codex_models if agent == "codex" else self._claude_models

    def _coerce_effort(self, agent: str, model, effort) -> str:
        """A thinking level that model lacks → Auto ('')."""
        m = next((x for x in self._models_of(agent) if x.get("value") == model), None)
        return effort if (effort and m and effort in (m.get("efforts") or [])) else ("" if m else (effort or ""))

    def _sync_view_agent(self, force: bool = False):
        """The composer's agent + model pill = the open chat's agent (else new chats'), with that agent's list."""
        sess = self._focused
        agent = self._view_agent()
        model, effort = self._model_for(agent)
        sig = (agent, self._primary, sess.agent if sess else None, model, effort, id(self._models_of(agent)))
        if sig == self._view_sig and not force:
            return
        self._view_sig = sig
        self.view.set_agent(agent, self._primary, sess.agent if sess else None)
        self.view.set_models(self._models_of(agent), model or "", effort or "")

    def _set_primary(self, agent: str):
        """The agent menu's "Agent" rows: who new chats (typed, Shift+Z, the card) go to. Saved."""
        if agent not in ("claude", "codex") or agent == self._primary:
            return
        self._primary = agent
        self._down.pop(agent, None)                 # you picked it: try it first again
        _save_setting(assistant_agent=agent)
        _trace(f"primary agent -> {agent}")
        if self._card_key is None:                  # a blank card offers the new pick's models
            self._set_card_agent(self._pick_agent())
        self._sync_view_agent(force=True)
        self._schedule_page()

    def _set_models(self, agent: str, model: str, effort: str):
        """Model / thinking for `agent` (saved) — that agent's next turn (of any chat) uses them."""
        effort = self._coerce_effort(agent, model, effort)
        if agent == "codex":
            self._codex_model, self._codex_effort = model or "", effort or ""
            _save_setting(assistant_codex_model=self._codex_model, assistant_codex_effort=self._codex_effort)
        else:
            self._model, self._effort = model or "", effort or ""
            _save_setting(assistant_model=self._model, assistant_effort=self._effort)
        if self._overlay is not None and (self._card_agent or self._pick_agent()) == agent:
            m, e = self._model_for(agent)
            self._overlay.set_model_effort(m or "", e or "")
        _trace(f"{agent} model -> {model or '(default)'} / {effort or 'auto'}")
        self._sync_view_agent(force=True)

    def refresh_agents_setup(self):
        """Which agents can run (installed + signed in), off the UI thread — so a new chat goes straight to the
        one that works (someone with only Codex never waits on a doomed Claude Code attempt)."""
        check = getattr(self, "_agents_check", None)          # (tests: a fake; never the real CLIs offscreen)
        if self._agents_waiting and time.time() > self._agents_until:
            self._agents_waiting = None              # (gave up waiting after 10 min — the card goes back)
            self._agents_poll.stop()
            self._publish_agents(self._agents_state)
        if self._agents_checking or self._closing or (check is None and _offscreen()):
            return
        self._agents_checking = True

        def work():
            st = None
            try:
                if check is not None:
                    st = check()
                else:
                    from app.core.agent_runner import claude_setup_state
                    from app.core.codex_runner import codex_setup_state
                    st = {"claude": claude_setup_state(), "codex": codex_setup_state()}
            except Exception as e:
                _trace(f"agent setup check failed: {e}")
            try:
                self._agents_checked.emit(st)
            except RuntimeError:
                pass
        threading.Thread(target=work, daemon=True, name="filect-agents-check").start()

    def _on_agents_checked(self, st):
        self._agents_checking = False
        if not st:
            return
        for agent in ("claude", "codex"):
            if st.get(agent) == "ready":
                self._down.pop(agent, None)
        if "ready" in (st.get("claude"), st.get("codex")) and self._agents_waiting:
            _trace(f"agent setup: {self._agents_waiting} side is done — the setup card goes")
            self._agents_waiting = None
            self._agents_poll.stop()
        self._publish_agents(st)

    def _publish_agents(self, st):
        """The agents' readiness → the new-chat screen's "Connect Claude Code or Codex" card, the card's agent."""
        if not st:
            return
        st = {"claude": st.get("claude"), "codex": st.get("codex")}
        if st != {k: (self._agents_state or {}).get(k) for k in ("claude", "codex")}:
            _trace(f"agents: claude={st.get('claude')} codex={st.get('codex')}")
        self._agents_state = st
        try:
            self.view.set_setup(dict(st, waiting=self._agents_waiting))
        except Exception as e:
            _trace(f"setup card failed: {e}")
        both_ready = st.get("claude") == "ready" and st.get("codex") == "ready"
        if both_ready:
            self._agents_recheck.stop()          # (an error triggers a check if that ever changes)
        elif not self._agents_recheck.isActive():
            self._agents_recheck.start()
        if self._card_key is None and self._overlay is not None:
            self._set_card_agent(self._pick_agent())
        self._sync_view_agent()
        self._schedule_page()

    def setup_agent(self, agent: str):
        """The "Connect Claude Code or Codex" card's button: a PowerShell window with the official installer / sign-in
        for `agent` (the same one a task's setup card opens); then Filect looks every 2 s for up to 10 min, and the
        card goes by itself once one is ready."""
        if agent not in ("claude", "codex"):
            return
        kind = (self._agents_state or {}).get(agent) or "install"
        if kind == "ready":
            self.refresh_agents_setup()
            return
        self._down.pop(agent, None)
        try:
            if agent == "codex":
                from app.core.codex_runner import open_codex_setup as open_setup
            else:
                from app.core.agent_runner import open_claude_setup as open_setup
            open_setup(kind)
        except Exception as e:
            _trace(f"{agent} setup failed to open: {e}")
            return
        _trace(f"agent setup: opened {agent} ({kind}) from the setup card")
        self._agents_waiting, self._agents_until = agent, time.time() + 600
        self._agents_poll.start()
        self._publish_agents(self._agents_state)

    def _load_codex_info(self, update_first: bool = False):
        """Codex's live model list + whether a newer Codex exists, off the UI thread (it runs `codex`).
        `update_first`: run Codex's own updater before (the card's "Update Codex")."""
        if self._closing:
            return

        def worker():
            msg = ""
            try:
                from app.core import codex_runner as cr
                if update_first:
                    ok, msg = cr.update_codex()
                    msg = "" if ok else (msg or "Codex couldn't update.")
                info = {"models": cr.codex_model_info(), "update": cr.codex_update_available(), "msg": msg}
            except Exception as e:
                info = {"models": None, "update": "", "msg": str(e) if update_first else ""}
            try:
                self._codex_info_ready.emit(info)
            except RuntimeError:
                pass
        threading.Thread(target=worker, daemon=True, name="filect-codex-info").start()

    def _on_codex_info(self, info):
        self._codex_info = info or {}
        models = self._codex_info.get("models")
        if models:
            self._codex_models = list(models)
            if self._codex_model and self._codex_model not in {m["value"] for m in models}:
                self._codex_model = ""              # (that model is gone: Codex's top one)
        _trace(f"codex models: {[m['value'] for m in self._codex_models]} update={self._codex_info.get('update')!r}"
               + (f" | update failed: {self._codex_info['msg']}" if self._codex_info.get("msg") else ""))
        if self._overlay is not None and self._card_agent == "codex":
            self._set_card_agent("codex", force=True)
        self._sync_view_agent(force=True)

    def _on_update_codex(self):
        """"Update Codex" in the card's model panel: Codex's own updater, then the fresh model list."""
        _trace("updating Codex (model panel)")
        self._load_codex_info(update_first=True)

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
            rows.append({"id": f"r:{r.id}", "title": self._display_title(r), "status": "done",
                         "agent": getattr(r, "agent", "claude") or "claude",
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
        return [{"id": f"r:{r.id}", "title": r.title, "status": "done", "agent": getattr(r, "agent", "claude") or "claude",
                 "ts": r.mtime,
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
        """(model, effort) that agent's next turn runs with. Codex: its top model unless you picked one — the
        model the card and the pill show, passed explicitly."""
        if agent == "codex":
            m = self._codex_model or (self._codex_models[0]["value"] if self._codex_models else "")
            return (m or None), (self._codex_effort or None)
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

    # ---- the top-centre card ("notch") + the left drawer ("rail") — Mac §2 / §7d / §8a–8i ----
    def _show_idle_overlays(self):
        """At start: the card's hotspot (top centre) and the drawer's edge (left) — summonable without a task."""
        if self._closing:
            return
        try:
            self._refresh_rail()
        except Exception as e:
            _trace(f"rail failed: {e}")
        try:
            self._notch("rest_idle")
        except Exception as e:
            _trace(f"card failed: {e}")

    def _ensure_notch(self):
        if self._overlay is not None:
            return self._overlay
        from app.ui.claude_overlay import ClaudeOverlay
        ov = ClaudeOverlay()
        # The card's buttons act on the CARD's chat ("" = none → nothing happens; never the page's chat).
        ov.approve_clicked.connect(lambda: self._decide(True, self._card_key or ""))
        ov.deny_clicked.connect(lambda: self._decide(False, self._card_key or ""))
        ov.open_in.connect(lambda where: self._open_task(where, self._card_key or ""))
        ov.set_open_defaults({a: self._open_in(a) for a in ("claude", "codex")})
        ov.update_requested.connect(self._on_update_codex)
        ov.codex_choice.connect(self._on_card_codex_choice)
        ov.dismissed.connect(self._on_notch_dismissed)
        ov.model_changed.connect(self._on_card_model_changed)
        ov.stop_clicked.connect(self._on_card_stop)
        ov.submit_text.connect(lambda t: self.run_voice_task(t, source="typed"))   # typed = same routing
        ov.collapsed.connect(self._on_notch_collapsed)
        ov.setup_requested.connect(lambda agent, kind: self._open_agent_setup(agent, kind, self._card_key or ""))
        self._overlay = ov
        self._sync_card_models()
        # Safety net: the card must never sit on "Working" / "Needs you" for a chat that isn't running.
        self._card_check = QTimer(self)
        self._card_check.setInterval(3000)
        self._card_check.timeout.connect(self._check_card)
        self._card_check.start()
        return ov

    def _card_shows(self, key) -> bool:
        """Is `key` the chat on the card (so its live updates go there)?"""
        return bool(key) and key == self._card_key

    def _check_card(self):
        """Every 3 s: is the card showing a live state ("Working" / "Needs you") for a chat that's not running
        (and no request is being routed)? Then it's stale — repaint it from that chat's real state, or rest it.
        Logged, so a path that strands the card shows up in the log."""
        ov = self._overlay
        if ov is None or self._closing or self._routing:
            return
        state = getattr(ov, "_state", "idle")
        if state not in ("working", "needs_you"):
            return
        sess = self._sessions.get(self._card_key) if self._card_key else None
        if sess is not None and (sess.running or sess.codex_checking):
            return
        _trace(f"card was stuck on '{state}' for {sess.key if sess else 'no chat'} — resynced")
        if sess is not None:
            self._render_card(sess.key)
        else:
            self._card_key = None
            self._notch("rest_idle", True)

    def _notch(self, method: str, *args):
        """Call a card method — never let the card break the engine or the tab."""
        if self._closing:
            return None
        try:
            ov = self._ensure_notch()
            return getattr(ov, method)(*args)
        except Exception as e:
            _trace(f"card.{method} failed: {e}")
            return None

    def _card_auto(self) -> bool:
        """Does the card open by itself for this turn / answer? "Open answers automatically" (on by default)
        — but not while you're looking at the Agents page itself (the page already shows it all)."""
        if not bool(_setting("assistant_auto_open", True)):
            return False
        try:
            if self.isVisible() and self.window().isActiveWindow():
                return False
        except Exception:
            pass
        return True

    def _page_in_front(self) -> bool:
        """You're looking at the Agents page in Filect's window right now."""
        try:
            return bool(self.isVisible() and self.window().isActiveWindow())
        except Exception:
            return False

    def _sync_card_models(self):
        """The card's model pill = its agent's list (Claude Code's current models / Codex's) + your pick."""
        self._set_card_agent(self._card_agent or self._pick_agent(), force=True)

    def _set_card_agent(self, agent: str, force: bool = False):
        """The card shows `agent`: its models on the pill, "Open in Claude / Codex", "Update Codex"."""
        ov = self._overlay
        if ov is None or (agent == self._card_agent and not force):
            return
        self._card_agent = agent
        try:
            if agent == "codex":
                models = [(m["value"], m["name"]) for m in self._codex_models]
                update = (self._codex_info or {}).get("update", "")
            else:
                pick = self._model or "opus"
                models = [(m["value"], m["name"]) for m in self._claude_models
                          if not str(m["value"]).startswith("claude-") or m["value"] == pick]
                update = ""
            ov.set_agent(agent, models or None, update)
            m, e = self._model_for(agent)
            ov.set_model_effort(m or ("opus" if agent == "claude" else ""), e or "")
        except Exception as e:
            _trace(f"card models failed: {e}")

    def _on_card_model_changed(self, model: str, effort: str):
        """Model / thinking picked on the card — for the agent the card shows."""
        self._set_models(self._card_agent or self._pick_agent(), model, effort)

    def _on_notch_dismissed(self):
        """✕ on the card: the card lets go of its chat and rests (see ClaudeOverlay._on_dismiss); the focus
        pointer clears so the next voice request starts fresh. The chat keeps running (and stays in the
        drawer and the page's list) — reopen it from the drawer."""
        sess = self._sessions.get(self._card_key) if self._card_key else None
        _trace(f"card closed (✕) on {sess.key + ' ' + repr(sess.title) if sess else 'nothing'}"
               f"{' — it keeps running' if sess and sess.running else ''}")
        self._card_key = None
        self._focused_key = None
        self._quiet_routes |= set(self._routing)    # a request still routing: it runs, but doesn't pop the card back
        self._refresh_rail()
        self._schedule_page()

    def _on_notch_collapsed(self):
        """The card tucked away (§8a): the chat you last SENT a request in stays; a chat you only VIEWED
        doesn't — the card goes back to your last chat, or to the blank default."""
        sess = self._sessions.get(self._card_key) if self._card_key else None
        if sess is None or sess.running or sess.key == self._last_sent_key:
            return
        last = self._last_sent_key if self._last_sent_key in self._sessions else None
        _trace(f"card closed on viewed {sess.key} '{sess.title}' -> {last or 'default'}")
        self._resume_target = None
        if self._focused_key == sess.key:           # (the page still shows the chat you viewed → follow)
            self._focused_key = last
        if last:
            self._render_card(last)
        else:
            self._card_key = None
            self._notch("rest_idle")
        self._refresh_rail()
        self._schedule_page()

    def _render_card(self, key=None):
        """Repaint the card from a chat's stored state (focus swap) — `key`, or the focused chat — and make it
        the card's chat."""
        sess = self._sessions.get(key) if key else self._focused
        if sess is None:
            return
        self._card_key = sess.key
        self._set_card_agent(sess.agent)
        self._notch("present", self._card_auto())
        self._notch("load_state", sess.title, list(sess.feed_lines), sess.status, sess.last_action,
                    sess.approval_req, sess.result_text if sess.status != "error" else sess.error, sess.cost,
                    sess.session_id, sess.prompt, sess.started_at, sess.duration, sess.entries)
        if sess.stopping and sess.running:
            self._notch("mark_stopping")
        if sess.awaiting_codex and not sess.running:
            self._notch("show_codex_choice", sess.codex_msg, sess.busy_in_app[1] if sess.busy_in_app else "")

    def _ensure_rail(self):
        if self._rail is not None:
            return self._rail
        try:
            from app.ui.conversation_rail import ConversationRail
            rail = ConversationRail()
            rail.session_clicked.connect(self._on_rail_clicked)
            rail.new_requested.connect(self._on_rail_new_chat)
            rail.recent_clicked.connect(self._on_rail_recent_clicked)
            self._rail = rail
        except Exception as e:
            _trace(f"rail unavailable: {e}")
        return self._rail

    def _refresh_rail(self, key=None):
        if self._closing or _offscreen():
            return
        rail = self._ensure_rail()
        if rail is None:
            return
        import dataclasses
        live_ids = {s.session_id or s.resume for s in self._sessions.values()} - {None, ""}
        recent = [dataclasses.replace(r, title=self._display_title(r)) for r in self._recent if r.id not in live_ids]
        try:
            rail.update_sessions(list(self._sessions.values()), self._card_key, recent)   # highlight = the card's
        except Exception as e:
            _trace(f"rail update failed: {e}")

    def _on_rail_new_chat(self):
        """Drawer "+ New chat": forget the current chat and drop the card open on a blank Ready card."""
        _trace("drawer new chat")
        self._new_chat()
        self._card_key = None                   # the card shows a blank Ready card now (a running chat keeps going)
        self._set_card_agent(self._pick_agent())
        self._notch("show_new_chat")
        self._refresh_rail()

    def _on_rail_clicked(self, key):
        if key in self._sessions:
            _trace(f"drawer click -> show {key} '{self._sessions[key].title}'")
            self._show_in_card(key)

    def _show_in_card(self, key):
        """Picking a chat in the DRAWER drops the card open on it (VoiceOS behaviour). (Picking one on the
        Agents PAGE opens it on the page; the card stays as it is.)"""
        self._focus(key)
        self._render_card(key)
        self._notch("open_full")
        self._refresh_rail()

    def _on_rail_recent_clicked(self, sid):
        live = self._session_by_claude_id(sid)
        if live is not None:
            self._show_in_card(live.key)
            return
        self._card_pick = sid                   # open it in the card once its transcript is loaded
        self._on_recent_clicked(sid)

    # ---- voice / typed requests from anywhere: route them (new / follow-up / "use my ... chat") ----
    def _build_voice_candidates(self, text: str):
        """The chats the router may RESUME: live ones first, then recent saved ones, then OLDER ones whose
        title/first message matches what you NAMED (Mac, as-is — from the lists already loaded off the UI
        thread: reading 500 chats per request froze the window on Mac)."""
        import re
        from app.core.agent_sessions import _TITLE_FILLERS as _fill
        cands, seen_ids, seen_titles = [], set(), set()

        def _add(kind, key, sid, cwd, title, agent="claude"):
            t = (title or "").strip()
            k = t.lower()[:60]
            if (sid and sid in seen_ids) or (k and k in seen_titles):
                return
            if sid:
                seen_ids.add(sid)
            if k:
                seen_titles.add(k)
            cands.append({"kind": kind, "key": key, "id": sid, "cwd": cwd, "title": t, "agent": agent or "claude"})

        for ss in sorted(self._sessions.values(), key=lambda x: x.started_at, reverse=True):
            _add("live", ss.key, ss.session_id or ss.resume, ss.cwd, ss.title, ss.agent)
        saved = sorted({r.id: r for r in list(self._recent) + list(self._all_sessions)
                        if not self._junk_session(r)}.values(), key=lambda r: r.mtime, reverse=True)
        for ss in saved[:12]:
            _add("saved", None, ss.id, ss.cwd, self._display_title(ss), getattr(ss, "agent", "claude"))
        asking = {"use", "conversation", "conversations", "chat", "chats", "about", "tell", "what", "back",
                  "continue", "codex", "claude", "the", "one", "where", "which", "had", "have", "did"}

        def stems(t):
            return {w[:4] for w in re.findall(r"[a-z0-9]+", (t or "").lower())
                    if len(w) > 2 and w not in _fill and w not in asking}
        words = stems(text)
        scored = []
        for ss in saved:
            in_title = len(words & stems(ss.title))
            in_first = len(words & (stems(ss.preview) - stems(ss.title)))
            if in_title + in_first >= 2:
                scored.append((3 * in_title + in_first, ss.mtime, ss))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        for _, _, ss in scored[:6]:
            _add("saved", None, ss.id, ss.cwd, self._display_title(ss), getattr(ss, "agent", "claude"))
        return cands

    def run_voice_task(self, text: str, source: str = "voice", images=None):
        """Shift+Z (voice) or the card's type field: route the request to a NEW chat, a FOLLOW_UP of the chat
        in focus, or a RESUME of one you named — one warm Claude call (~2-3 s) off the UI thread. The card
        shows "Routing..." meanwhile. Any routing failure falls back safely (continue the focused one, else new)."""
        text = (text or "").strip()
        if not text:
            return
        focused = self._voice_target()
        focused_title = focused.title if focused else None
        candidates = self._build_voice_candidates(text)
        images = list(images or [])
        _trace(f"{source} heard: {text!r} | focused={focused_title!r} | screenshots={len(images)} | "
               f"candidates={[c['title'] for c in candidates][:10]}")
        self._card_key = None                     # the card shows THIS request now ("Routing…"), not a chat
        self._notch("present", True)
        self._notch("set_title", "Routing…")
        self._notch("show_working", "", "")
        self._notch("flash")
        self._route_seq += 1
        rid = self._route_seq
        self._routing[rid] = text
        claude_ok, codex_ok = self._agent_ok("claude"), self._agent_ok("codex")

        def worker():
            titles = [c["title"] for c in candidates]
            try:
                try:                                   # Claude routes when it's available (fast, warm)…
                    if not claude_ok:
                        raise RuntimeError("Claude Code unavailable right now")
                    from app.core.intent_router import IntentRouter
                    d = IntentRouter.get().classify(text, focused_title, titles)
                except Exception as e:                 # …else Codex does (a cold `codex exec`, ~5 s)
                    if not codex_ok:
                        raise
                    _trace(f"router: Claude couldn't route ({e}) -> Codex")
                    from app.core.intent_router import classify_with_codex
                    d = classify_with_codex(text, focused_title, titles)
                intent, idx, task = d["intent"], d["index"], d["task"]
            except Exception as e:
                intent, idx, task = ("FOLLOW_UP" if focused_title else "NEW"), None, text
                _trace(f"router FAILED ({e}) -> fallback {intent}")
            try:
                self._voice_decided.emit({"rid": rid, "intent": intent, "idx": idx, "task": task,
                                          "candidates": candidates, "images": images,
                                          "focused_key": focused.key if focused else None})
            except RuntimeError:
                pass
        threading.Thread(target=worker, daemon=True, name="filect-agents-route").start()
        return rid

    def _voice_target(self):
        """The chat a voice / card-typed request may continue: the one you're looking at — the Agents page's
        chat while that page is in front, otherwise the card's (or, with nothing on the card, the focus)."""
        if self._page_in_front():
            return self._focused
        if self._card_key:
            return self._sessions.get(self._card_key)
        return self._focused

    def is_routing(self, rid) -> bool:
        return rid in self._routing

    def cancel_routing(self, rid=None) -> bool:
        """Esc (or Stop) while a request is still being routed: drop it before it becomes a chat. `rid` = that
        request (None = any). True if something was cancelled."""
        if not self._routing or (rid is not None and rid not in self._routing):
            return False
        heard = self._routing.pop(rid) if rid is not None else list(self._routing.values())[-1]
        if rid is None:
            self._routing.clear()
        _trace("STOP while routing — request cancelled")
        self._notch("set_task", heard, "")
        self._notch("show_done", "", 0.0, "", "Stopped by you.", True)
        self._notch("hide_primary")            # it never became a chat — nothing to open
        return True

    def _on_voice_decided(self, result):
        rid = result.get("rid")
        if rid not in self._routing:
            _trace(f"request {rid} dropped — you stopped it while it was routing")
            return
        del self._routing[rid]
        quiet = rid in self._quiet_routes           # you ✕'d the card while this was routing: it starts tucked
        self._quiet_routes.discard(rid)
        self._quiet_start = quiet
        try:
            self._dispatch_decided(result)
        finally:
            self._quiet_start = False
        if self._card_key is None and not quiet:      # (every path binds the card; never leave it on "Routing…")
            if self._focused is not None:
                self._render_card(self._focused_key)
            else:
                self._notch("rest_idle", True)

    def _dispatch_decided(self, result):
        intent, idx, task = result["intent"], result["idx"], result["task"]
        cands, fkey = result["candidates"], result["focused_key"]
        images = result.get("images") or []
        _trace(f"decided: {intent} idx={idx} task={task!r}")

        def _continue(sess, title):
            self._notch("set_title", f"↳ continuing “{title}”")
            if sess.running:
                sess.pending_followup, sess.pending_images = task, images
                self._last_sent_key = sess.key
                _trace(f"  -> queue follow-up on {sess.key} '{title}' (running)")
                self._focus(sess.key)
                if not self._quiet_start:
                    self._render_card(sess.key)
                    self._notch("show_working", "Queued — will continue when this step finishes")
            else:
                _trace(f"  -> continue {sess.key} '{title}'")
                self.run_task(task, resume=sess.session_id or sess.resume, cwd=sess.cwd, images=images,
                              agent=sess.agent)

        if intent == "FOLLOW_UP" and fkey and fkey in self._sessions:
            _continue(self._sessions[fkey], self._sessions[fkey].title)
            return
        if intent == "RESUME" and isinstance(idx, int) and 0 <= idx < len(cands):
            c = cands[idx]
            if c["kind"] == "live" and c["key"] in self._sessions:
                _continue(self._sessions[c["key"]], c["title"])
            else:
                self._resume_target = {"id": c["id"], "cwd": c["cwd"], "title": c["title"],
                                       "agent": c.get("agent", "claude")}
                _trace(f"  -> resume saved '{c['title']}' id={(c['id'] or '')[:8]} ({c.get('agent', 'claude')})")
                self._notch("set_title", f"↳ continuing “{c['title']}”")
                self.run_task(task, resume=c["id"], cwd=c["cwd"], images=images, agent=c.get("agent", "claude"))
                self._resume_target = None
            return
        _trace("  -> NEW conversation")
        self._resume_target = None
        self._focused_key = None
        self.run_task(task, images=images)

    # ---- what you did on the page ----
    def _on_send(self, text: str):
        """Enter / ↑. Typing in an open chat continues it; with no chat open it's a new one."""
        text = (text or "").strip()
        if not text:
            return
        self._from_page = True
        try:
            self._send_from_page(text)
        finally:
            self._from_page = False

    def _send_from_page(self, text: str):
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
                self.run_task(text, resume=sid, cwd=sess.cwd, agent=sess.agent)
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
        """Model / thinking picked on the page's pill — for the agent it shows (the open chat's, else new
        chats'); saved; that agent's next turn (of any chat) uses it."""
        self._set_models(self._view_agent(), model, effort)

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


    def _open_task(self, where: str = "app", key=None):
        """"Open in …" — open the ACTUAL conversation: "app" → the Claude app (claude://resume), "terminal"
        → Claude Code in a terminal, in the chat's folder, on the same chat. The pick becomes the default.
        `key`: the card's chat ("" = none); None = the page's chat."""
        sess = self._sessions.get(key) if key is not None else self._focused
        sid = (sess.session_id or sess.resume or "") if sess else ""
        agent = sess.agent if sess else self._view_agent()
        if where in ("app", "terminal") and self._open_in(agent) != where:
            _save_setting(**{f"assistant_{agent}_open_in": where})
            if self._overlay is not None:
                self._overlay.set_open_defaults({agent: where})
        if not sid:
            self._schedule_page()
            return
        _trace(f"open {sess.key} ({agent}) in {where}")
        if agent == "codex":                        # off the UI thread: it looks at what's running (~50 ms+)
            k, cwd = sess.key, sess.cwd

            def worker():
                how = ""
                try:
                    from app.core.codex_runner import open_in_codex
                    r = open_in_codex(sid, cwd, where)
                    how = "opened" if r.startswith("codex://") else "terminal"
                except Exception as e:
                    _trace(f"open failed: {e}")
                try:
                    self._codex_opened.emit(k, how)
                except RuntimeError:
                    pass
            threading.Thread(target=worker, daemon=True, name="filect-open-codex").start()
        else:
            try:
                from app.core.agent_runner import open_in_claude
                open_in_claude(sid, sess.cwd, where)
            except Exception as e:
                _trace(f"open failed: {e}")
        self._schedule_page()

    def _on_codex_opened(self, key, how: str):
        """Open in Codex finished (off the UI thread): "opened" (the Codex app shows the chat) / "terminal" / ""."""
        sess = self._sessions.get(key)
        if not how and sess is not None:
            self.view.set_hint("Couldn't open the Codex app — try Open in Terminal (the arrow next to it).")
            QTimer.singleShot(9000, lambda: self.view.set_hint("") if not self._recording else None)

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
            agent = agent or self._resume_target.get("agent")
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
        # A saved chat continues with ITS agent (Claude's store unless it says Codex); a new one: your pick.
        agent = (agent or "claude") if resume else self._pick_agent()
        sess = AgentSession(key, workdir, prompt, display, bypass, *self._model_for(agent), resume=resume, agent=agent)
        sess.images = list(images or [])
        self._sessions[key] = sess
        self._start_worker(sess, resume)

    def _restart_session(self, sess, prompt, bypass, images=None):
        """Continue a finished conversation with a new turn, REUSING its AgentSession."""
        sess.prompt, sess.bypass, sess.images = prompt, bypass, list(images or [])
        sess.status, sess.result_text, sess.cost, sess.approval_req = "working", "", 0.0, None
        sess.final_text, sess.error, sess.last_action = "", "", ""
        sess.started_at, sess.duration = time.time(), None
        sess.fallback_to, sess.fell_back, sess.first_error = None, False, ""   # each turn may hand over once
        sess.before_switch = None
        sess.codex_held = sess.awaiting_codex = False
        sess.busy_in_app = None
        _trace(f"restart {sess.key} '{sess.title}' (new turn, resume={sess.session_id or sess.resume})")
        self._start_worker(sess, sess.session_id or sess.resume or None)

    def _start_worker(self, sess, resume_id, seed: bool = True, send: str | None = None):
        """Build + start this session's worker thread, wire its signals (keyed by session), focus it.
        `seed=False`: the same turn re-run (after setup) — the page already shows it."""
        sess.model, sess.effort = self._model_for(sess.agent)     # the pill's choice applies to every next turn
        sess.reopen_token += 1                      # a pending Codex reopen waits for this turn too
        sess.awaiting_codex = False
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
        if fresh and self._quiet_start:
            # You ✕'d the card while this request was routing: it runs like any chat you closed the card on —
            # in the drawer (and the page's list), with the usual "task finished" ping — the card stays shut.
            sess.feed_lines = [f"▸ {sess.prompt}"]
            details_view.add_step(sess.entries, "user", text=sess.prompt, images=sess.images)
            sess.last_action = "Starting…"
        elif fresh:
            sess.feed_lines = [f"▸ {sess.prompt}"]
            details_view.add_step(sess.entries, "user", text=sess.prompt, images=sess.images)
            sess.last_action = "Starting…"
            auto = self._card_auto() and not self._from_page
            self._card_key = key                            # a turn just started: the card follows it
            self._set_card_agent(sess.agent)
            self._notch("present", auto)                    # resets the card's Details
            self._notch("set_task", sess.prompt, sess.title)  # bubble = what you asked, bold = its name
            self._notch("show_working", "Starting…", "")
            if auto:
                self._notch("flash")
            self._notch("set_details", sess.entries)
        self._refresh_page()
        if self.isVisible():
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
                if getattr(info, "agent", "claude") == "codex":
                    from app.core.codex_runner import load_codex_transcript
                    data = load_codex_transcript(sid)
                else:
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
        agent = getattr(info, "agent", "claude") or "claude"
        sess = AgentSession(f"s{next(_session_keys)}", info.cwd, prompt, self._display_title(info),
                            self.bypass_check.isChecked(), *self._model_for(agent), resume=sid, agent=agent)
        sess.status, sess.result_text = "done", result
        sess.feed_lines = lines or [f"▸ {prompt}"]
        sess.entries = entries or [{"id": "0", "kind": "user", "text": prompt}]
        sess.started_at, sess.duration = info.mtime, (data or {}).get("duration")
        sess.preview = info.preview
        self._sessions[sess.key] = sess
        _trace(f"picked up past chat {sess.key} '{sess.title}' id={sid[:8]} ({len(sess.entries)} entries)")
        if getattr(self, "_card_pick", None) == sid:
            self._card_pick = None
            self._show_in_card(sess.key)
            return
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
                try:                                # + Codex chats in the workspace (Codex's own index: they stay
                    from app.core.codex_runner import codex_threads     # after Codex prunes old files)
                    found += codex_threads(DEFAULT_WORKSPACE)
                except Exception as e:
                    _trace(f"codex chats failed: {e}")
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
                try:                                # + your Codex chats (the Codex app, terminal, Filect's)
                    from app.core.codex_runner import codex_threads
                    items += [s for s in codex_threads(DEFAULT_WORKSPACE, everywhere=True) if not self._junk_session(s)]
                    items.sort(key=lambda s: s.mtime, reverse=True)
                except Exception:
                    pass
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
        self._effort = self._coerce_effort("claude", self._model, self._effort)
        self._sync_view_agent(force=True)
        if self._card_agent in (None, "claude"):
            self._set_card_agent("claude" if self._card_agent == "claude" else self._pick_agent(), force=True)

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
            sess.status = "working"
            if not sess.stopping:                   # (keep "Stopping…" while a Stop winds the turn down)
                sess.last_action = label
            sess.final_text = ""
            _trace(f"{key} · {label}")
            if not sess.feed_lines or sess.feed_lines[-1] != label:
                sess.feed_lines.append(label)
            details_view.add_step(sess.entries, "tool", tool=step.tool, input=getattr(step, "input", None),
                                  tool_id=getattr(step, "tool_id", ""))
        elif step.kind == "text" and step.text:
            n = getattr(sess, "live_updates", 0)
            if n:                                   # (diagnostics: did the reply stream in, or arrive whole?)
                _trace(f"{key} reply streamed live: {n} update(s) over "
                       f"{time.time() - getattr(sess, 'live_t0', time.time()):.1f}s, {len(step.text)} chars")
                sess.live_updates = 0
            sess.result_text = (sess.result_text + " " + step.text).strip()
            sess.final_text = f"{sess.final_text}\n\n{step.text}".strip()
            sess.status = "working"
            if not sess.stopping:
                sess.last_action = "Writing the answer"
            if not sess.feed_lines or sess.feed_lines[-1] != step.text:
                sess.feed_lines.append(step.text)
            details_view.add_step(sess.entries, "text", text=step.text)
        elif step.kind == "result":
            details_view.add_step(sess.entries, "result", text=step.text, tool_id=step.tool_id,
                                  is_error=getattr(step, "is_error", False))
            if getattr(step, "is_error", False):
                _trace(f"{key} ✗ {step.text[:120]!r}")
        elif step.kind == "partial":
            # The reply as Claude writes it (≤5×/s): one live row that grows; the finished text replaces it.
            details_view.set_live_text(sess.entries, step.text)
            if not getattr(sess, "live_updates", 0):
                sess.live_t0 = time.time()
            sess.live_updates = getattr(sess, "live_updates", 0) + 1
            sess.status = "working"
            if not sess.stopping:
                sess.last_action = "Writing the answer"
            if self._card_shows(key):
                # Only the status line follows the words (cheap at any length). Details catches up on the
                # next step, or when you open it — re-laying-out a long reply 5×/s there would lag.
                tail = " ".join(step.text[-400:].split())
                self._notch("show_working", ("…" + tail[-150:]) if len(step.text) > 150 else tail, "")
            self._schedule_page()
            return
        if self._card_shows(key):
            self._notch("set_details", sess.entries)
            if step.kind in ("tool", "text"):
                self._notch("show_working", sess.last_action if step.kind == "tool" else step.text, "")
        self._schedule_page()

    def _on_approval(self, key, req):
        sess = self._sessions.get(key)
        if sess is None:
            return
        sess.approval_req, sess.status = req, "needs_you"
        _trace(f"{key} NEEDS APPROVAL: {req.tool} (focused={key == self._focused_key}, card={self._card_shows(key)})")
        if self._card_shows(key):
            self._notch("show_needs_you", req.tool, req.tool_input)
            if not self._page_in_front():
                self._notch("open_full")
        self._schedule_page()
        if key not in (self._focused_key, self._card_key) or not _app_is_active():
            self._notify_needs_you(sess)

    def _decide(self, approved: bool, key=None):
        """Approve/Deny — answers ONE chat's gate: the card's (`key`; "" = none → nothing) or, from the page
        (key None), the page's chat. Never another chat's."""
        sess = self._sessions.get(key) if key is not None else self._focused
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
        self._down.pop(sess.agent, None)            # it just worked — usable again
        if session_id:
            sess.session_id = session_id
            if sess.agent == "claude" and sess.cwd and os.path.normcase(os.path.normpath(sess.cwd)) != \
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
               f"focused={key == self._focused_key} card={self._card_shows(key)}")
        if self._card_shows(key):
            self._notch("show_done", session_id, cost, sess.prompt, sess.result_text)
            if not sess.stopping and self._card_auto():
                self._notch("open_full")
        self._schedule_page()
        if not sess.stopping:
            _play_chime(True)
            if key != self._focused_key or not _app_is_active():
                self._last_notified_key = key
                _notify("Filect · task finished", sess.title or "Done")

    def _open_agent_setup(self, agent: str, kind: str, key=None):
        """The error card's "Set up / Sign in to Claude Code →": a terminal with the official setup. The task
        on this chat (`key` = the card's; None = the page's) then waits for you and runs by itself once
        Claude Code is ready."""
        self._down.pop(agent, None)                 # you're fixing it — try it first again next time
        try:
            if agent == "codex":
                from app.core.codex_runner import open_codex_setup as open_setup
            else:
                from app.core.agent_runner import open_claude_setup as open_setup
            open_setup(kind)
        except Exception as e:
            _trace(f"{agent} setup failed to open: {e}")
            return
        key = (key or None) if key is not None else self._focused_key
        self._setup_wait = {"agent": agent, "key": key, "until": time.time() + 600}
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

        def worker():                               # off the UI thread: it runs the agent's CLI
            try:
                if agent == "codex":
                    from app.core.codex_runner import codex_ready as ready
                else:
                    from app.core.agent_runner import claude_ready as ready
                ok = ready()
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
        self._down.pop(agent, None)
        st = dict(self._agents_state or {})
        st[agent] = "ready"
        if self._agents_waiting:
            self._agents_waiting = None
            self._agents_poll.stop()
        self._publish_agents(st)
        _trace(f"{agent} is ready — running the waiting task {w['key']}")
        self._run_after_setup(w["key"], agent)

    def _run_after_setup(self, key, agent: str):
        """You finished the setup: run the task that was waiting on it (same chat, same request). Not if
        you've since re-sent or stopped it yourself."""
        sess = self._sessions.get(key) if key else None
        if sess is None or sess.running or sess.status != "error":
            return
        send = sess.prompt
        if agent != sess.agent:                     # you set up the OTHER agent (yours was out of usage)
            if sess.session_id or sess.resume:
                send = self._recap(sess, sess.agent) + sess.prompt
            sess.agent = agent
            sess.session_id, sess.resume = "", None
        sess.status, sess.result_text, sess.approval_req, sess.final_text, sess.error = "working", "", None, "", ""
        sess.fallback_to, sess.fell_back, sess.first_error, sess.before_switch = None, False, "", None
        sess.started_at, sess.duration = time.time(), None
        sess.last_action = f"{_agent_name(agent)} is ready — running your task"
        self._start_worker(sess, sess.session_id or sess.resume or None, seed=False, send=send)
        if self._card_shows(key):
            self._set_card_agent(agent)
            self._notch("show_working", sess.last_action, "")
            if self._card_auto():
                self._notch("open_full")

    def _on_error(self, key, msg: str):
        sess = self._sessions.get(key)
        if sess is None:
            return
        if sess.stopping:                           # a Stop that cancelled the turn — not a failure
            return
        from app.core.agent_runner import setup_info, setup_kind
        try:
            from app.core.codex_runner import CHAT_BUSY
        except Exception:
            CHAT_BUSY = None
        if CHAT_BUSY and msg.strip() == CHAT_BUSY and not sess.fell_back:
            sess.codex_held, sess.first_error = True, msg       # the Codex app holds it: _on_done decides
            _trace(f"{key} chat is open in the Codex app")
            return
        info = setup_info(msg)
        if info and info[1] in ("install", "signin"):
            self.refresh_agents_setup()              # (signed out since the last check? the saved answer catches up)
        if info and info[0] == sess.agent:          # this agent can't run: not set up / out of usage
            self._down[sess.agent] = (info[1], time.time() + (1800 if info[1] == "usage" else 300))
            other = _other_agent(sess.agent)
            if not sess.fell_back and self._agent_ok(other):
                sess.fallback_to, sess.first_error = other, msg      # _on_done hands this turn over
                _trace(f"{key} {sess.agent} unavailable ({info[1]}) -> handing the turn to {other}")
                return
            if sess.fell_back and sess.first_error:  # BOTH can't run: show the one you can fix
                fixable = [m for m in (sess.first_error, msg) if setup_kind(m) in ("install", "signin")]
                msg = fixable[0] if fixable else f"{sess.first_error}\n{msg}"   # both out: both reset times
                info = setup_info(msg)
                if sess.before_switch:               # no hand-over after all: the chat keeps its own agent
                    sess.agent, sess.session_id, sess.resume, sess.model, sess.effort = sess.before_switch
                    if self._card_shows(key):
                        self._set_card_agent(sess.agent)
        sess.status, sess.error = "error", msg
        sess.last_action = ""
        _trace(f"{key} ERROR '{sess.title}': {msg}")
        if self._card_shows(key):
            self._notch("show_error", msg)
            if self._card_auto() or (self._setup_kind(msg) and not self._page_in_front()):
                self._notch("open_full")
        self._schedule_page()
        _play_chime(False)
        if key != self._focused_key or not _app_is_active():
            self._last_notified_key = key
            name = _agent_name(info[0]) if info else ""
            _notify("Filect · task failed" if not info else f"Filect · {name} is out of usage"
                    if info[1] == "usage" else f"Filect · set up {name}", msg)

    def _on_card_stop(self):
        """■ Stop on the card: a request still being routed is cancelled; otherwise the CARD's chat stops."""
        if self.cancel_routing():                  # still deciding where it goes: cancel the request
            return
        sess = self._sessions.get(self._card_key) if self._card_key else None
        if sess is None or not sess.running:
            _trace(f"card Stop, but {sess.key if sess else 'no chat'} isn't running — resyncing the card")
            if sess is not None:
                self._render_card(sess.key)
            else:
                self._notch("rest_idle", True)
            return
        self._stop_session(sess)

    def _on_stop_task(self):
        """■ on the Agents page: stops the PAGE's chat (a request routing on the card is the card's to stop)."""
        sess = self._focused
        if sess is None or not sess.running:
            return
        self._stop_session(sess)

    def _stop_session(self, sess):
        """Stop on ONE click: interrupt this chat's turn now, and force-cancel after 2.5 s if the interrupt
        didn't end it (Claude Code on Windows can take ~1-3 s to honour it). A second click changes nothing."""
        if sess.stopping:
            return
        _trace(f"{sess.key} STOP requested '{sess.title}'")
        if self._card_shows(sess.key):
            self._notch("mark_stopping")
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
        details_view.end_live(sess.entries)         # (stopped mid-reply: what it wrote stays, as a normal row)
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
            if self._card_shows(key):
                self._notch("show_done", "", 0.0, "", "Stopped by you.", True)
            self._schedule_page()
            return
        if sess.codex_held:                         # the Codex app holds this chat: close it, or a new chat
            sess.codex_held = False
            self._ask_or_close_codex(sess)
            return
        if sess.reopen_codex and not sess.pending_followup:   # we closed the Codex app for this: bring it back
            self._reopen_codex_when_quiet(sess)               # …once you've stopped talking to this chat
        if sess.fallback_to:                        # this agent couldn't run → the other takes the turn
            agent, sess.fallback_to = sess.fallback_to, None
            self._switch_agent(sess, agent)
            return
        if sess.status == "working":                # ended without a result or an error
            sess.status = "done"
        if sess.pending_followup:                   # sent while this was running — continue the SAME chat
            text, sess.pending_followup = sess.pending_followup, None
            imgs, sess.pending_images = sess.pending_images, []
            sid = sess.session_id or sess.resume
            if sid:
                self.run_task(text, resume=sid, cwd=sess.cwd, images=imgs, agent=sess.agent)
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

    # ---- the other agent takes over (§8l) ----
    def _switch_agent(self, sess, agent: str):
        """Hand this turn to the other agent (the first isn't set up / is out of usage). A new chat just runs
        there; a continued one can't take its memory across, so the other agent starts a new chat with a recap
        of this one. From then on the chat stays with `agent`."""
        from app.core.agent_runner import setup_kind
        try:
            from app.core.codex_runner import CHAT_BUSY
        except Exception:
            CHAT_BUSY = None
        old = sess.agent
        why = "is out of usage" if setup_kind(sess.first_error) == "usage" else "isn't set up"
        send = sess.prompt
        if sess.session_id or sess.resume:
            send = self._recap(sess, old) + sess.prompt
        sess.before_switch = (old, sess.session_id, sess.resume, sess.model, sess.effort)
        sess.agent, sess.fell_back = agent, True
        sess.model, sess.effort = self._model_for(agent)
        sess.session_id, sess.resume = "", None
        sess.status, sess.result_text, sess.final_text, sess.error = "working", "", "", ""
        cue = f"{_agent_name(old)} {why} — switching to {_agent_name(agent)}"
        if CHAT_BUSY and sess.first_error == CHAT_BUSY:
            cue = "This chat is open in the Codex app — continuing it here as a new chat"
        _trace(f"{sess.key} -> {agent}: {cue} (recap={send != sess.prompt})")
        sess.last_action = cue
        self._start_worker(sess, None, seed=False, send=send)
        if self._card_shows(sess.key):
            self._set_card_agent(agent)
            self._notch("show_working", cue, "")
        self._refresh_rail()

    def _recap(self, sess, old_agent: str, turns: int = 3) -> str:
        """The chat's last few exchanges, for the agent picking it up: from this run, else from the chat's saved
        history (a chat you named by voice — "use my email conversation" — has none loaded here)."""
        old_name = _agent_name(old_agent)

        def pairs_of(entries):
            pairs, cur = [], None
            for e in entries:
                if e.get("kind") == "user":
                    cur = [e.get("text", ""), ""]
                    pairs.append(cur)
                elif e.get("kind") == "text" and cur is not None:
                    cur[1] = e.get("text", "")
            return pairs
        pairs = pairs_of(sess.entries)[:-1]          # the last one is this turn's request
        sid = sess.session_id or sess.resume
        if not pairs and sid:
            try:
                if old_agent == "codex":
                    from app.core.codex_runner import load_codex_transcript
                    data = load_codex_transcript(sid)
                else:
                    from app.core.agent_sessions import load_transcript
                    data = load_transcript(sid, sess.cwd)
                pairs = pairs_of(self._transcript_view(data)[3]) if data else []
            except Exception as e:
                _trace(f"recap: couldn't read {sid[:8]}'s history ({e})")
        pairs = pairs[-turns:]
        body = "\n\n".join(f"Me: {u[:600]}\n{old_name}: {(r or '(no reply)')[:900]}" for u, r in pairs) \
            or f"(a chat called “{sess.title}”)"
        return (f"[You're picking up a conversation that started in {old_name}, which can't continue it "
                f"right now. Earlier in it:\n\n{body}\n\nCarry on from there.]\n\n")

    # ---- a chat the Codex app (or Codex in a terminal) is holding ----
    def _ask_or_close_codex(self, sess):
        """Codex lets one program write a chat at a time, and the Codex app keeps any chat it opened until it
        quits. First find out who holds it (off the UI thread — on Windows that may ask WMI): "Always close
        Codex" (and nothing running there) → close it and continue the SAME chat; else ask."""
        sid = sess.session_id or sess.resume or ""
        key = sess.key
        sess.codex_checking = True
        sess.last_action = "This chat is open in Codex — checking…"
        self._schedule_page()

        def worker():
            held_by, busy = "", []
            try:
                from app.core import codex_runner as cr
                held_by = cr.chat_holder(sid)
                busy = cr.codex_app_busy() if held_by != "terminal" else []
            except Exception as e:
                _trace(f"codex holder check failed: {e}")
            try:
                self._codex_hold_checked.emit(key, held_by, busy)
            except RuntimeError:
                pass
        threading.Thread(target=worker, daemon=True, name="filect-codex-holder").start()

    def _on_codex_hold_checked(self, key, held_by: str, busy):
        sess = self._sessions.get(key)
        if sess is None:
            return
        sess.codex_checking = False
        if sess.running:                            # (you sent something else meanwhile)
            return
        busy = list(busy or [])
        if _setting("assistant_codex_close_app", False) and not busy:
            _trace(f"{key} always-close Codex -> closing it")
            self._close_codex_and_continue(key, "")
            return
        sess.status, sess.awaiting_codex = "needs_you", True
        sess.busy_in_app = busy[0] if busy else None
        sess.codex_msg = ("This chat is open in Codex in a terminal." if held_by == "terminal"
                          else "This chat is open in the Codex app.")
        sess.last_action = "This chat is open in Codex"
        _trace(f"{key} asking: close Codex or new chat (held by {held_by or '?'}, Codex busy: {busy})")
        if self._card_shows(key):
            self._set_card_agent("codex")
            self._notch("show_codex_choice", sess.codex_msg, busy[0][1] if busy else "")
            if not self._page_in_front():
                self._notch("open_full")
        self._refresh_rail()
        self._schedule_page()
        _play_chime(False)
        if key not in (self._focused_key, self._card_key) or not _app_is_active():
            self._last_notified_key = key
            _notify("Filect · needs you", f"{sess.title}: this chat is open in the Codex app")

    def _on_card_codex_choice(self, which: str, always: bool):
        self._on_codex_choice(which, always, self._card_key or "")

    def _on_page_codex_choice(self, which: str, always: bool):
        self._on_codex_choice(which, always, None)

    def _on_codex_choice(self, which: str, always: bool, key=None):
        """"Close Codex and continue here" / "Continue as a new chat" — for ONE chat: the card's (`key`) or,
        from the page (None), the page's."""
        sess = self._sessions.get(key) if key is not None else self._focused
        if sess is None or not sess.awaiting_codex:
            return
        sess.awaiting_codex = False
        _trace(f"{sess.key} Codex choice: {which} (always={always})")
        if which == "new":
            self._switch_agent(sess, "codex")       # a new chat with a recap
            return
        if always:
            _save_setting(assistant_codex_close_app=True)
        self._close_codex_and_continue(sess.key, sess.busy_in_app[0] if sess.busy_in_app else "")

    def _close_codex_and_continue(self, key, interrupted: str):
        """Close what holds the chat (off the UI thread — it takes a few seconds), then continue it."""
        sess = self._sessions.get(key)
        if sess is None:
            return
        sid = sess.session_id or sess.resume
        sess.status, sess.last_action = "working", "Closing Codex…"
        sess.awaiting_codex, sess.codex_checking = False, True
        if self._card_shows(key):
            self._notch("show_working", "Closing Codex…", "")
        self._refresh_rail()
        self._schedule_page()

        def worker():
            what = ""
            try:
                from app.core.codex_runner import release_chat
                what = release_chat(sid, force=bool(interrupted))   # "Close Codex anyway" (it was working)
            except Exception as e:
                _trace(f"closing Codex failed: {e}")
            try:
                self._codex_released.emit(key, what, interrupted or "")
            except RuntimeError:
                pass
        threading.Thread(target=worker, daemon=True, name="filect-close-codex").start()

    def _on_codex_released(self, key, what: str, interrupted: str):
        sess = self._sessions.get(key)
        if sess is None:
            return
        sess.codex_checking = False
        if not what:                                # it didn't close (it may be asking you something)
            sess.status, sess.awaiting_codex = "needs_you", True
            sess.codex_msg = "Codex didn't close — it may be asking you something. Close it and try again, " \
                             "or continue here as a new chat."
            _trace(f"{key} Codex didn't close")
            if self._card_shows(key):
                self._notch("show_codex_choice", sess.codex_msg, "")
            self._refresh_rail()
            self._schedule_page()
            return
        sid = sess.session_id or sess.resume
        # Bring the Codex app back where you were (or on the task it stopped) — not if it was Codex in a terminal
        # you closed (you weren't using the app).
        sess.reopen_codex = (interrupted or sid) if what == "app" else None
        cue = "Closed Codex — continuing this chat"
        _trace(f"{key} {what} closed -> continuing {sid[:8]} (reopen on {(sess.reopen_codex or '-')[:8]})")
        sess.last_action = cue
        self._start_worker(sess, sid, seed=False, send=sess.prompt)
        if self._card_shows(key):
            self._set_card_agent("codex")
            self._notch("show_working", cue, "")

    def _reopen_codex_when_quiet(self, sess):
        """Reopen the Codex app on the chat after REOPEN_QUIET_MS without a new turn on it."""
        sess.reopen_token += 1
        tok = sess.reopen_token
        _trace(f"{sess.key} will reopen the Codex app on {sess.reopen_codex[:8]} in {REOPEN_QUIET_MS // 1000}s "
               "unless you follow up")
        QTimer.singleShot(REOPEN_QUIET_MS, lambda k=sess.key, t=tok: self._reopen_codex_now(k, t))

    def _reopen_codex_now(self, key, tok):
        sess = self._sessions.get(key)
        if self._closing or sess is None or not sess.reopen_codex or sess.reopen_token != tok or sess.running:
            return                                  # you followed up meanwhile — it reopens after that one
        chat, sess.reopen_codex = sess.reopen_codex, None
        _trace(f"{key} reopening the Codex app on {chat[:8]}")

        def worker():
            try:
                from app.core.codex_runner import open_codex_chat
                open_codex_chat(chat)
            except Exception as e:
                _trace(f"reopening Codex failed: {e}")
        threading.Thread(target=worker, daemon=True, name="filect-reopen-codex").start()
