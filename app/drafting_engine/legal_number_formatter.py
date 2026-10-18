"""Deterministic Indian legal number formatting (Digits + Hindi Words in Parentheses)."""
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

WORD_TO_INT = {w: i for i, w in enumerate(ONES) if i > 0}
WORD_TO_INT["पांच"] = 5

HINDI_MONTH_MAP = {
    "जनवरी": 1,
    "फरवरी": 2,
    "फ़रवरी": 2,
    "मार्च": 3,
    "अप्रैल": 4,
    "मई": 5,
    "जून": 6,
    "जुलाई": 7,
    "अगस्त": 8,
    "सितंबर": 9,
    "सितम्बर": 9,
    "अक्टूबर": 10,
    "अक्तूबर": 10,
    "नवंबर": 11,
    "नवम्बर": 11,
    "दिसंबर": 12,
    "दिसम्बर": 12,
}


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


def hindi_decimal(raw: str) -> str:
    """Convert decimal string like '0.250' to 'शून्य दशमलव दो पाँच शून्य'."""
    if "." not in raw:
        return hindi_number(int(raw))
    whole_str, frac_str = raw.split(".", 1)
    whole_word = hindi_number(int(whole_str)) if whole_str.strip() else "शून्य"
    frac_words = " ".join(ONES[int(ch)] for ch in frac_str if ch.isdigit())
    return f"{whole_word} दशमलव {frac_words}".strip()


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


_UNIT_RE = (
    r"वर्ष|साल|वर्षों|सालों|माह|महीने|दिवस|दिन|घंटे|मिनट|"
    r"डिसमिल|डेसिमल|बीघा|बिस्वा|बिस्वांसी|एकड़|एकड़|हेक्टेयर|हेयर|हे०|हे\.|"
    r"मीटर|फीट|फुट|गज|वर्गफुट|वर्ग फीट|वर्गगज|वर्ग गज|वर्गमीटर|वर्ग मीटर|"
    r"किलो|किलोग्राम|ग्राम|लीटर|प्रतिशत|"
    r"रुपये|रुपए|रुपया|रु०|रु\.?|Rs\.?|₹"
)

_KEYWORD_RE = (
    r"(?:भूमि\s+गाटा|गाटा|खसरा|सर्वे|प्लॉट|प्लाट|भूखण्ड|भूखंड|खतौनी|खाता|चक|भूमि|आराजी|"
    r"मकान|भवन|दुकान|कमरा|किता|मूल\s+वाद|प्रकीर्ण\s+वाद|वाद|मुकदमा|अपील|कैवियट|"
    r"प्रतिवादी|वादी|विपक्षी|साक्षी|गवाह)"
    r"(?:\s*(?:गाटा|खसरा|सर्वे))?"
    r"(?:\s*(?:संख्या|सं०|सं\.|सं-|क्रमांक|नंबर|नं०|नं\.|नं-))?|"
    r"कुल\s+रकबा|कुल\s+रकवा|रकबा|रकवा|क्षेत्रफल|कुल|मात्र|लगभग|करीब|आयु|उम्र|वय|सन्"
)

