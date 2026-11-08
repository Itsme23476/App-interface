"""AI-suggested Custom Words from the user's niche.

The user describes their field in plain words; we ask the AI for DISTINCTIVE terms a
speech-to-text model is likely to mis-hear (brand/tool names, jargon, acronyms, proper
nouns) — never ordinary words. The user then picks which to add. Chosen terms go to
`settings.dictation_custom_terms`, which already flows to Grok's key-term biasing (so it
improves raw transcription regardless of the AI Cleanup toggle) and the cleanup pinning.

Design notes:
  * One cheap gpt-5-mini call via the existing proxy (entitlement-gated). No new backend.
  * Two guards keep biasing SAFE — the prompt forbids common words, AND _is_distinctive()
    drops any plain dictionary word the model slips in (biasing common words is useless and
    can skew normal speech). Everything is opt-in: the user selects what to add.
"""
import re
import logging

logger = logging.getLogger(__name__)

MAX_TERMS = 25
MAX_TERM_LEN = 50


def _dictionary():
    """A spellchecker used only to drop plain English words; None if unavailable (then we
    trust the prompt + pattern check)."""
    try:
        from spellchecker import SpellChecker
        return SpellChecker()
    except Exception:
        return None


def _is_distinctive(term: str, sc) -> bool:
    """True if `term` looks like jargon/a name worth biasing, False if it's a plain word.
    Keeps acronyms (ALL CAPS), camelCase/PascalCase, anything with a digit or . - + # /,
    and multi/single words where at least one token isn't in the dictionary. Drops plain
    words even when Title-cased (e.g. 'Marketing')."""
    t = term.strip()
    if not t:
        return False
    if len(t) >= 2 and t.isupper():
        return True                                   # acronym: GPT, API, JSON
    if re.search(r"[a-z][A-Z]", t) or re.search(r"\d", t) or re.search(r"[._+#/-]", t):
        return True                                   # PostgreSQL, Next.js, GPT-4o, C#
    words = re.findall(r"[A-Za-z]+", t)
    if not words:
        return False
    if sc is None:
        return True                                   # no dictionary -> trust the AI/prompt
    return any(w.lower() not in sc for w in words)    # distinctive if any token is non-dictionary


def clean_suggestions(terms, max_terms: int = MAX_TERMS) -> list:
    """Strip/dedup/cap a raw term list and drop plain words. Pure + testable."""
    sc = _dictionary()
    out, seen = [], set()
    for raw in (terms or []):
        t = str(raw).strip().strip('"\'.,')
        if not t or len(t) > MAX_TERM_LEN:
            continue
        k = t.lower()
        if k in seen:
            continue
        if not _is_distinctive(t, sc):
            continue
        seen.add(k)
        # Display-normalize: capitalize the first letter of an all-lowercase suggestion
        # ("grok" -> "Grok"). Leave acronyms/camelCase/multi-cap terms untouched. (Biasing
        # itself is case-insensitive; this is purely so the chips look consistent.)
        if t.islower():
            m = re.search(r"[a-z]", t)
            if m:
                i = m.start()
                t = t[:i] + t[i].upper() + t[i + 1:]
        out.append(t)
        if len(out) >= max_terms:
            break
    return out


def suggest_terms_for_niche(niche: str, max_terms: int = MAX_TERMS) -> list:
    """Ask the AI for distinctive custom-word candidates for the user's field. Returns a
    cleaned list (possibly empty — e.g. not entitled, or the model returned nothing).
    Never raises."""
    niche = (niche or "").strip()
    if not niche:
        return []
    try:
        from .vision import _call_openai_proxy
        system = (
            "You build a key-term biasing list for a speech-to-text dictation app, for someone "
            "who works in a given field. List DISTINCTIVE terms the transcriber is likely to GET "
            "WRONG: jargon, acronyms, tools and products, techniques, metrics, named methods, and "
            "proper nouns specific to the field. Make the list DIVERSE across the field's whole "
            "workflow — do NOT just list variations of the field's own name, and do NOT return "
            "near-duplicates of each other. Strongly prefer terms that are coined, abbreviated "
            "(acronyms), non-dictionary, or multi-word technical phrases. EXCLUDE ordinary words "
            "any transcriber already spells correctly. No duplicates, no explanations. Return "
            "JSON only as an object: {\"terms\": [\"Term1\", \"Term2\"]}."
        )
        user = (f"Field: {niche}\nReturn up to {max_terms} distinctive terms, covering different "
                "parts of the field — not minor variations of a single word.")
        resp = _call_openai_proxy(
            "chat",
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=500, model="gpt-5-mini",
        )
        if not resp:
            return []
        content = (resp.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        if not content:
            return []
        import json
        data = None
        try:
            data = json.loads(content)
        except Exception:
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:
                    data = None
        terms = data.get("terms") if isinstance(data, dict) else (data if isinstance(data, list) else None)
        if not isinstance(terms, list):
            return []
        return clean_suggestions(terms, max_terms)
    except Exception as e:
        logger.warning(f"suggest_terms_for_niche failed: {e}")
        return []


# NOTE: a "learn from your dictation history" suggester was prototyped and dropped. The history
# is the wrong source for a *biasing* list: it contains both correctly-transcribed coined terms
# and mis-transcriptions, and nothing can reliably tell them apart — raw mining suggests the
# mis-spellings (e.g. "Superbase"), and an AI "fix" pass corrupts correct coined names it doesn't
# know (observed: "Filect" -> "Filecat"). Either way a wrong spelling lands in the biasing list,
# which makes transcription worse. Correct-spelling suggestions come from the niche prompt above
# (model knowledge) and from the user's own typed/edited terms (ground truth).


if __name__ == "__main__":
    # Offline checks for the pure helpers (no network).
    assert clean_suggestions(["GPT", "Supabase", "Marketing", "machine learning",
                              "Next.js", "PostgreSQL", "the", "Supabase"]) == \
        ["GPT", "Supabase", "Next.js", "PostgreSQL"], clean_suggestions(
            ["GPT", "Supabase", "Marketing", "machine learning", "Next.js", "PostgreSQL", "the", "Supabase"])
    assert suggest_terms_for_niche("") == []
    print("vocab pure-helper checks passed")
