"""Find a file by voice, in the hands-free assistant: the flow around core/file_finder.py, on the panel Organize uses.

Always searches first: whether a question is needed depends on what comes back, and a question never holds back the
answer. Nothing opens by itself unless they said "open". The only step that reads files is the deep look, which asks
first unless they named the folder, and reads at most file_finder.DEEP_CAP files.

Ported from the Mac (2026-11-10), with the newer design (screens 09-11): when nothing matches the time they said, it
says so and asks before showing the closest ones. Windows: typed answers are read the way spoken ones are
(intent_router.find_quick first, else the decider); the panel's handle says "+ Find" while it's tucked away.
"""
import logging
import os
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, QTimer, Signal

from app.core import file_finder as finder
from app.ui.voice_organize_controller import _ReadWorker

logger = logging.getLogger(__name__)


class _Job(QThread):
    done = Signal(object, int)

    def __init__(self, fn, rid):
        super().__init__()
        self._fn, self._rid = fn, rid

    def run(self):
        try:
            result = self._fn()
        except Exception as e:
            logger.warning(f"[find] failed: {e}", exc_info=True)
            result = {"error": str(e)}
        self.done.emit(result, self._rid)


class _Thumbs(QThread):
    ready = Signal(int, str, int)

    def __init__(self, paths, out_dir, rid):
        super().__init__()
        self._paths, self._dir, self._rid = paths, out_dir, rid

    def run(self):
        def one(i, p):
            made = finder.thumbnail(p, self._dir)
            if made:
                self.ready.emit(i, made, self._rid)

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="filect-find-thumb") as pool:
            for i, p in enumerate(self._paths):
                pool.submit(one, i, p)