_DATE_RE = re.compile(r"(?<!\w)(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?!\w)")
_HINDI_MONTH_NAMES = "|".join(sorted(HINDI_MONTH_MAP.keys(), key=len, reverse=True))
_HINDI_TEXT_DATE_RE = re.compile(
    rf"(?<!\w)(\d{{1,2}})\s+({_HINDI_MONTH_NAMES})(?:[,\s]+(?:सन्\s+)?)(\d{{4}})(?!\w)"
)
_TIME_COLON_RE = re.compile(r"(?<!\w)(\d{1,2})[:.](\d{2})(?=\s*बजे(?!\w))")
_TIME_INT_RE = re.compile(r"(?<![\w:.])(\d{1,2})(?=\s*बजे(?!\w))")
_DECIMAL_UNIT_RE = re.compile(
    rf"(?<![\w.])(\d+\.\d+)(?=\s*(?:{_UNIT_RE})(?!\w))", re.I
)
_DECIMAL_PREFIX_RE = re.compile(
    r"(?P<prefix>(?:कुल\s+रकबा|कुल\s+रकवा|रकबा|रकवा|क्षेत्रफल)\s*)(?P<num>\d+\.\d+)(?!\d)",
    re.I,
)
_FRACTION_RE = re.compile(
    r"(?<![\w/.-])(\d{1,2})/(\d{1,2})(?=\s*(?:हिस्सा|हिस्से|भाग|अंश)(?!\w))"
)
_MONEY_RE = re.compile(
    r"(?<![\w.])([0-9]{1,3}(?:,[0-9]{2,3})*|[0-9]+)(?=\s*(?:रुपये|रुपए|रुपया|रु०|रु\.?|Rs\.?|₹))",
    re.I,
)
_MONEY_PREFIX_RE = re.compile(
    r"(?P<prefix>(?:रु०|रु\.|रुपये|रुपए|रुपया|मुबलिग|₹|Rs\.?)\s*)(?P<num>[0-9]{1,3}(?:,[0-9]{2,3})*|[0-9]+)(?P<slash>/-)?",
    re.I,
)
_UNIT_RE_C = re.compile(rf"(?<![\w.:])([0-9]{{1,9}})(?=\s*(?:{_UNIT_RE})(?!\w))", re.I)
_KEYWORD_NUM_RE = re.compile(
    rf"(?P<prefix>(?:{_KEYWORD_RE})\s*[-:]*\s*)(?P<num>[0-9]{{1,9}})(?![0-9:])",
    re.I,
)
_LEADING_PARTY_ITEM_RE = re.compile(r"^(\s*0*)(\d{1,2})(?=\s*[.)\-:]\s*[\u0900-\u097F])")

_PARA_CHAIN_RE = re.compile(
    r"(?P<head>(?:पैराग्राफ|पैरा|चरण|मद|प्रतिवादीगण|प्रतिवादी|वादीगण|वादी|विपक्षीगण|विपक्षी|"
    r"(?:वाद\s*पत्र|वादपत्र|प्रतिवाद\s*पत्र|प्रतिवादपत्र|शपथ\s*पत्र|शपथपत्र|जवाब\s*दावा|लिखित\s*कथन|उत्तर\s*पत्र|उत्तरपत्र)\s*(?:की|के)\s*धारा)"
    r"(?:\s*(?:संख्या|सं०|सं\.|सं-|नं०|नं\.|नं-|क्रमांक))?\s*)"
    r"(?P<chain>\d{1,3}(?:\s*\([^)]{1,30}\))?(?:\s*(?:से|लगायत|तथा|एवं|व|और|,|तक)\s*(?:(?:पैराग्राफ|पैरा|धारा)(?:\s*(?:संख्या|सं०|सं\.|नं०|नं\.))?\s*)?\d{1,3}(?:\s*\([^)]{1,30}\))?)*)"
)

_BARE_WORD_UNIT_RE = re.compile(
    r"(?<![\w(])(" + "|".join(["एक", "दो", "तीन", "चार", "पाँच", "पांच", "छह", "सात", "आठ", "नौ", "दस"]) + r")(?=\s+(?:बिस्वा|बीघा|एकड़|एकड़|हेक्टेयर|डिसमिल|डेसिमल|फीट|फुट|गज|वर्गफुट)(?!\w))"
)


def _already_expanded(text: str, start: int, end: int) -> bool:
    tail = text[end:end + 90]
    head = text[max(0, start - 20):start]
    return bool(re.match(r"\s*(?:/-)?\s*\([^)]{1,100}\)", tail)) or bool(re.search(r"\([^)]{1,100}\)\s*$", head))


def _is_part_of_comma_number(text: str, start: int, end: int) -> bool:
    """True when the matched digit span is adjacent to `,` + digit or digit + `,`."""
    if start >= 2 and text[start - 1] == "," and text[start - 2].isdigit():
        return True
    if end + 1 < len(text) and text[end] == "," and text[end + 1].isdigit():
        return True
    return False


def _fmt_int(raw: str) -> str:
    try:
        val = int(raw.replace(",", ""))
        if val >= 100000 and "," in raw:
            return raw
        return f"{val:,}" if "," in raw or val >= 1000 else raw
    except ValueError:
        return raw


