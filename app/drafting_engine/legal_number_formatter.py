"""Deterministic Indian legal number formatting."""
from __future__ import annotations

import re

ONES = [
    "शून्य", "एक", "दो", "तीन", "चार", "पाँच", "छह", "सात", "आठ", "नौ",
    "दस", "ग्यारह", "बारह", "तेरह", "चौदह", "पंद्रह", "सोलह", "सत्रह",
    "अठारह", "उन्नीस", "बीस", "इक्कीस", "बाईस", "तेईस", "चौबीस", "पच्चीस",
    "छब्बीस", "सत्ताईस", "अट्ठाईस", "उनतीस", "तीस", "इकतीस", "बत्तीस",
    "तैंतीस", "चौंतीस", "पैंतीस", "छत्तीस", "सैंतीस", "अड़तीस", "उनतालीस",
    "चालीस", "इकतालीस", "बयालीस", "तैंतालीस", "चवालीस", "पैंतालीस", "छियालीस",
    "सैंतालीस", "अड़तालीस", "उनचास", "पचास", "इक्यावन", "बावन", "तिरपन",
    "चौवन", "पचपन", "छप्पन", "सत्तावन", "अट्ठावन", "उनसठ", "साठ", "इकसठ",
    "बासठ", "तिरसठ", "चौंसठ", "पैंसठ", "छियासठ", "सड़सठ", "अड़सठ", "उनहत्तर",
    "सत्तर", "इकहत्तर", "बहत्तर", "तिहत्तर", "चौहत्तर", "पचहत्तर", "छिहत्तर",
    "सतहत्तर", "अठहत्तर", "उनासी", "अस्सी", "इक्यासी", "बयासी", "तिरासी",
    "चौरासी", "पचासी", "छियासी", "सतासी", "अट्ठासी", "नवासी", "नब्बे", "इक्यानबे",
    "बानबे", "तिरानबे", "चौरानबे", "पंचानबे", "छियानबे", "सत्तानबे", "अट्ठानबे",
    "निन्यानबे",
]

UNITS = ["", "हजार", "लाख", "करोड़"]


def _under_1000(n: int) -> str:
    if n < 100:
        return ONES[n]
    h, r = divmod(n, 100)
    if r:
        return f"{ONES[h]} सौ {ONES[r]}"
    return f"{ONES[h]} सौ"


def hindi_number(n: int) -> str:
    n = int(n)
    if n < 0:
        return "ऋण " + hindi_number(-n)
    if n < 100:
        return ONES[n]
    if n < 1000:
        return _under_1000(n)

    parts: list[str] = []
    crore, n = divmod(n, 10_000_000)
    if crore:
        parts.append(_under_1000(crore) + " करोड़")
    lakh, n = divmod(n, 100_000)
    if lakh:
        parts.append(_under_1000(lakh) + " लाख")
    thousand, n = divmod(n, 1000)
    if thousand:
        parts.append(_under_1000(thousand) + " हजार")
    if n:
        parts.append(_under_1000(n))
    return " ".join(parts)


def hindi_year(year: int) -> str:
    if 1000 <= year <= 1999:
        first, last = divmod(year, 100)
        if last:
            return f"{hindi_number(first)} सौ {hindi_number(last)}"
        return f"{hindi_number(first)} सौ"
    return hindi_number(year)


def hindi_date(day: int, month: int, year: int) -> str:
    months = {
        1: "जनवरी", 2: "फरवरी", 3: "मार्च", 4: "अप्रैल", 5: "मई", 6: "जून",
        7: "जुलाई", 8: "अगस्त", 9: "सितंबर", 10: "अक्टूबर", 11: "नवंबर", 12: "दिसंबर",
    }
    month_word = months.get(month)
    if not month_word:
        return ""
    return f"{hindi_number(day)} {month_word} {hindi_year(year)}"


_UNIT_RE = r"वर्ष|साल|वर्षों|सालों|डिसमिल|डेसिमल|बीघा|बिस्वा|एकड़|एकड़|हेक्टेयर|हेयर|मीटर|फीट|फुट|गज|वर्गफुट|वर्ग फीट|किलो|किलोग्राम|ग्राम|लीटर|प्रतिशत|रुपये|रुपए|रुपया|रु\.?|Rs\.?|₹"
_KEYWORD_RE = r"(?:गाटा|खसरा|सर्वे|प्लॉट|प्लाट|भूखण्ड|भूखंड|खतौनी|चक)(?:\s*(?:संख्या|सं०|सं\.|क्रमांक|नंबर|नं०|नं\.))?|कुल|मात्र|लगभग|आयु|उम्र|वय"

