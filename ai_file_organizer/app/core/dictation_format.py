"""Deterministic spoken-number -> written-form formatter for dictation output.

The STT model returns numbers spelled out ("ten thousand dollars", "fifty
percent"). This module converts the high-confidence, context-free cases to
written form instantly and for free (pure Python, no API call):

    "ten thousand dollars"                      -> "$10,000"
    "fifty percent"                             -> "50%"
    "twenty five dollars and ninety nine cents" -> "$25.99"

Safety: it only converts *well-formed multi-word* number spans (real quantities)
plus currency/percent contexts. A lone small number in prose ("I have one
question"), a spoken digit run ("five five five one two three four") or a year
("nineteen eighty four") and all other text are left untouched. Lossless on
anything it does not recognize.
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


def _num_value(phrase: str):
    """Int value if `phrase` is a WELL-FORMED cardinal; None if it's an ambiguous run that
    must NOT be summed — a spoken digit sequence ("five five five one two three four", a
    phone number / code) or a year said in groups ("nineteen eighty four"). Summing those
    was corrupting dictations ("five five five..." -> 25), so we leave them as words."""
    words = [w for w in re.split(r"[\s-]+", phrase.lower()) if w and w != "and"]
    if not words:
        return None
    if len(words) == 1:                        # a single number word
        return _parse_cardinal(words[0])
    if any(w in _SCALES for w in words):       # has hundred/thousand/... -> structured cardinal
        return _parse_cardinal(phrase)
    if words[0] in _TENS and len(words) == 2 and words[1] in _ONES:  # "twenty five"
        return _parse_cardinal(phrase)
    return None                                # digit sequence / ambiguous -> leave as words


_ONEW = r"(?:%s)" % "|".join(sorted(_ONES, key=len, reverse=True))


def format_spoken(text: str) -> str:
    """Format spelled-out numbers/currency/percent in a dictation transcript.
    Returns text unchanged on any internal error (formatting must never eat a dictation)."""
    if not text or not text.strip():
        return text
    try:
        s = text
        # 0) Decimals: "<n> point <d> <d> ..." -> "<n>.<dd>"  (three point five -> 3.5)
        def _dec(m):
            whole = _num_value(m.group(1))
            if whole is None:
                return m.group(0)
            digits = "".join(str(_ONES[w]) for w in re.split(r"[\s-]+", m.group(2).lower()) if w in _ONES)
            return f"{whole:,}.{digits}" if digits else m.group(0)
        s = re.sub(r"\b(%s)\s+point\s+((?:%s)(?:[\s-]+(?:%s))*)" % (_SEQ, _ONEW, _ONEW),
                   _dec, s, flags=re.IGNORECASE)
        # 1) Currency with cents: "<n> dollars and <m> cents" -> "$n.mm"
        def _cc(m):
            d, c = _num_value(m.group(1)), _num_value(m.group(2))
            return "$%s.%02d" % (f"{d:,}", c) if (d is not None and c is not None) else m.group(0)
        s = re.sub(r"\b(%s)\s+dollars\s+and\s+(%s)\s+cents\b" % (_SEQ, _SEQ), _cc, s, flags=re.IGNORECASE)
        # 2) Whole-dollar: "<n> dollars" -> "$n"
        s = re.sub(r"\b(%s)\s+dollars\b" % _SEQ,
                   lambda m: ("$%s" % f"{_num_value(m.group(1)):,}") if _num_value(m.group(1)) is not None else m.group(0),
                   s, flags=re.IGNORECASE)
        # 3) Percent: "<n> percent" -> "n%"
        s = re.sub(r"\b(%s)\s+percent\b" % _SEQ,
                   lambda m: ("%s%%" % f"{_num_value(m.group(1)):,}") if _num_value(m.group(1)) is not None else m.group(0),
                   s, flags=re.IGNORECASE)
        # 4) Bare numbers — only MULTI-word, WELL-FORMED cardinals, so lone "one"/"two" in
        #    prose AND digit sequences ("five five five") are left untouched.
        s = re.sub(r"\b(%s)\b" % _SEQ,
                   lambda m: f"{_num_value(m.group(1)):,}" if (_is_multiword(m.group(1)) and _num_value(m.group(1)) is not None) else m.group(0),
                   s, flags=re.IGNORECASE)
        # 5) Catch DIGIT-form currency/percent an upstream LLM cleanup may have left
        #    half-done ("10,000 dollars" -> "$10,000", "50 percent" -> "50%").
        s = re.sub(r"\$?(\d[\d,]*(?:\.\d+)?)\s+dollars\b", r"$\1", s, flags=re.IGNORECASE)
        s = re.sub(r"(\d[\d,]*(?:\.\d+)?)\s+percent\b", r"\1%", s, flags=re.IGNORECASE)
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


def _leading_term(text, custom_terms):
    """The Custom Word the dictation STARTS with, incl. multi-word ones ("Brugada syndrome", what
    the suggester adds) — else None. A one-word check alone missed those: 'Brugada' isn't a term by
    itself, so mid-sentence it was lowercased to 'brugada syndrome'."""
    low = text.lstrip().lower()
    for t in sorted((str(t).strip() for t in (custom_terms or [])), key=len, reverse=True):
        tl = t.lower()
        if tl and low.startswith(tl) and (len(low) == len(tl) or not low[len(tl)].isalnum()):
            return t
    return None


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
        term = _leading_term(text, custom_terms)
        if term is not None:
            # Starts with a Custom Word: keep its own spelling. Mid-sentence that also means its
            # own first-letter case ("Brugada syndrome" stays "B", "cardiac MRI" becomes "c").
            if not at_start and term[0].isalpha():
                idx = next((i for i, c in enumerate(first) if c.isalpha()), None)
                if idx is not None:
                    first = first[:idx] + term[0] + first[idx + 1:]
            return pre + first + rest
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
        # digit runs / years are NOT summed (they used to corrupt into "25" / "103")
        ("call five five five one two three four", "call five five five one two three four"),
        ("born in nineteen eighty four", "born in nineteen eighty four"),
        ("three point five", "3.5"),
        ("10,000 dollars", "$10,000"),
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
    _check("case protects a multi-word custom term",
           apply_sentence_case("Brugada syndrome was ruled out", "we suspected ", ["Brugada syndrome"]),
           "Brugada syndrome was ruled out")
    _check("case uses a term's own lowercase start",
           apply_sentence_case("Cardiac MRI was clear", "the ", ["cardiac MRI"]), "cardiac MRI was clear")
    _check("case term must match whole words",
           apply_sentence_case("Brugadas are rare", "and ", ["Brugada"]), "brugadas are rare")

    print("=" * 56)
    print(f"dictation_format self-test: {p} PASS, {f} FAIL")
    sys.exit(1 if f else 0)