def _clean_erroneous_expansions(text: str) -> str:
    return re.sub(r"\bबीस\s+सौ(?=\s+[\u0900-\u097F])", "दो हजार", text)


def _replace_bare_word_units(text: str) -> str:
    """Convert bare Hindi number words before land units (e.g. 'दो बिस्वा' -> '2 (दो) बिस्वा')."""
    def repl(m: re.Match[str]) -> str:
        word = m.group(1)
        num = WORD_TO_INT.get(word)
        if not num:
            return m.group(0)
        canon = ONES[num]
        return f"{num} ({canon})"
    return _BARE_WORD_UNIT_RE.sub(repl, text)


def _replace_dates(text: str) -> str:
    def repl_numeric(m: re.Match[str]) -> str:
        d, mo, y = map(int, m.groups())
        if not (1 <= d <= 31 and 1 <= mo <= 12):
            return m.group(0)
        word = hindi_date(d, mo, y)
        return f"{m.group(0)} ({word})"

    text = _DATE_RE.sub(
        lambda m: m.group(0) if _already_expanded(text, m.start(), m.end()) else repl_numeric(m),
        text,
    )

    def repl_hindi_month(m: re.Match[str]) -> str:
        if _already_expanded(text, m.start(), m.end()):
            return m.group(0)
        d = int(m.group(1))
        mo_name = m.group(2)
        y = int(m.group(3))
        mo = HINDI_MONTH_MAP.get(mo_name, 0)
        if not (1 <= d <= 31 and 1 <= mo <= 12):
            return m.group(0)
        word = f"{hindi_number(d)} {mo_name} {hindi_year(y)}"
        return f"{m.group(0)} ({word})"

    return _HINDI_TEXT_DATE_RE.sub(repl_hindi_month, text)


def _replace_times(text: str) -> str:
    def repl_colon(m: re.Match[str]) -> str:
        if _already_expanded(text, m.start(), m.end()):
            return m.group(0)
        hr = int(m.group(1))
        mn = int(m.group(2))
        if hr > 24 or mn > 59:
            return m.group(0)
        if mn == 0:
            return f"{m.group(0)} ({hindi_number(hr)})"
        return f"{m.group(0)} ({hindi_number(hr)} बजकर {hindi_number(mn)} मिनट)"

    text = _TIME_COLON_RE.sub(repl_colon, text)

    def repl_int(m: re.Match[str]) -> str:
        if _already_expanded(text, m.start(), m.end()):
            return m.group(0)
        hr = int(m.group(1))
        if hr > 24:
            return m.group(0)
        return f"{m.group(0)} ({hindi_number(hr)})"

    return _TIME_INT_RE.sub(repl_int, text)


def _replace_decimals_and_fractions(text: str) -> str:
    def repl_dec(m: re.Match[str]) -> str:
        if _already_expanded(text, m.start(), m.end()):
            return m.group(0)
        raw = m.group(1)
        return f"{raw} ({hindi_decimal(raw)})"

    text = _DECIMAL_UNIT_RE.sub(repl_dec, text)

    def repl_dec_prefix(m: re.Match[str]) -> str:
        if _already_expanded(text, m.start("num"), m.end("num")):
            return m.group(0)
        raw = m.group("num")
        return f"{m.group('prefix')}{raw} ({hindi_decimal(raw)})"

    text = _DECIMAL_PREFIX_RE.sub(repl_dec_prefix, text)

    def repl_frac(m: re.Match[str]) -> str:
        if _already_expanded(text, m.start(), m.end()):
            return m.group(0)
        num = int(m.group(1))
        den = int(m.group(2))
        if num <= 0 or den <= 0:
            return m.group(0)
        return f"{m.group(0)} ({hindi_number(num)} बटा {hindi_number(den)})"

    return _FRACTION_RE.sub(repl_frac, text)


def _replace_para_chains(text: str) -> str:
    num_token_re = re.compile(r"(?<!\d)(\d{1,3})(?!\d)(\s*\([^)]{1,30}\))?")

    def repl_chain(m: re.Match[str]) -> str:
        head = m.group("head")
        chain = m.group("chain")

        def exp_num(nm: re.Match[str]) -> str:
            if nm.group(2):
                return nm.group(0)
            val = int(nm.group(1))
            if 0 < val <= 999:
                return f"{nm.group(1)} ({hindi_number(val)})"
            return nm.group(0)

        return head + num_token_re.sub(exp_num, chain)

    return _PARA_CHAIN_RE.sub(repl_chain, text)