_DATE_RE = re.compile(r"(?<!\w)(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?!\w)")
_MONEY_RE = re.compile(r"(?<![\w.])([0-9]{1,3}(?:,[0-9]{2,3})*|[0-9]+)(?=\s*(?:रुपये|रुपए|रुपया|रु\.?|Rs\.?|₹))", re.I)
_UNIT_RE_C = re.compile(rf"(?<![\w])([0-9]{{1,9}})(?=\s*(?:{_UNIT_RE})(?!\w))", re.I)
_KEYWORD_NUM_RE = re.compile(rf"(?P<prefix>(?:{_KEYWORD_RE})\s*)(?P<num>[0-9]{{1,9}})(?![0-9])", re.I)


def _already_expanded(text: str, start: int, end: int) -> bool:
    tail = text[end:end + 90]
    head = text[max(0, start - 20):start]
    return bool(re.match(r"\s*\([^)]{1,100}\)", tail)) or bool(re.search(r"\([^)]{1,100}\)\s*$", head))


def _fmt_int(raw: str) -> str:
    try:
        val = int(raw.replace(",", ""))
        if val >= 100000 and "," in raw:
            return raw
        return f"{val:,}" if "," in raw or val >= 1000 else raw
    except ValueError:
        return raw


def _clean_erroneous_expansions(text: str) -> str:
    s = re.sub(r"\bबीस\s+सौ(?=\s+[\u0900-\u097F])", "दो हजार", text)
    s = re.sub(r"(धारा\s+\d+)\s*\([^)]{1,30}\)(?=\s*लगायत)", r"\1", s)
    num_words = "|".join(ONES)
    s = re.sub(
        rf"((?:प्रतिवादी|वादी|विपक्षी)\s*(?:संख्या|सं०|सं\.|नं०|नं\.)\s*0?\d+)\s*\(\s*(?:{num_words})\s*\)",
        r"\1",
        s,
    )
    return s


def _replace_dates(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        d, mo, y = map(int, m.groups())
        if not (1 <= d <= 31 and 1 <= mo <= 12):
            return m.group(0)
        word = hindi_date(d, mo, y)
        return f"{m.group(0)} ({word})"
    return _DATE_RE.sub(lambda m: m.group(0) if _already_expanded(text, m.start(), m.end()) else repl(m), text)


def _replace_contextual_numbers(text: str) -> str:
    occupied: list[tuple[int, int]] = []
    replacements: list[tuple[int, int, str]] = []

    def add_match(m: re.Match[str], raw: str):
        start, end = m.span(1) if m.lastindex else m.span()
        if (start > 0 and text[start - 1] in ".-/") or (end < len(text) and text[end:end + 1] in ".-/"):
            return
        if any(a < end and start < b for a, b in occupied):
            return
        if _already_expanded(text, start, end):
            return
        try:
            n = int(raw.replace(",", ""))
        except ValueError:
            return
        if n <= 0 or n > 99_99_99_999:
            return
        replacements.append((start, end, f"{_fmt_int(raw)} ({hindi_number(n)})"))
        occupied.append((start, end))

    for m in _MONEY_RE.finditer(text):
        add_match(m, m.group(1))
    for m in _UNIT_RE_C.finditer(text):
        add_match(m, m.group(1))

    for m in _KEYWORD_NUM_RE.finditer(text):
        num_start = m.start("num")
        num_end = m.end("num")
        if (num_start > 0 and text[num_start - 1] in ".-/") or (num_end < len(text) and text[num_end:num_end + 1] in ".-/"):
            continue
        if any(a < num_end and num_start < b for a, b in occupied):
            continue
        if _already_expanded(text, num_start, num_end):
            continue
        n = int(m.group("num"))
        if 0 < n <= 99_99_99_999:
            replacements.append((num_start, num_end, f"{_fmt_int(m.group('num'))} ({hindi_number(n)})"))
            occupied.append((num_start, num_end))

    age_re = re.compile(r"(?<![\w])([0-9]{1,3})(?=\s*(?:वर्ष|साल|years?|yrs?)(?!\w))", re.I)
    for m in age_re.finditer(text):
        add_match(m, m.group(1))

    for start, end, repl in sorted(replacements, reverse=True):
        text = text[:start] + repl + text[end:]
    return text


def format_legal_numbers(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    text = _clean_erroneous_expansions(text)
    text = _replace_dates(text)
    return _replace_contextual_numbers(text)


def format_draft_numbers(draft: dict) -> dict:
    def walk(value):
        if isinstance(value, str):
            return format_legal_numbers(value)
        if isinstance(value, list):
            return [walk(x) for x in value]
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        return value
    return walk(draft)
