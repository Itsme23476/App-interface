"""Each page's "See what it can do": the first-run guide's own panel (onboarding.OnboardingOverlay in page-tour
mode): the app dimmed, a bright cutout around the real button, a drawing, Back / Next, Esc closes.

A step points at widgets by their path on the main window. Several paths = the cutout covers whichever of them
are showing; none showing (the Search filters before a search) = the whole app dimmed, the panel still explains it.

Ported from the Mac (page_tours.py, 92e895f) for Windows:
  * Windows page numbers (Voice is 3 and Settings 4 here; the Mac has them the other way round);
  * Windows keys and words: Ctrl + Win to dictate (+ Shift search, + Alt organize), hold Ctrl + Alt to talk to the
    agent (or what you set), Shift + Ctrl to draw, Ctrl Ctrl to collect; "your PC", "the card at the top";
  * what Windows doesn't have isn't shown: no Mute on the Assistant page, no "collect" with the agent shortcut, no
    double-tap to keep dictation listening;
  * the button reuses live_page.style_tour_button (Windows' cheaper glow: the Mac's animated blur cost 20 % of a core).
"""
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QPushButton

from app.core.settings import settings

SEARCH, ORGANIZE, INDEX, VOICE, SETTINGS, AGENTS, ASSISTANT = range(7)   # Windows page_stack numbers

_NAMES = {"ctrl": "Ctrl", "control": "Ctrl", "shift": "Shift", "alt": "Alt", "option": "Alt", "win": "Win",
          "cmd": "Win", "command": "Win", "space": "Space", "esc": "Esc", "escape": "Esc", "tab": "Tab",
          "enter": "Enter", "return": "Enter"}


def _keys(shortcut: str, default: str = "ctrl+alt+h") -> list:
    """'ctrl+alt+h' → Ctrl + Alt + H (Windows key names, as printed on the keys)."""
    out = []
    for part in (shortcut or "").lower().split("+"):
        if part.strip():
            out += ["+", _NAMES.get(part.strip(), part.strip().upper())]
    return out[1:] or _keys(default, default)


def _words(shortcut: str, default: str) -> str:
    """'ctrl+alt' → 'Ctrl + Alt' (for the text)."""
    return " ".join(_keys(shortcut, default))


def _assistant():
    lp = "live_page."
    return ASSISTANT, [
        (("icon", "mic"), "Just talk",
         "• Click Start listening and say what you want, like you'd tell a person\n• When you pause, Claude Code "
         "or Codex gets it\n• Keep talking about the same thing to follow up", lp + "start_btn"),
        (("keys", ["Shift", "→", "Ctrl", "+", "Win"]), "Mute it",
         "• Recording a video or on a call? Press Shift, then Ctrl + Win\n• Or click Mute while it's listening\n"
         "• The mic closes until you do it again", lp + "mute_btn"),
        (("keys", ["Shift", "+", "Ctrl"]), "Point at your screen",
         "• While it listens, press Shift + Ctrl\n• Draw in red: circle a button, underline a line\n• Press them "
         "again to snap it. It goes with what you say next", (lp + "_draw_lbl", lp + "draw_key")),
        (("keys", ["Ctrl", "Ctrl", "→", "Ctrl"]), "Several things at once",
         "• Double-tap Ctrl to start collecting\n• Talk, scroll and snap as much as you want\n• Tap Ctrl once "
         "to send it all as one message", (lp + "_many_lbl", lp + "_many_key")),
        (("keys", ["Esc"]), "Changed your mind?",
         "• Esc throws away what you're saying\n• It also cancels a request it's still checking, or stops it "
         "talking\n• Starting to talk stops it talking too", None),
        (("icon", "gear"), "Make it yours",
         "• Which agent gets new requests\n• How long it waits after you stop talking\n• Whether it speaks its "
         "replies, and in which voice", (lp + "_set_card", lp + "_show_set_btn")),
        (("icon", "clock"), "Everything it did",
         "• History lists what you asked, newest first\n• Chats open in Claude Code or Codex\n• Organizing opens "
         "its details, with Revert", lp + "tab_btns.1"),
        (("icon", "lock"), "It only hears you",
         "• Only your speech is sent, never the silence\n• It steps aside while another app uses the mic\n• And "
         "while your PC is locked", lp + "_hint"),
    ]