def _replace_contextual_numbers(text: str) -> str:
    occupied: list[tuple[int, int]] = []
    replacements: list[tuple[int, int, str]] = []

    def add_match(m: re.Match[str], raw: str, span_override: tuple[int, int] | None = None, suffix: str = ""):
        start, end = span_override if span_override else (m.span(1) if m.lastindex else m.span())
        if (start > 0 and text[start - 1] in ".-/:") or (end < len(text) and text[end:end + 1] in ".-/:" and not suffix):
            return
        if _is_part_of_comma_number(text, start, end):
            return
        if any(a < end and start < b for a, b in occupied):
            return
        if _already_expanded(text, start, end):
            return
        clean_digits = raw.replace(",", "")
        if len(clean_digits) >= 10:
            return
        prefix_ctx = text[max(0, start - 15):start].casefold()
        if len(clean_digits) == 6 and ("pin" in prefix_ctx or "पिन" in prefix_ctx):
            return
        try:
            n = int(clean_digits)
        except ValueError:
            return
        if n <= 0 or n > 99_99_99_999:
            return
        replacements.append((start, end, f"{_fmt_int(raw)}{suffix} ({hindi_number(n)})"))
        occupied.append((start, end))

    for m in _MONEY_RE.finditer(text):
        add_match(m, m.group(1))

    for m in _MONEY_PREFIX_RE.finditer(text):
        slash = m.group("slash") or ""
        span = (m.start("num"), m.end("slash") if slash else m.end("num"))
        add_match(m, m.group("num"), span_override=span, suffix=slash)

    for m in _UNIT_RE_C.finditer(text):
        add_match(m, m.group(1))

    for m in _KEYWORD_NUM_RE.finditer(text):
        num_start = m.start("num")
        num_end = m.end("num")
        if (num_start > 0 and text[num_start - 1] in "./:") or (num_end < len(text) and text[num_end:num_end + 1] in ".-/:"):
            continue
        if _is_part_of_comma_number(text, num_start, num_end):
            continue
        if any(a < num_end and num_start < b for a, b in occupied):
            continue
        if _already_expanded(text, num_start, num_end):
            continue
        raw_num = m.group("num")
        if len(raw_num) >= 10:
            continue
        n = int(raw_num)
        if 0 < n <= 99_99_99_999:
            is_year = "सन्" in m.group("prefix") and 1000 <= n <= 2999
            word = hindi_year(n) if is_year else hindi_number(n)
            disp = raw_num if is_year else _fmt_int(raw_num)
            replacements.append((num_start, num_end, f"{disp} ({word})"))
            occupied.append((num_start, num_end))

    age_re = re.compile(r"(?<![\w.:])([0-9]{1,3})(?=\s*(?:वर्ष|साल|years?|yrs?)(?!\w))", re.I)
    for m in age_re.finditer(text):
        add_match(m, m.group(1))

    for start, end, repl in sorted(replacements, reverse=True):
        text = text[:start] + repl + text[end:]

    m_lead = _LEADING_PARTY_ITEM_RE.match(text)
    if m_lead and not _already_expanded(text, m_lead.start(2), m_lead.end(2)):
        n_lead = int(m_lead.group(2))
        if 0 < n_lead < len(ONES):
            text = f"{m_lead.group(1)}{m_lead.group(2)} ({ONES[n_lead]})" + text[m_lead.end(2):]

    return text


def format_legal_numbers(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    text = _clean_erroneous_expansions(text)
    text = _replace_bare_word_units(text)
    text = _replace_dates(text)
    text = _replace_times(text)
    text = _replace_decimals_and_fractions(text)
    text = _replace_para_chains(text)
    return _replace_contextual_numbers(text)


def format_draft_numbers(draft: dict) -> dict:
    def walk(value, key_name: str = ""):
        if isinstance(value, str):
            return format_legal_numbers(value)
        if isinstance(value, list):
            return [walk(x, key_name) for x in value]
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        return value
    return walk(draft)
