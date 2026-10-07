"""Self-contained card widgets for the Voice page.

History, Custom Words, Language, Polishing (cleanup) and Mute-while-recording.
Ported from the Mac Filect build and adapted to the Windows codebase.
"""

from app.ui.voice_cards.custom_words_card import VoiceCustomWordsCard
from app.ui.voice_cards.language_card import VoiceLanguageCard
from app.ui.voice_cards.cleanup_card import VoiceCleanupCard
from app.ui.voice_cards.mute_card import VoiceMuteCard
from app.ui.voice_cards.history_card import VoiceHistoryCard

__all__ = [
    "VoiceCustomWordsCard",
    "VoiceLanguageCard",
    "VoiceCleanupCard",
    "VoiceMuteCard",
    "VoiceHistoryCard",
]
