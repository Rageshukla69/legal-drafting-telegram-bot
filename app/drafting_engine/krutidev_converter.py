```python
"""Deterministic KrutiDev-010 remnant & hybrid text cleaner for UP court Hindi drafts.

The 306 reference files in `legal_corpus/` and `corpus_index.json` were originally
typed in KrutiDev 010 / DevLys 010 and partially converted to Unicode, leaving
KrutiDev character codes (`h`, `w`, `s`, `k`, `q`, `a`, `Z`, `z`, `¼`, `½`, `&`,
`]`, `A`, `L`, `U`, `R`, `D`, `E`, `I`, `C`, `F`, `O`, `P`, `T`, `X`, `Y`, `v`,
`b`, `m`, `}`, `|`, etc.) embedded inside Hindi words.

This module deterministically converts any hybrid/remnant KrutiDev text into
clean, canonical Unicode Devanagari Hindi while preserving legitimate English
words, punctuation (`:`, `,`, `/`), case numbers, dates, phone numbers, and IDs.
It also normalizes UP court Hindi abbreviations (`स्व0` -> `स्व०`, `म0नं0` -> `म०नं०`,
`मो0नं0` -> `मो०नं०`, `मि0` -> `मि०`, `सं0` -> `सं०`) and fixes erroneous `बीस सौ ...`
year phrases to `दो हजार ...`.
"""
from __future__ import annotations

import re
from typing import Any

# Strict signal: only triggers when true KrutiDev-010 codes are attached to Devanagari
# or unmistakable KrutiDev tokens (`¼...½`, `श्रh`, `जwनियर`, `mपराsDत`, etc.) exist.
# Ordinary punctuation (`,`, `:`, `/`, `-`, `'`, `"`, `?`) never triggers this.
_HYBRID_SIGNAL_RE = re.compile(
    r"(?:[\u0900-\u097F][ksShqwzaZ`~¼½\]&}|{#\[UYRDCIEFOHPTXL])"
    r"|(?:[vbmÅ_ksShqwzaZ¼½&\[{}|#UYRDCIEFOHPTXL][\u0900-\u097F])"
    r"|(?:¼[^\n½]{1,60}½)"
    r"|(?:\b(?:vौर|काs|सs|नs|मs|हs|हाs|जाs|दks|,क|यहकि\]|श्रh|जwनियर|डिवhजन|बिधwना|mपराsDत)\b)"
)

# Matches ASCII '0' or degree sign '°' used as an abbreviation circle immediately after
# a Devanagari letter/matra (e.g. स्व0 -> स्व०, म0नं0 -> म०नं०, मो0नं0 -> मो०नं०, सं0 -> सं०, मि0 -> मि०)
_HINDI_ABBREV_ZERO_RE = re.compile(r"(?<=[\u0901-\u0939\u093C-\u094D\u0958-\u095F])[0°](?![0-9०-९])")


def normalize_hindi_abbreviations(text: str) -> str:
    """Replace ASCII '0' or '°' attached to Hindi abbreviations with '०' (\\u0966), fix 'बीस सौ' years, and clean spacing."""
    if not text:
        return ""
    s = _HINDI_ABBREV_ZERO_RE.sub("०", str(text))
    # Ensure a space between a number and an opening parenthesis containing Devanagari: e.g. '02(रामकान्ती)' -> '02 (रामकान्ती)'
    s = re.sub(r"(?<=[0-9०-९])\((?=[\u0900-\u097F])", " (", s)
    # Fix any 'बीस सौ बारह' / 'बीस सौ छब्बीस' year wording to 'दो हजार बारह' / 'दो हजार छब्बीस'
    s = re.sub(r"\bबीस\s+सौ(?=\s+[\u0900-\u097F])", "दो हजार", s)
    # Remove redundant word expansion in verification clause 'धारा 1 (एक) लगायत' -> 'धारा 1 लगायत'
    s = re.sub(r"(धारा\s+\d+)\s*\([^)]{1,30}\)(?=\s*लगायत)", r"\1", s)
    return s


def has_krutidev_remnants(text: str) -> bool:
    """Return True if `text` contains hybrid KrutiDev-010 encoding remnants."""
    if not text:
        return False
    return bool(_HYBRID_SIGNAL_RE.search(str(text)))


