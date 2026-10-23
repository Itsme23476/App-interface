"""Deterministic spoken-number -> written-form formatter for dictation output.

The STT model returns numbers spelled out ("ten thousand dollars", "fifty
percent"). This module converts the high-confidence, context-free cases to
written form instantly and for free (pure Python, no API call):

    "ten thousand dollars"                      -> "$10,000"
    "fifty percent"                             -> "50%"
    "twenty five dollars and ninety nine cents" -> "$25.99"

Safety: it only converts *multi-word* number spans (real quantities) plus
currency/percent contexts. A lone small number in prose ("I have one
question") and all other text are left untouched. Lossless on anything it
does not recognize.
"""
import re

_ONES = {"zero":0,"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,
    "nine":9,"ten":10,"eleven":11,"twelve":12,"thirteen":13,"fourteen":14,"fifteen":15,
    "sixteen":16,"seventeen":17,"eighteen":18,"nineteen":19}
_TENS = {"twenty":20,"thirty":30,"forty":40,"fifty":50,"sixty":60,"seventy":70,"eighty":80,"ninety":90}
_SCALES = {"hundred":100,"thousand":1000,"million":1000000,"billion":1000000000}
_NUMWORD = set(_ONES) | set(_TENS) | set(_SCALES)
_W = r"(?:%s)" % "|".join(sorted(_NUMWORD, key=len, reverse=True))
_SEQ = r"%s(?:[\s-]+(?:and[\s-]+)?%s)*" % (_W, _W)

def _parse_cardinal(phrase):
    total = current = 0
    for w in re.split(r"[\s-]+", phrase.lower()):
        if w in ("", "and"): continue
        if w in _ONES: current += _ONES[w]
        elif w in _TENS: current += _TENS[w]
        elif w == "hundred": current = (current or 1) * 100
        elif w in _SCALES: total += (current or 1) * _SCALES[w]; current = 0
    return total + current

def _is_multiword(phrase):
    return bool(re.search(r"[\s-]", phrase.strip()))

def format_spoken(text):
    if not text or not text.strip(): return text
    try:
        s = text
        s = re.sub(r"\b(%s)\s+dollars\s+and\s+(%s)\s+cents\b" % (_SEQ, _SEQ),
            lambda m: "$%s.%02d" % (f"{_parse_cardinal(m.group(1)):,}", _parse_cardinal(m.group(2))),
            s, flags=re.IGNORECASE)
        s = re.sub(r"\b(%s)\s+dollars\b" % _SEQ,
            lambda m: "$%s" % f"{_parse_cardinal(m.group(1)):,}", s, flags=re.IGNORECASE)
        s = re.sub(r"\b(%s)\s+percent\b" % _SEQ,
            lambda m: "%s%%" % f"{_parse_cardinal(m.group(1)):,}", s, flags=re.IGNORECASE)
        s = re.sub(r"\b(%s)\b" % _SEQ,
            lambda m: f"{_parse_cardinal(m.group(1)):,}" if _is_multiword(m.group(1)) else m.group(0),
            s, flags=re.IGNORECASE)
        return s
    except Exception:
        return text


# ---- deterministic text tidy + sentence-case (run even with AI polishing OFF) ----

_ACRONYM_RE = re.compile(r"^[A-Z0-9]{2,}$")  # 2+ chars, all caps/digits -> acronym


def tidy_text(text):
    """Context-free cleanup: collapse runs of spaces, drop a space before
    punctuation, fix the standalone pronoun 'i' -> 'I', and capitalize the
    first letter after sentence-ending punctuation (. ! ?). Lossless otherwise."""
    if not text:
        return text
    try:
        s = text
        s = re.sub(r"[ \t]{2,}", " ", s)                      # collapse runs of spaces/tabs
        s = re.sub(r"[ \t]+([,.;:!?%)])", r"\1", s)           # no space before punctuation
        s = re.sub(r"\bi\b", "I", s)                          # pronoun i -> I
        s = re.sub(r"([.!?]\s+)([a-z])",                      # capitalize after . ! ?
                   lambda m: m.group(1) + m.group(2).upper(), s)
        return s
    except Exception:
        return text