def _agents():
    v = "assistant_page.view."
    hold = _words(getattr(settings, "assistant_voice_shortcut", ""), "ctrl+alt")
    return AGENTS, [
        (("icon", "chat"), "Ask for anything",
         "• Type what you want. Claude Code or Codex does the work on your PC\n• Each job gets its own chat\n• Or "
         "click the mic and say it", v + "composer.box"),
        (("keys", _keys(getattr(settings, "assistant_voice_shortcut", ""), "ctrl+alt")), "Talk to it from anywhere",
         f"• Hold {hold}, say what you want, let go\n• Works in any app. Filect's window doesn't need to be open\n"
         "• Changed your mind? Press Esc", None),
        (("keys", ["Space"]), "Show it your screen",
         f"• While you hold {hold} and talk, press Space (or right-click)\n• Draw on your screen in red to point at "
         "something\n• Press again to snap it, as many times as you like", None),
        (("logos",), "Pick the agent",
         "• Claude Code or Codex\n• The model, and how hard it thinks\n• All in this menu", v + "composer.agent_pill"),
        (("icon", "shield"), "How much it may do",
         "• Ask first: it checks with you before it runs commands or changes files\n• Full access: it just does "
         "the work", v + "composer.access_pill"),
        (("icon", "sidebar"), "Your chats",
         "• Open the list to see past chats\n• Click one, and what you type continues it\n• Want it hands-free? "
         "Use the Assistant page and just talk", (v + "list", v + "show_list_btn")),
    ]


def _voice():
    return VOICE, [
        (("keys", ["Ctrl", "+", "Win"]), "Hold Ctrl + Win and talk",
         "• Your words appear wherever you're typing, in any app\n• Ctrl + Win + Shift searches your files\n"
         "• Ctrl + Win + Alt organizes a folder", "voice_shortcuts_card"),
        (("icon", "type"), "Custom Words",
         "• Add names and terms it should spell right\n• Your company, your clients, the jargon of your field",
         "voice_custom_words_card"),
        (("icon", "globe"), "Language",
         "• Auto-detect works for most speech\n• Pick one language to have it write only in that one\n• Helps with "
         "non-English or mixed speech", "voice_language_card"),
        (("icon", "sparkle"), "Polishing",
         "• None: word for word\n• Light: drops filler words and fixes punctuation\n• Polished: also smooths the "
         "phrasing", "voice_cleanup_card"),
        (("icon", "mute"), "Mute while recording",
         "• Turns your PC's sound off while you hold Ctrl + Win\n• So a video or music doesn't get into the mic\n"
         "• It comes back the moment you let go", "voice_mute_card"),
        (("icon", "clock"), "History",
         "• Everything you've dictated lately\n• Copy one again, or delete it", "voice_history_card"),
    ]


def _organize():
    op = "organize_page."
    now, auto = {"sub_tab": 0}, {"sub_tab": 1}
    return ORGANIZE, [
        ("organize", "Say how you want it sorted",
         "• In your own words: by client, by year, photos by date\n• Or click the mic and say it",
         op + "instruction_card", now),
        (("icon", "folder"), "Pick the folder",
         "• Its files get sorted into subfolders inside it\n• Nothing is moved out of it", op + "dest_card", now),
        (("icon", "sparkle"), "See the plan first",
         "• Generate Plan shows where every file would go\n• Nothing moves until you click Apply",
         op + "generate_button", now),
        (("icon", "refresh"), "Undo it any time",
         "• History lists every time you organized\n• Revert puts the files back where they were",
         op + "history_button", now),
        (("icon", "shield"), "Keep files where they are",
         "• Pin a file or a folder\n• Organizing never moves what's pinned", op + "pinned_button", now),
        ("organize", "Keep a folder tidy",
         "• Auto-Organize sorts new files as they arrive\n• Set the folders with Edit, then press Start\n• By voice: "
         "tell the Assistant \"keep my Downloads tidy\"", op + "watch_card", auto),
    ]


def _search():
    return SEARCH, [
        ("search", "Describe what you're looking for",
         "• In your own words: \"the invoice from the dentist\"\n• It looks inside files, not just at their names\n"
         "• Photos and videos too, by what's in them", "search_container"),
        (("keys", ["Ctrl", "+", "Win", "+", "Shift"]), "Or just say it",
         "• Hold Ctrl + Win + Shift in any app and say what you need\n• No need to open this page first",
         "voice_hint_row"),
        (("keys", _keys(settings.quick_search_shortcut)), "Search from anywhere",
         "• This shortcut opens a search box over any app\n• In an Open or Save window, pick a file and Filect "
         "fills it in\n• Change the shortcut in Settings", None),
        (("icon", "layers"), "Narrow it down",
         "• After a search: filter by type and date\n• Select results to open their folder, copy their paths or "
         "export them", ("type_filter", "date_filter")),
    ]