class VoiceFindController(QObject):
    TUCK_SECS = 30               # ignored: it tucks away into the handle…
    EXPIRE_SECS = 300            # …and after this, "open it" no longer means these files
    GONE_SECS = 8                # how long "I couldn't find it" stays up
    TYPE_HINT = "Or type your answer and press Enter"
    THUMBS = 12                  # rows that get a small picture
    TELL_DECIDER = 30            # rows the decider hears about ("open the twelfth one")
    BRAND = "+ Find"
    ORGANIZE_BRAND = "+ Organize"
    _WAITING = ("results", "ask_what", "ask_folder", "ask_narrow", "ask_deep", "ask_closest")

    def __init__(self, main_window, organize):
        super().__init__()
        self.mw, self.org, self.overlay = main_window, organize, organize.overlay
        self._f = None               # the search in progress. Stages: searching, results, ask_what, ask_folder,
        self._rid = 0                # ask_narrow, ask_deep, ask_closest, reading, gone
        self._threads = []           # QThreads kept alive until they finish
        self._reader = None
        self._thumb_dir = ""
        self._shown = 0              # which list of results is on screen (its pictures belong to it alone)
        self.said_at = 0.0           # when it last said something (a newer approval's "yes" isn't for this)
        self._tuck = QTimer(self, singleShot=True, timeout=self._tuck_away)
        self._expire = QTimer(self, singleShot=True, timeout=self.close)

        ov = self.overlay
        ov.result_opened.connect(self._row_clicked)
        ov.result_revealed.connect(self._folder_clicked)
        ov.result_chip.connect(self._chip)
        ov.results_all.connect(self.show_all)
        ov.confirm_clicked.connect(self._confirm_clicked)
        ov.text_submitted.connect(self._typed)
        ov.dismissed.connect(self._on_dismissed)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._on_quit)

    # ---- what the voice assistant asks ------------------------------------------------------------------------
    def start(self, heard, details) -> str:
        """It heard "find my invoice". Returns "find", or "organize_busy" if Organize is using the panel."""
        if self.org.panel_busy():
            return "organize_busy"
        self._stop_jobs()
        d = finder.clean(details)
        if not (d["words"] or d["kind"] or d["after"] or d["before"] or d["date_words"] or d["folder"]):
            # (it said FIND and sent nothing to look for: the plain words, keeping what it called the file)
            d = dict(finder.clean(finder.details_from_text(heard or "")), **({"what": d["what"]} if d["what"] else {}))
        d = finder.fix_place(finder.fix_day(d, heard or ""), heard or "")
        self._f = {"stage": "searching", "heard": (heard or "").strip(), "d": d, "asked": False, "rows": [], "all": [],
                   "seen": set(), "widened": False, "read": False, "targets": [], "question": ""}
        logger.info(f"[find] heard {heard!r}: {d}")
        self.overlay.take_over(self.BRAND)
        self.overlay.set_hands_free(True)
        if not self.overlay.isVisible() and not self.overlay._minimized:
            from app.core.settings import settings
            self.overlay.present(tucked=not getattr(settings, "assistant_auto_open", True))
        if not (d["words"] or d["kind"] or d["after"] or d["before"] or d["date_words"] or d["folder"]):
            self._ask("ask_what", "What should I look for?")
            return "find"
        self._search(f"Looking for {finder.noun(d)[:40]}…")
        return "find"

    def more(self, text="", details=None) -> bool:
        """More detail on this search: an answer to its question, "the Acme one"."""
        f = self._f
        if f is None or f["stage"] not in self._WAITING:
            return False
        new = finder.clean(details)
        if not (new["words"] or new["kind"] or new["after"] or new["before"] or new["date_words"] or new["folder"]):
            word = finder._word(text)
            if not word:
                return False
            if f["stage"] == "ask_folder" and not finder.month_range(word)[0]:
                new["folder"] = text.strip()     # ("Which folder should I look in?" — "my Downloads")
            else:                            # (the plain words of the answer: "October" is a time, "Acme" a word)
                new = finder.clean(finder.details_from_text(text))
                if not (new["words"] or new["after"] or new["kind"] or new["folder"]):
                    new["words"] = [[word]]
        new = finder.fix_place(finder.fix_day(new, text or ""), text or "")
        f["d"] = finder.merge(f["d"], new)
        if new["when"]:
            f["d"]["when"] = new["when"]
        f["asked"] = True
        f["seen"] = set()
        if text:                             # (what Claude checks against: without this it never heard "October the 2nd")
            f["heard"] = f"{f['heard']} {text}"[-300:]
        logger.info(f"[find] more detail {text!r}: {f['d']}")
        self._search("Looking again…")
        return True

    def not_those(self) -> bool:
        """"Not those": if it went by a date, that may be what's wrong; otherwise the next ones."""
        f = self._f
        if f is None or f["stage"] != "results":
            return False
        d = f["d"]
        if (d["after"] or d["before"] or d["date_words"]) and not f["widened"]:
            f["widened"] = True
            f["d"] = dict(d, after="", before="", date_words=[], when="")
            self._search("Looking further…")
            return True
        rest = [r for r in f["all"] if r["file_path"] not in f["seen"]][:finder.SHOW_ROWS]
        if rest:
            self._show(rest, "Here are some more. Which one do you want?" if len(rest) > 1
                       else f"Here's another, in {rest[0]['folder']}. Want me to open it?")
            return True
        self._nothing()
        return True

    def open(self, which="") -> str:
        """Open one of the files shown. Returns what happened, for "What it heard"."""
        f = self._f
        if f is None or f["stage"] != "results":
            return "ignored"
        row = self._pick(which)
        if row is None:
            self._say("Which one? Say the number.")
            return "find_which"
        path = row["file_path"]
        self._note(f"Opened {row['file_name']}")
        self.close()
        logger.info(f"[find] opening {path}")
        self.mw.open_file_in_os(path)
        return "find_opened"

    def reveal(self, which="") -> str:
        f = self._f
        if f is None or f["stage"] != "results":
            return "ignored"
        row = self._pick(which)
        if row is None:
            self._say("Which one? Say the number.")
            return "find_which"
        path = row["file_path"]
        self._note(f"Showed {row['file_name']}")
        self.close()
        logger.info(f"[find] showing where {path} is")
        finder.reveal(path)
        return "find_shown"

    def show_all(self) -> bool:
        """The whole list, in the same panel. It stays until you're done with it: you're reading."""
        f = self._f
        if f is None or f["stage"] != "results" or not f["all"] or f.get("expanded"):
            return False
        self._show(f["all"], "Here's everything I found. Which one do you want?", scroll=True, expanded=True)
        self._tuck.stop()
        return True

    def deep_go(self) -> bool:
        """Yes to what it asked: read the files ("Look through them"), or show the closest ones."""
        f = self._f
        if f is None:
            return False
        if f["stage"] == "ask_closest":
            rows = f.pop("closest", None) or []
            if not rows:
                return False
            msg = (f"Here's the closest, in {rows[0]['folder']}. Want me to open it?" if len(rows) == 1
                   else "Here are the closest. Which one do you want?")
            self._show(rows, msg)
            return True
        if f["stage"] != "ask_deep":
            return False
        self._read()
        return True

    def close(self) -> bool:
        if self._f is None:
            return False
        self.overlay._on_dismiss()           # (→ dismissed → _on_dismissed)
        return True

    def stop(self):
        """Organize is taking the panel."""
        if self._f is not None:
            self._stop_jobs()
            self._f = None
            self.overlay.take_over(self.ORGANIZE_BRAND)

    def waiting_for_answer(self) -> bool:
        return self._f is not None and self._f["stage"] in self._WAITING

    def decider_state(self):
        """What it's doing right now, for the decider (and intent_router.find_quick). None if it's not in play."""
        f = self._f
        if f is None or f["stage"] == "gone":
            return None
        head = "FILECT IS FINDING A FILE (separate from the conversations above): "
        stage = f["stage"]
        base = {"head": head, "stage": stage, "rows": len(f["rows"]) if stage == "results" else 0,
                "names": [r["file_name"] for r in f["rows"][:self.TELL_DECIDER]] if stage == "results" else [],
                "question": f.get("question") or ""}
        if stage in ("searching", "reading"):
            return dict(base, text=f"It is looking for {f['heard']!r} right now.", allowed=["FIND_CLOSE"])
        if stage == "results":
            listing = "; ".join(f"{i}. {r['file_name']} in {r['folder']}" + (f" ({r['about']})" if r["about"] else "")
                                for i, r in enumerate(f["rows"][:self.TELL_DECIDER], 1))
            q = (f" It asked them: {f['question']!r}. If they just answer that with a name or a month ('Printify', 'the "
                 "Acme one', 'November'), that is FIND_MORE with that detail, even when a file on screen matches it: they "
                 "may have several. Only an explicit 'open' or a number is FIND_OPEN."
                 if f.get("question") else "")
            return dict(base, text=f"For {f['heard']!r} it is showing these numbered files on screen: {listing}.{q}",
                        allowed=["FIND_OPEN", "FIND_SHOW", "FIND_ALL", "FIND_NOT", "FIND_MORE", "FIND_CLOSE"])
        if stage in ("ask_deep", "ask_closest"):
            return dict(base, text=f"It couldn't find {f['heard']!r} and asked: {f['question']!r}",
                        allowed=["FIND_GO", "FIND_MORE", "FIND_CLOSE"])
        return dict(base, text=f"While looking for {f['heard']!r} it asked: {f['question']!r}",
                    allowed=["FIND_MORE", "FIND_CLOSE"])

    # ---- the flow ---------------------------------------------------------------------------------------------
    def _hold(self, thread):
        """A QThread destroyed while running aborts the app: hold it until it's finished, then let it go (on the UI
        thread: finished is delivered queued to this object) — otherwise every search left its threads behind."""
        if isinstance(self.mw, QObject):
            thread.setParent(self.mw)
        thread.finished.connect(self._on_thread_finished, Qt.QueuedConnection)
        self._threads.append(thread)

    def _on_thread_finished(self):
        t = self.sender()
        if t is None:
            return
        try:
            self._threads.remove(t)
        except ValueError:
            pass
        if t is self._reader:                # (still referenced: it's let go when the read is over)
            return
        try:
            if not t.isRunning():
                t.deleteLater()
        except RuntimeError:
            pass

    def _agents(self):
        return self.org._agent_order()

    def _say(self, line):
        self.said_at = time.time()
        self.org._say(line)

    def _run(self, fn, on_done):
        self._rid += 1
        job = _Job(fn, self._rid)
        self._hold(job)
        job.done.connect(on_done, Qt.QueuedConnection)
        job.start()

    def _search(self, message):
        f = self._f
        f["stage"] = "searching"
        f["question"] = ""
        self._tuck.stop()
        self._expire.stop()
        self.overlay.show_thinking(message)
        d, heard, agents, can_ask = f["d"], f["heard"], self._agents(), not f["asked"]
        self._run(lambda: finder.find(d, heard, agents, can_ask), self._on_found)

    def _on_found(self, r, rid):
        f = self._f
        if rid != self._rid or f is None:
            return
        if r.get("error"):
            self._gone("Something went wrong looking for that.")
            return
        f["all"] = r["all"]
        rows = r["rows"]
        if f["seen"]:                        # ("not those": what you've already turned down doesn't come back)
            rows = [x for x in r["all"] if x["file_path"] not in f["seen"]][:finder.SHOW_ROWS]
        if not rows:
            self._nothing()
            return
        d = f["d"]
        # "Open the Acme invoice" and there's just one: open it. Not after "not those" (that's one they've never seen),
        # nor when it isn't the type they said ("open my resume PDF" mustn't open resume.docx unseen).
        if d["open"] and len(rows) == 1 and not r["many"] and not r["dropped"] and not f["seen"]:
            f["stage"], f["rows"] = "results", rows
            self.open("1")
            return
        if r["dropped"] == "date":
            # Nothing from then: say so and ask, never show another time's files as if they were it (people misremember
            # months — and "due by November 3" made an October invoice look like November's).
            question = f"I couldn't find one {d['when'] or 'from then'}. Want me to show the closest?"
            self._note(question.split(" Want")[0])
            f.update(stage="ask_closest", question=question, closest=rows)
            self._tuck.stop()
            self.overlay.ensure_open()
            self.overlay.show_confirm(question, "Show the closest")
            self._say(question)
            self._expire.start(self.EXPIRE_SECS * 1000)
            return
        self._note(f"Found {rows[0]['file_name']}" if len(rows) == 1 else "Showed what it found")
        lead = "Nothing of that type. " if r["dropped"] == "type" else ""
        question = r["ask"] if r["many"] else ""
        if r["many"]:
            msg = f"You've got a lot of {finder.noun(d)}. Here are the newest." + (f" {question}" if question else "")
            f["asked"] = f["asked"] or bool(question)
        elif r.get("exact", 0) > len(rows):  # (a filter with nothing to judge: this number is real)
            when = f" {d['when']}" if d["when"] else ""
            msg = f"You've got {r['exact']} {finder.noun(d)}{when}. Here are the newest. Which one do you want?"
        elif len(rows) == 1:
            msg = f"{lead}Found it, in {rows[0]['folder']}. Want me to open it?"
        elif r.get("fit", 0) > len(rows) or (not r.get("fit") and len(r["all"]) > len(rows)):
            msg = f"{lead}Found several. Here are the top {len(rows)}. Which one do you want?"   # (never "found 3" when more fit)
        else:
            msg = f"{lead}Found {len(rows)} that fit. Which one do you want?"
        self._show(rows, msg, r["choices"] if question else (), question)

    @staticmethod
    def _sub(r):
        t = finder.local_time(r["mtime"])
        day = f"{time.strftime('%b', t)} {t.tm_mday}" + ("" if t.tm_year == time.localtime().tm_year else f", {t.tm_year}")
        return f"{r['folder']}, {day}" + (f". {r['about']}" if r["about"] else "")

    def _show(self, rows, message, chips=(), question="", scroll=False, expanded=False):
        f = self._f
        f.update(stage="results", rows=rows, question=question, expanded=expanded)
        self._shown += 1                     # (a picture still coming for the last list must not land on this one)
        f["seen"] |= {r["file_path"] for r in rows}
        self.overlay.ensure_open()
        self.overlay.show_results(message, [{"name": r["file_name"], "sub": self._sub(r),
                                             "ext": os.path.splitext(r["file_name"])[1].lstrip(".").upper()[:4]}
                                            for r in rows],
                                  chips, show_all=len(f["all"]) > len(rows), scroll=scroll,
                                  field=self.TYPE_HINT if question else "")
        self._say(message)
        self._thumbs([r["file_path"] for r in rows[:self.THUMBS]])
        if question:                         # (it's asking: like every question, it stays open — you may be typing)
            self._tuck.stop()
        else:
            self._tuck.start(self.TUCK_SECS * 1000)
        self._expire.start(self.EXPIRE_SECS * 1000)

    def _thumbs(self, paths):
        if not paths:
            return
        if not self._thumb_dir:
            self._thumb_dir = tempfile.mkdtemp(prefix="filect-find-")
        worker = _Thumbs(paths, self._thumb_dir, self._shown)
        self._hold(worker)
        worker.ready.connect(self._on_thumb, Qt.QueuedConnection)
        worker.start()

    def _on_thumb(self, i, path, shown):
        if shown == self._shown and self._f is not None and self._f["stage"] == "results":
            self.overlay.set_result_thumb(i, path)

    def _tuck_away(self):
        if self._f is not None and self._f["stage"] == "results":
            self.overlay.minimize()

    def _pick(self, which):
        rows = self._f["rows"]
        w = str(which or "").strip()
        if w.isdigit():
            i = int(w) - 1
            return rows[i] if 0 <= i < len(rows) else None
        return rows[0] if len(rows) == 1 else None

    def _ask(self, stage, question):
        f = self._f
        f.update(stage=stage, question=question)
        self._tuck.stop()
        self.overlay.ensure_open()
        self.overlay.show_choice(question, [], field=self.TYPE_HINT)
        self._say(question)
        self._expire.start(self.EXPIRE_SECS * 1000)

    def _note(self, status):
        """The History tab: one row per search, however many follow-ups it took."""
        f = self._f
        if f is None:
            return
        try:
            from app.core import assistant_history
            if f.get("row"):
                assistant_history.update(f["row"], said=f["heard"], status=status)
            else:
                f["row"] = assistant_history.add("find", f["heard"], f"Looked for {finder.noun(f['d'])}", status)
        except Exception as e:
            logger.warning(f"[find] couldn't write History: {e}")

    def _gone(self, msg):
        if not msg.startswith("Something went wrong"):
            self._note(msg)
        self._f["stage"] = "gone"
        self._tuck.stop()
        self.overlay.ensure_open()
        self.overlay.show_error(msg, question=True, buttons=False)
        self._say(msg)
        self._expire.start(self.GONE_SECS * 1000)

    def _nothing(self):
        """Nothing fits by name or text. Only now could reading files help."""
        f = self._f
        d = f["d"]
        if f["read"]:
            self._gone("I looked through them and couldn't find it.")
        elif not (d["folder"] or d["kind"] or d["after"] or d["before"] or d["date_words"]):
            if f.get("asked_folder"):
                self._gone("I couldn't find that.")
            else:
                f["asked_folder"] = True
                self._ask("ask_folder", "I couldn't find it by name or text. Which folder should I look in?")
        else:
            f["stage"] = "searching"
            self.overlay.show_thinking("Checking what's there…")
            self._run(lambda: finder.deep_targets(d), self._on_targets)

    def _on_targets(self, r, rid):
        f = self._f
        if rid != self._rid or f is None:
            return
        d = f["d"]
        if r.get("error"):
            self._gone("Something went wrong looking for that.")
        elif r.get("missing"):
            self._gone(f"I couldn't find a folder called {d['folder']}.")
        elif r.get("too_many"):
            if f["asked"]:
                self._gone("That folder has too many files for me to read through.")
            else:
                f["asked"] = True
                self._ask("ask_narrow", "That folder has a lot in it. What type of file, or which month?")
        elif not r["paths"]:
            self._gone("I couldn't find that.")
        else:
            paths = r["paths"]
            n = len(paths)
            if n > finder.DEEP_CAP and not f["asked"]:
                f["asked"] = True
                self._ask("ask_narrow", f"I'd have to read {n:,} {finder.noun(d)}. Which month?")
                return
            f["targets"] = paths[:finder.DEEP_CAP]
            if r["named"]:                   # they said where to look
                self._read()
                return
            n = len(f["targets"])
            when = f" {d['when']}" if d["when"] else ""
            what = "scanned PDFs" if r.get("scans") else finder.noun(d)
            question = f"I couldn't find it. Want me to look through your {what}{when}?"
            f.update(stage="ask_deep", question=question)
            self._tuck.stop()
            self.overlay.ensure_open()
            self.overlay.show_confirm(f"{question} That's {n:,} file{'s' if n != 1 else ''}.", "Look through them")
            self._say(question)
            self._expire.start(self.EXPIRE_SECS * 1000)

    def _typed(self, text):
        """Typed instead of said: understood the way speech is (a plain answer straight away, else the decider)."""
        if not self.waiting_for_answer():
            return
        from app.core.intent_router import find_quick
        d = find_quick(text, self.decider_state())
        if d is not None:
            logger.info(f"[find] typed {text!r} -> {d['intent']} (no AI)")
            from app.ui.assistant_page import AssistantPage
            AssistantPage._act_on_find(self, d["intent"], d["task"], text, {})
            return
        ap = getattr(self.mw, "assistant_page", None)
        if ap is not None:
            ap.run_voice_task(text, source="live")

    def _row_clicked(self, i):
        if self._f is not None and self._f["stage"] == "results":
            self.open(str(i + 1))

    def _folder_clicked(self, i):
        if self._f is not None and self._f["stage"] == "results":
            self.reveal(str(i + 1))

    def _confirm_clicked(self):
        if self._f is not None and self._f["stage"] in ("ask_deep", "ask_closest"):
            self.deep_go()

    def _read(self):
        f = self._f
        paths = f["targets"]
        f.update(stage="reading", read=True, question="")
        self._expire.stop()
        self.overlay.ensure_open()
        self.overlay.show_thinking(f"Reading {len(paths)} files, 0 of {len(paths)}")
        self._rid += 1
        worker = _ReadWorker(paths, self._agents(), self._rid)
        self._reader = worker
        self._hold(worker)
        worker.progress.connect(self._on_progress, Qt.QueuedConnection)
        worker.done.connect(self._on_read, Qt.QueuedConnection)
        worker.start()

    def _on_progress(self, done, total, rid):
        if rid == self._rid and self._f is not None and self._f["stage"] == "reading":
            self.overlay.show_thinking(f"Reading {total} files, {done} of {total}")

    def _on_read(self, result, rid):
        f = self._f
        if rid != self._rid or f is None or result.get("cancelled"):
            return
        self._reader = None
        logger.info(f"[find] read: {result}")
        self.overlay.show_thinking("Checking what I read…")
        d, heard, paths, agents = f["d"], f["heard"], f["targets"], self._agents()
        f["stage"] = "searching"
        self._run(lambda: finder.after_deep(d, heard, paths, agents), self._on_found)

    def _chip(self, label):
        lo, hi = finder.month_range(label)
        self.more(label, {"after": lo, "before": hi, "date_words": [label], "when": f"from {label}"} if lo
                  else {"words": [[label]]})

    def _stop_jobs(self):
        self._rid += 1
        self._tuck.stop()
        self._expire.stop()
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancelled.set()

    def _on_dismissed(self):
        if self._f is None:
            return
        self._stop_jobs()
        self._f = None
        self.overlay.take_over(self.ORGANIZE_BRAND)

    def _on_quit(self):
        reader = self._reader
        self._stop_jobs()
        if reader is not None:
            reader.wait(4000)
        if self._thumb_dir:
            shutil.rmtree(self._thumb_dir, ignore_errors=True)