def _is_protected(word, custom_lower):
    """A word that must NOT be re-cased: an ALL-CAPS acronym or a user Custom Word."""
    bare = word.strip(".,!?;:)(\"'")
    if not bare:
        return False
    if _ACRONYM_RE.match(bare):
        return True
    return bare.lower() in custom_lower


def apply_sentence_case(text, before_caret, custom_terms=None):
    """Context-aware casing of the dictated text based on what precedes the caret.

    - ``before_caret is None`` -> return text UNCHANGED (safe no-op when we can't
      read the caret context; nothing breaks).
    - At a sentence start (field start, or right after . ! ? or a newline) the
      first word is Capitalized.
    - Mid-sentence, a STRAY leading capital is lowercased.
    - EXCEPT acronyms (ALL CAPS) and the user's Custom Words, left as-is.
    Internal sentence boundaries (after . ! ? / newline) are capitalized too.
    """
    if before_caret is None or not text:
        return text
    try:
        custom_lower = {str(t).strip().lower() for t in (custom_terms or []) if str(t).strip()}

        # Is the caret at a sentence-start position?
        ctx = before_caret.rstrip(" \t")
        at_start = (ctx == "") or ctx.endswith((".", "!", "?")) or ctx.endswith("\n")

        # Internal boundaries: capitalize the first lowercase letter after .!? or a newline.
        text = re.sub(r"([.!?]\s+|\n[ \t]*)([a-z])",
                      lambda m: m.group(1) + m.group(2).upper(), text)

        # Leading word: cased by caret context, unless protected.
        m = re.match(r"(\s*)(\S+)(.*)$", text, flags=re.DOTALL)
        if not m:
            return text
        pre, first, rest = m.group(1), m.group(2), m.group(3)
        if not _is_protected(first, custom_lower):
            idx = next((i for i, c in enumerate(first) if c.isalpha()), None)
            if idx is not None:
                if at_start and first[idx].islower():
                    first = first[:idx] + first[idx].upper() + first[idx + 1:]
                elif (not at_start) and first[idx].isupper():
                    first = first[:idx] + first[idx].lower() + first[idx + 1:]
        return pre + first + rest
    except Exception:
        return text


if __name__ == "__main__":
    import sys
    p = f = 0

    def _check(label, got, exp):
        global p, f
        ok = got == exp
        p += ok; f += (not ok)
        print(f"{'PASS' if ok else 'FAIL'}: {label}: {got!r}" + ("" if ok else f"   (expected {exp!r})"))

    for src, exp in [
        ("ten thousand dollars", "$10,000"),
        ("fifty percent", "50%"),
        ("twenty five dollars and ninety nine cents", "$25.99"),
        ("twenty five dollars", "$25"),
        ("ninety nine percent", "99%"),
        ("ten thousand", "10,000"),
        ("one million dollars", "$1,000,000"),
        ("I have one question", "I have one question"),
        ("", ""),
    ]:
        _check(f"format_spoken {src!r}", format_spoken(src), exp)

    for src, exp in [
        ("hello    world", "hello world"),
        ("hello , world", "hello, world"),
        ("i think i am", "I think I am"),
        ("done. next thing", "done. Next thing"),
        ("", ""),
    ]:
        _check(f"tidy_text {src!r}", tidy_text(src), exp)

    _check("case None no-op", apply_sentence_case("hello there", None), "hello there")
    _check("case start-of-field", apply_sentence_case("hello world", "", []), "Hello world")
    _check("case after period", apply_sentence_case("next item", "all done. ", []), "Next item")
    _check("case mid lowercases stray cap",
           apply_sentence_case("Then we left", "i was walking and ", []), "then we left")
    _check("case protects acronym", apply_sentence_case("API is great", "we use ", []), "API is great")
    _check("case protects custom word",
           apply_sentence_case("Filect rocks", "i love ", ["Filect"]), "Filect rocks")
    _check("case internal boundary", apply_sentence_case("hello. world", "", []), "Hello. World")

    print("=" * 56)
    print(f"dictation_format self-test: {p} PASS, {f} FAIL")
    sys.exit(1 if f else 0)
