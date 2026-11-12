"""The REAL Filect main window, off-screen, for tests that need the whole app (the first-run guide, the page tours).

Safe by construction: the app-data folder is a temp folder, settings are never saved, no global shortcut or keyboard
hook is installed (dictation, the agent shortcut, Quick Search), no folder is watched, nothing is sent (track is a
recorder), no update check. Everything is put back by close().
"""
import os
import tempfile
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication


def pump(s=0.1):
    app = QApplication.instance()
    t = time.time()
    while time.time() - t < s:
        app.processEvents()
        time.sleep(0.005)


class RealWindow:
    def __init__(self, size=(1250, 860)):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        from app.core.settings import settings
        self.tmp = Path(tempfile.mkdtemp(prefix="filect-window-"))
        self.tracked = []
        mp = self.mp = pytest.MonkeyPatch()
        # FIRST the temp folder and no saving - before anything that opens a file of yours is imported
        mp.setattr(type(settings), "get_app_data_dir", lambda _self: self.tmp)
        mp.setattr(type(settings), "_save_config", lambda *a, **k: None)       # NEVER the user's settings.json
        # The index: a module made it when first imported (maybe by another test, with YOUR folder). Every part of
        # the app shares that one object, and it opens its file for each call: point it at a temp file.
        from app.core import database
        mp.setattr(database.file_index, "db_path", self.tmp / "file_index.db")
        database.file_index._init_database()
        database.file_index._ensure_fts_healthy()
        assert str(database.file_index.db_path).startswith(str(self.tmp)), database.file_index.db_path
        from app.core import supabase_client as sc
        import app.ui.dictation as dct
        import app.ui.agent_voice as av
        import app.ui.main_window as mwm
        import app.ui.assistant_page as ap
        from app.ui.theme_manager import theme_manager
        self._theme0 = theme_manager.current_theme
        for k, v in (("watch_custom_folders", []), ("watch_common_folders", False), ("auto_organize_folders", []),
                     ("auto_organize_auto_start", False), ("auto_organize_paused", True),
                     ("has_completed_onboarding", True),             # (the guide would open by itself otherwise)
                     ("onboarding_remind_count", 0), ("page_tours_seen", []), ("live_tour_seen", False),
                     ("agents_tour_seen", False), ("agent_was_ready", False), ("theme", settings.theme)):
            mp.setattr(settings, k, v)
        mp.setattr(sc, "track", lambda e, **k: self.tracked.append((e, k)))
        mp.setattr(dct.DictationController, "start", lambda _self: None)
        mp.setattr(av.AgentVoiceController, "start", lambda _self: True)

        def no_global_shortcut(win):
            from app.ui.quick_search_overlay import QuickSearchOverlay
            win.quick_overlay = QuickSearchOverlay(None)
        mp.setattr(mwm.MainWindow, "setup_quick_search", no_global_shortcut)
        mp.setattr(mwm.MainWindow, "_check_for_updates", lambda _self: None)
        mp.setattr(mwm.MainWindow, "_run_background_db_cleanup", lambda _self: None)   # (it prunes the index)
        mp.setattr(ap.AssistantPage, "_show_idle_overlays", lambda _self: None)
        mp.setattr(ap.AssistantPage, "_load_recent", lambda _self: None)
        mp.setattr(ap, "DEFAULT_WORKSPACE", str(self.tmp / "Filect Assistant"))
        self.W = mwm.MainWindow()
        self.W.resize(*size)
        self.W.show()
        pump(0.3)
        assert str(database.file_index.db_path).startswith(str(self.tmp)), "the index isn't the temp one"
        assert str(settings._config_file()).startswith(str(self.tmp)), "settings point at your folder"

    def close(self):
        W = self.W
        try:
            if getattr(W, "_page_tour", None) is not None:
                W._page_tour.reject()
            W.assistant_page.shutdown()
        except Exception:
            pass
        W.hide()
        W.deleteLater()
        pump(0.1)
        from app.ui.theme_manager import theme_manager
        try:                                         # (back to the theme it had - while saving is still stubbed)
            if theme_manager.current_theme != self._theme0:
                theme_manager.apply_theme(self._theme0)
        except Exception:
            pass
        self.mp.undo()
