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


if __name__ == "__main__":
    # Self-test: (input, expected). PASS/FAIL per case.
    CASES = [
        ("ten thousand dollars", "$10,000"),
        ("fifty percent", "50%"),
        ("twenty five dollars and ninety nine cents", "$25.99"),
        ("send ten thousand dollars, that's fifty percent",
         "send $10,000, that's 50%"),
        ("one million dollars", "$1,000,000"),
        ("twenty five percent", "25%"),
        ("I have one question", "I have one question"),      # lone small number untouched
        ("the quick brown fox", "the quick brown fox"),       # no numbers untouched
        ("", ""),                                             # empty untouched
        ("two hundred fifty", "250"),                         # multiword bare cardinal
    ]
    p = f = 0
    for src, exp in CASES:
        got = format_spoken(src)
        ok = got == exp
        p += ok; f += (not ok)
        print(f"{'PASS' if ok else 'FAIL'}: {src!r} -> {got!r}" + ("" if ok else f"   (expected {exp!r})"))
    print("=" * 56)
    print(f"dictation_format self-test: {p} PASS, {f} FAIL")
    import sys; sys.exit(1 if f else 0)