def _index():
    return INDEX, [
        ("index", "Add files or a folder",
         "• Drag them here, or click to choose\n• Filect reads what's inside: text, photos, and what's said in "
         "videos\n• Search and Organize work from this", "drop_zone"),
        (("icon", "bolt"), "More options",
         "• Watch for New Downloads: new files are read as they arrive\n• Index Entire PC Now: read everything once",
         "more_options_header"),
        (("icon", "file"), "What Filect has read",
         "• Every file it has read so far\n• The number shows how many", "view_files_btn"),
    ]


def _settings():
    return SETTINGS, [
        (("logos",), "Connect to Claude",
         "• Let Claude Desktop and Codex use Filect\n• They can then search, read and organize your files\n• Restart "
         "them after connecting", "_mcp_card"),
        (("icon", "search"), "Quick Search",
         "• The shortcut that opens search over any app\n• Auto-Paste fills the file you pick into the Open window "
         "you're in\n• Auto-Confirm also presses Open", "quick_search_card"),
        (("icon", "shield"), "Files that never move",
         "• Exclusions: anything matching these patterns is skipped when organizing\n• For example .json or .py "
         "files", "exclusions_toggle_btn"),
        (("icon", "user"), "Your account",
         "• Your plan and your subscription\n• Sign out here", "account_card"),
        (("icon", "appearance"), "Light or dark", "• Switch the theme here", "appearance_card"),
        (("icon", "book"), "This guide again",
         "• Show Welcome Guide replays the first-run guide\n• Every page has its own tour: \"See what it can do\"",
         "help_card"),
    ]


TOURS = {"assistant": _assistant, "agents": _agents, "voice": _voice, "organize": _organize, "search": _search,
         "index": _index, "settings": _settings}


def steps(name: str) -> list:
    """Built when the tour opens (the Quick Search and agent shortcuts can change)."""
    page, items = TOURS[name]()
    out = []
    for i, (art, title, text, highlight, *extra) in enumerate(items):
        s = {"art": art, "title": title, "description": text, "nav_index": page, "highlight": highlight,
             "button_text": "Done" if i == len(items) - 1 else "Next"}
        for e in extra:
            s.update(e)
        out.append(s)
    return out


def open_tour(main_window, name: str):
    from app.ui.onboarding import OnboardingOverlay
    try:
        from app.core.supabase_client import track
        track("page_tour_opened", page=name)
    except Exception:
        pass
    old = getattr(main_window, "_page_tour", None)
    if old is not None:
        try:
            old.reject()                           # (one at a time; its `finished` deletes its dim and pill)
        except RuntimeError:
            pass
    t = OnboardingOverlay(main_window, steps=steps(name), tour=name)

    def gone(_=0):
        for w in (t.continue_btn, t.spotlight):    # (they live on the main window)
            if w is not None:
                w.deleteLater()
        t.deleteLater()
        if getattr(main_window, "_page_tour", None) is t:
            main_window._page_tour = None
    t.finished.connect(gone)
    main_window._page_tour = t
    t.show()
    return t


def is_seen(name: str) -> bool:
    if name == "assistant":
        return bool(settings.live_tour_seen)
    if name == "agents":
        return bool(settings.agents_tour_seen)
    return name in settings.page_tours_seen


def mark_seen(name: str) -> None:
    if is_seen(name):
        return
    if name == "assistant":
        settings.live_tour_seen = True
    elif name == "agents":
        settings.agents_tour_seen = True
    else:
        settings.page_tours_seen = list(settings.page_tours_seen) + [name]
    try:
        settings._save_config()
    except Exception:
        pass


def tour_button(name: str) -> QPushButton:
    """"New here? See what it can do" (glowing) until opened once, then a quiet link. Opens `name`'s tour."""
    from app.ui.live_page import _colors, style_tour_button
    from app.ui.theme_manager import theme_manager
    btn = QPushButton(" See what it can do")
    btn.setObjectName(f"{name}TourButton")
    btn.setIconSize(QSize(13, 13))
    btn.setCursor(Qt.PointingHandCursor)
    btn.setFixedHeight(30)

    def restyle(*_):
        try:
            style_tour_button(btn, is_seen(name), _colors())
        except RuntimeError:                       # (the button is gone)
            pass

    def clicked():
        mark_seen(name)
        restyle()
        open_tour(btn.window(), name)
    restyle()
    theme_manager.theme_changed.connect(restyle)
    btn.clicked.connect(clicked)
    return btn