def decode_krutidev_hybrid(text: str, *, force: bool = False) -> str:
    """Convert hybrid KrutiDev-010 + Unicode text into clean Unicode Devanagari."""
    if not text:
        return ""
    s = str(text)
    if not force and not has_krutidev_remnants(s):
        return normalize_hindi_abbreviations(s)

    # Protect legitimate standalone ASCII words (3+ letters with no Devanagari in the token)
    protected: dict[str, str] = {}
    if not force:
        def _protect_eng(m: re.Match[str]) -> str:
            tok = m.group(0)
            if tok in {"vks", "vkS", "bZ"}:
                return tok
            key = f"\uE000{len(protected)}\uE001"
            protected[key] = tok
            return key

        s = re.sub(r"(?<![\u0900-\u097F])[A-Za-z]{3,}(?![\u0900-\u097F])", _protect_eng, s)

    # Handle quotation marks in KrutiDev before ' and " become श् and ष्:
    s = re.sub(r'(?<!\S)"(?=[\u0900-\u097F])(?![kksS])', "“", s)
    s = re.sub(r'(?<=[\u0900-\u097FqhwsSZa])"(?!\S|[kksS\u0900-\u097F])', "”", s)

    # Step 1: Handle 'ण' followed by 'k' (since '.' was converted to 'ण' without consuming 'k' in '.k')
    s = s.replace("णिk", "णि")
    s = s.replace("णkks", "णो")
    s = s.replace("णkkS", "णौ")
    s = s.replace("णkk", "णा")
    s = s.replace("णks", "णे")
    s = s.replace("णkS", "णै")
    s = s.replace("णk", "ण")

    # Step 2: '/' in KrutiDev is 'ध्' ONLY when followed by 'k', 'य', 'व', or preceded by 'ि' ('/िk')
    s = s.replace("/िk", "धि")
    s = s.replace("/kks", "धो")
    s = s.replace("/kkS", "धौ")
    s = s.replace("/kk", "धा")
    s = s.replace("/ks", "धे")
    s = s.replace("/kS", "धै")
    s = s.replace("/k", "ध")
    s = re.sub(r"/(?=[यव])", "ध्", s)

    # Step 3: Handle pre-base 'ि' (\u093F) and half-consonants + 'k' -> full consonant
    half_to_full = [
        ('"', "ष", "ष्"),
        ("'", "श", "श्"),
        ("’", "श", "श्"),
        ("“", "श", "श्"),
        ("”", "श", "श्"),
        ("‘", "श", "श्"),
        ("{", "क्ष", "क्ष्"),
        ("[", "ख", "ख्"),
        ("?", "घ", "घ्"),
        ("F", "थ", "थ्"),
        ("H", "भ", "भ्"),
        ("Y", "ल", "ल्"),
        ("U", "न", "न्"),
        ("R", "त", "त्"),
        ("L", "स", "स्"),
        ("C", "ब", "ब्"),
        ("D", "क", "क्"),
        ("E", "म", "म्"),
        ("I", "प", "प्"),
        ("O", "व", "व्"),
        ("P", "च", "च्"),
        ("T", "ज", "ज्"),
        ("X", "ग", "ग्"),
    ]
    for code, full_c, half_c in half_to_full:
        s = s.replace(code + "िk", full_c + "ि")
        s = s.replace(code + "kks", full_c + "ो")
        s = s.replace(code + "kkS", full_c + "ौ")
        s = s.replace(code + "kk", full_c + "ा")
        s = s.replace(code + "ks", full_c + "े")
        s = s.replace(code + "kS", full_c + "ै")
        s = s.replace(code + "k", full_c)
        s = s.replace(code, half_c)

    # Step 4: Handle special conjuncts & vowels in KrutiDev
    s = s.replace("Gकि", "यहकि")
    s = s.replace("vkS", "औ")
    s = s.replace("vks", "ओ")
    s = s.replace("vk", "आ")
    s = s.replace("vौ", "औ")
    s = s.replace("vो", "ओ")
    s = s.replace("vा", "आ")
    s = s.replace("v", "अ")

    s = s.replace("bZ", "ई")
    s = s.replace("b", "इ")

    s = s.replace("m", "उ")
    s = s.replace("Å", "ऊ")
    s = s.replace("_", "ऋ")

    # ',' in KrutiDev is 'ए' ONLY when attached to a Hindi word or ',s' (ऐ), never when followed by space!
    s = s.replace(",s", "ऐ")
    s = re.sub(r",(?=[\u0900-\u097FksShqwzaZ\]])", "ए", s)
    s = re.sub(r"(?<=[\u0900-\u097F]),(?=\s|$|[A।½\)])", "ए", s)

    s = s.replace("}", "द्व")
    s = s.replace("|", "द्य")

    # # -> रु, : -> रू (only in Hindi word contexts, never after a label colon like 'वादी: '), and ')' -> द्ध / द्भ
    s = s.replace("स)ावना", "सद्भावना")
    s = s.replace("स)ाव", "सद्भाव")
    s = s.replace("वि:)", "विरुद्ध")
    s = s.replace("वि#)", "विरुद्ध")
    s = s.replace("अनि#)", "अनिरुद्ध")
    s = s.replace("अनि:)", "अनिरुद्ध")
    s = re.sub(r"(?:व`|वृ)\)", "वृद्ध", s)
    s = re.sub(r"(?:बq|बु)\)", "बुद्ध", s)
    s = re.sub(r"(?:शq|शु)\)", "शुद्ध", s)
    s = re.sub(r"(?:यq|यु)\)", "युद्ध", s)
    s = re.sub(r"(?:कz|क्रq|क्रु)\)", "क्रुद्ध", s)
    s = re.sub(r"(?:उ|m)\)", "उद्ध", s)
    s = s.replace("ब)", "बद्ध")
    s = s.replace("#", "रु")
    # ':' is 'रू' when inside a Hindi word or at the start of a Hindi word (e.g. ':प' -> 'रूप', 'ज:र' -> 'जरूर')
    s = re.sub(r"(?<=[\u0900-\u097F]):(?=[\u0900-\u097FksShqwzaZ])", "रू", s)
    s = re.sub(r"(?<!\S):(?=[\u0900-\u097FksShqwzaZ])", "रू", s)
    s = re.sub(r"(?:शq|शु|अनq|अनु|ब|घ|गq|गु):(?=\s|$|[A।,])", lambda m: m.group(0)[:-1] + "रू", s)

    # z -> ्र (rakar), ª -> ्र (rakar under retroflex), ` -> ृ (ri matra), ~ -> ् (halant), + -> ़ (nukta)
    s = s.replace("z", "्र")
    s = s.replace("ª", "्र")
    s = s.replace("`", "ृ")
    s = s.replace("~", "्")
    s = re.sub(r"(?<=[\u0915-\u0939])\+", "़", s)
    s = re.sub(r"(?<=\d)\+|\+(?=\d|-)", "", s)

    # Matras:
    s = s.replace("ाks", "ो")
    s = s.replace("ाkS", "ौ")
    s = s.replace("ाk", "ा")
    s = s.replace("ाs", "ो")
    s = s.replace("ाS", "ौ")
    s = s.replace("ks", "ो")
    s = s.replace("kS", "ौ")
    s = s.replace("k", "ा")
    s = s.replace("s", "े")
    s = s.replace("S", "ै")
    s = s.replace("h", "ी")
    s = s.replace("q", "ु")
    s = s.replace("w", "ू")

    # Clean up double/inverted matras
    s = s.replace("श्िा", "शि")
    s = s.replace("ष्िा", "षि")
    s = s.replace("श्ौ", "शै")
    s = s.replace("ोे", "ो")
    s = s.replace("ौै", "ौ")
    s = s.replace("ौे", "ौ")
    s = s.replace("ाा", "ा")
    s = s.replace("ेा", "ो")

    # W and a (chandrabindu / anusvara)
    s = s.replace("aW", "ँ")
    s = s.replace("Wa", "ँ")
    s = s.replace("W", "ँ")
    s = s.replace("aa", "ं")
    s = s.replace("a", "ं")

    # Fix misplaced 'ि' (\u093F) that sits BEFORE a half-consonant cluster:
    s = re.sub(
        r"([\u0915-\u0939\u0958-\u095F])\u093F(\u094D[\u0915-\u0939\u0958-\u095F])",
        lambda m: m.group(1) + m.group(2) + "\u093F",
        s,
    )
    for _ in range(2):
        s = re.sub(
            r"((?:[\u0915-\u0939\u0958-\u095F]\u093C?\u094D)+)\u093F([\u0915-\u0939\u0958-\u095F]\u093C?(?:\u094D\u0930)?)",
            lambda m: m.group(1) + m.group(2) + "\u093F",
            s,
        )
    s = re.sub(r"\u093F(\u094D\u0930)", lambda m: m.group(1) + "\u093F", s)
    s = re.sub(r"([\u093E-\u094C])(\u094D\u0930)", lambda m: m.group(2) + m.group(1), s)
    s = re.sub(r"([\u0901\u0902])([\u093E-\u094C])", lambda m: m.group(2) + m.group(1), s)
    s = s.replace("ींे", "ीं").replace("हींे", "हीं").replace("ाें", "ों").replace("ेें", "ें")

    # Fix Repha 'Z' (\u0930\u094D before the syllable it follows!)
    s = re.sub(r"Z+", "Z", s)
    s = re.sub(
        r"((?:[\u0915-\u0939\u0958-\u095F]\u093C?\u094D)*[\u0915-\u0939\u0958-\u095F]\u093C?[\u093E-\u094C\u0962\u0963\u0901\u0902]*)Z",
        lambda m: "र्" + m.group(1),
        s,
    )
    s = s.replace("ईZ", "ई")
    s = s.replace("Z", "र्")

    # Punctuation:
    s = s.replace("¼", "(")
    s = s.replace("½", ")")
    s = s.replace("]", ",")
    s = s.replace("&", "-")
    s = re.sub(r"(?<=[\u0900-\u097F])%", "ः", s)
    s = s.replace("%", ":")
    s = re.sub(r"(?m)^(\s*)A(?=-\s*\()", r"\g<1>1", s)
    s = s.replace("A", "।")
    s = s.replace("\\", "।")
    s = s.replace("@", "/")
    s = s.replace("^", '"')
    s = s.replace("$", "×")
    s = s.replace("-*", "-")

    # Restore any protected English tokens
    for key, val in protected.items():
        s = s.replace(key, val)

    return normalize_hindi_abbreviations(s)


def sanitize_draft_unicode(draft: Any) -> Any:
    """Recursively clean any residual KrutiDev artifacts and normalize Hindi abbreviations inside a structured draft."""
    if isinstance(draft, str):
        return decode_krutidev_hybrid(draft, force=False)
    if isinstance(draft, list):
        return [sanitize_draft_unicode(x) for x in draft]
    if isinstance(draft, dict):
        return {k: sanitize_draft_unicode(v) for k, v in draft.items()}
    return draft
```
