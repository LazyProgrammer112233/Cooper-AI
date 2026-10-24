"""Hindi and Hinglish questions -> the English Cooper's parser reads.

    normalize("इस हफ्ते मेरे प्रोडक्टिव कॉल्स कम क्यों हैं")  -> ("this week my productive calls low why", "hi")
    normalize("pichle mahine ki primary sale dikhao")         -> ("last month primary sales show", "hinglish")
    normalize("Show primary sales for MTD")                   -> ("Show primary sales for MTD", "en")

Words come from backend/data/hindi_lexicon.json and speech mishearings from speech_fixes.json, both
editable. Proper nouns are kept; Devanagari place names become their English spelling, and any other
Devanagari word is transliterated so names can still be matched against the company's masters.
English questions pass through unchanged apart from the speech fixes.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from . import config

DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_B = r"(?<![\wऀ-ॿ])"  # word boundary that also works for Devanagari (matras are not \w)
_E = r"(?![\wऀ-ॿ])"

# Roman words that only appear in Hinglish, never in English questions. Two of them (or one strong
# one) make Cooper read the question as Hinglish.
_STRONG = {"kyu", "kyun", "kyon", "kyoon", "dikhao", "batao", "bataiye", "dikhaiye", "kitne", "kitna", "kitni", "sabse",
           "pichle", "pichhle", "hafte", "mahine", "kaunsa", "konsa", "kaun", "chahiye", "nikalo", "bikri", "girawat",
           "giravat", "dukaan", "vitrak", "kaise", "mein", "hain", "galat", "dikkat", "aaj", "zyada", "jyada", "kya",
           "kal", "tak", "saal", "sunao", "gaana", "karo", "saare", "sabhi", "naye", "wale", "sudhare", "badhaye",
           "kyunki", "hafta", "mahina", "bikri", "tulna", "kaisa"}
_WEAK = {"hai", "ka", "ki", "ke", "kam", "wala", "wali", "raha", "rahe", "rahi", "gaya", "gayi", "mera", "meri",
         "mere", "hamare", "hamari", "tak", "se", "aur", "din", "saal", "bhi", "toh", "ko", "par", "ho", "tha", "thi"}


def _norm_dev(s: str) -> str:
    """Ignore nukta and chandrabindu so ज़्यादा == ज्यादा and पाँच == पांच."""
    return s.replace("़", "").replace("ँ", "ं")


@lru_cache(maxsize=1)
def lexicon() -> dict:
    data = json.loads((config.DATA_DIR / "hindi_lexicon.json").read_text(encoding="utf-8"))
    for key in ("phrases", "words", "roman_only", "places"):
        data[key] = {_norm_dev(k.lower()): v for k, v in data[key].items()}
    return data


@lru_cache(maxsize=1)
def speech_fixes() -> list[tuple[re.Pattern, str]]:
    raw = json.loads((config.DATA_DIR / "speech_fixes.json").read_text(encoding="utf-8"))["fixes"]
    return [(re.compile(p, re.I), re.sub(r"\$(\d)", r"\\\1", r)) for p, r in raw]


def fix_speech(text: str) -> str:
    for rx, rep in speech_fixes():
        text = rx.sub(rep, text)
    return text


def detect(text: str) -> str:
    """'hi' (Devanagari), 'hinglish' (Roman Hindi) or 'en'."""
    if DEVANAGARI.search(text):
        return "hi"
    words = re.findall(r"[a-z]+", text.lower())
    strong = sum(w in _STRONG for w in words)
    weak = sum(w in _WEAK for w in words)
    if strong >= 1 or weak >= 3:
        return "hinglish"
    return "en"


@lru_cache(maxsize=2)
def _compiled(roman: bool) -> list[tuple[re.Pattern, str]]:
    lx = lexicon()
    entries: dict[str, str] = {}
    for key in ("places", "phrases", "words") + (("roman_only",) if roman else ()):
        for k, v in lx[key].items():
            if roman or DEVANAGARI.search(k):
                entries.setdefault(k, v)
    out = []
    for k in sorted(entries, key=len, reverse=True):
        out.append((re.compile(_B + re.escape(k) + _E, re.I), entries[k]))
    return out


# Simple Devanagari -> Latin transliteration for names the lexicon doesn't know ("रमेश" -> "ramesh").
_VOWELS = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
           "ऑ": "o"}
_MATRAS = {"ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o",
           "ं": "n", "ः": "h"}
_CONS = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
         "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n", "प": "p",
         "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s",
         "ह": "h", "क्ष": "ksh", "त्र": "tr", "ज्ञ": "gy"}
_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def transliterate(word: str) -> str:
    out, i, w = [], 0, word
    while i < len(w):
        ch = w[i]
        if ch in _CONS:
            out.append(_CONS[ch])
            nxt = w[i + 1] if i + 1 < len(w) else ""
            if nxt == "्":
                i += 2
                continue
            if nxt in _MATRAS:
                out.append(_MATRAS[nxt])
                i += 2
                continue
            if i + 1 < len(w):  # inherent 'a', dropped at the end of the word
                out.append("a")
        elif ch in _VOWELS:
            out.append(_VOWELS[ch])
        elif ch in _MATRAS:
            out.append(_MATRAS[ch])
        elif ch != "्":
            out.append(ch)
        i += 1
    return "".join(out)


STOP = {"show", "tell", "what", "why", "which", "how", "much", "many", "low", "high", "lowest", "highest", "best", "worst",
        "top", "bottom", "this", "last", "week", "month", "year", "today", "yesterday", "my", "our", "sales", "primary",
        "secondary", "productive", "calls", "total", "coverage", "penetration", "orders", "order", "value", "and", "or",
        "vs", "compare", "trend", "for", "in", "by", "of", "on", "from", "to", "the", "a", "an", "is", "are", "need",
        "dropped", "declined", "increased", "decline", "growth", "improve", "wrong", "doing", "days", "till", "date",
        "pc", "tc", "ubo", "mtd", "lmtd", "ytd", "qtd", "lpc", "count", "number", "list", "all", "per", "where", "when"}
_DIMS = {"zone", "zones", "region", "regions", "territory", "state", "states", "city", "category", "brand", "distributor",
         "sku", "outlet"}
GEN = "§"  # marks ka / ki / ke until the word order is fixed


def _reorder_postpositions(text: str) -> str:
    """Hindi puts the place before its postposition and before the rest of the question:
    'Maharashtra in how many distributor' -> 'how many distributor in Maharashtra',
    'Pure category § sale this month' -> 'sale this month for Pure category'."""
    toks = text.split()
    out: list[str] = []
    tail: list[str] = []
    for tok in toks:
        low = tok.lower()
        if low in ("in", "for") and out:
            j = len(out)
            while j > 0 and len(out) - j < 3 and out[j - 1].lower() not in STOP:
                j -= 1
            if j < len(out):
                tail += [low] + out[j:]
                del out[j:]
                continue
        if tok == GEN:
            if out and out[-1].lower() in _DIMS:
                j = len(out) - 1
                while j > 0 and len(out) - 1 - j < 2 and out[j - 1].lower() not in STOP | _DIMS:
                    j -= 1
                if j < len(out) - 1:
                    tail += ["for"] + out[j:]
                    del out[j:]
            continue
        out.append(tok)
    return " ".join(out + tail)


def normalize(question: str) -> tuple[str, str]:
    """(English text for the parser, detected language)."""
    text = fix_speech(question.strip())
    lang = detect(text)
    if lang == "en":
        return text, lang
    t = _norm_dev(text.translate(_DIGITS))
    t = re.sub(r"[।?!,]+", " ", t)
    for pat, rep in lexicon().get("patterns", []):
        if lang == "hi" or not DEVANAGARI.search(pat):
            t = re.sub(pat, rep, t, flags=re.I)
    latin = re.sub(r"[\u0900-\u097F]+", " ", t)
    roman = lang == "hinglish" or detect(latin) == "hinglish"  # "North ज़ोन mein sales" mixes both
    for rx, rep in _compiled(roman):
        t = rx.sub(rep, t)
    t = re.sub(r"[\u0900-\u097F]+", lambda m: transliterate(m.group(0)), t)
    t = re.sub(r"\s+", " ", t).strip()
    t = _reorder_postpositions(t).replace(GEN, " ")
    t = re.sub(r"\s+", " ", t).strip()
    t = fix_speech(t)
    return t or question, lang


def is_hindi(lang: str) -> bool:
    return lang in ("hi", "hinglish")


# --------------------------------------------------------------------------- one Hindi line per answer
# Built from the same template numbers as the English summary: the local model never writes these.

_REASONS = {
    "holiday": ("chhutti aur kam working days", "छुट्टी और कम कार्य दिवस"),
    "fewer_reps": ("market mein kam reps", "मार्केट में कम रिप्स"),
    "leave_up": ("zyada leave", "ज़्यादा छुट्टियाँ (लीव)"),
    "fewer_calls": ("har rep ke kam calls", "हर रिप के कम कॉल"),
    "low_pc_pct": ("kam calls order mein badle (PC %)", "कम कॉल ऑर्डर में बदले (PC %)"),
    "late_start": ("reps der se market pahunche", "रिप्स देर से मार्केट पहुँचे"),
    "zero_pc": ("kai din bina order ke", "कई दिन बिना ऑर्डर के"),
    "low_drop_size": ("chhote orders", "छोटे ऑर्डर"),
    "low_lpc": ("har call mein kam lines", "हर कॉल में कम लाइनें"),
    "ubo_down": ("kam outlets bill hue", "कम आउटलेट बिल हुए"),
    "distributors_not_billing": ("kam distributors ne billing ki", "कम डिस्ट्रीब्यूटर्स ने बिलिंग की"),
    "low_invoice_value": ("chhote primary invoices", "छोटे प्राइमरी इनवॉइस"),
    "low_coverage": ("plan ke kam outlets visit hue", "प्लान के कम आउटलेट विज़िट हुए"),
    "tour_plan": ("tour plan follow nahi hua", "टूर प्लान फॉलो नहीं हुआ"),
    "region_drop": ("kuch hi areas mein giravat", "कुछ ही इलाकों में गिरावट"),
    "season": ("season ka asar", "सीज़न का असर"),
    "sustain": ("team ka accha kaam", "टीम का अच्छा काम"),
}


def summary_line(answer: dict, lang: str, company: str) -> str | None:
    deva = lang == "hi"
    kind = answer.get("kind")

    def say(roman: str, dev: str) -> str:
        return dev if deva else roman

    if kind == "refusal":
        return say(f"Main sirf {company} ke Pulse data se jude FMCG sawaalon ka jawab deta hoon.",
                   f"मैं सिर्फ़ {company} के Pulse डेटा से जुड़े FMCG सवालों का जवाब देता हूँ।")
    if kind == "error":
        return say("Data abhi nahi mil paaya, thodi der mein phir koshish kijiye.",
                   "डेटा अभी नहीं मिल पाया, थोड़ी देर में फिर कोशिश कीजिए।")
    if kind == "clarify":
        return say("Thoda aur bataiye, jaise kaunsa metric, period ya zone.",
                   "थोड़ा और बताइए, जैसे कौन सा मेट्रिक, अवधि या ज़ोन।")
    if kind == "empty":
        return say("Is period ke liye koi data nahi mila.", "इस अवधि के लिए कोई डेटा नहीं मिला।")
    if kind == "rca" and answer.get("rca"):
        from . import fmt
        r = answer["rca"]
        unit = r["unit"]
        cur, prev = fmt.value(r["current"]["value"], unit), fmt.value(r["previous"]["value"], unit)
        m = re.match(r"(up|down) ([\d.]+)( pts|%)", r.get("change") or "")
        if m and r.get("direction") != "flat":
            amt = m.group(2) + ("%" if m.group(3) == "%" else " points")
            move = say(f"{amt} {'badha' if m.group(1) == 'up' else 'gira'}",
                       f"{amt} {'बढ़ा' if m.group(1) == 'up' else 'गिरा'}")
        else:
            move = say("lagbhag utna hi raha", "लगभग उतना ही रहा")
        line = say(f"{r['metric_label']}: abhi {cur}, pichhle period mein {prev}, yaani {move}.",
                   f"{r['metric_label']}: अभी {cur}, पिछली अवधि में {prev}, यानी {move}।")
        if r.get("why") and r.get("direction") != "flat":
            why = _REASONS.get(r["why"][0]["id"])
            if why:
                line += say(f" Mukhya wajah: {why[0]}.", f" मुख्य वजह: {why[1]}।")
        return line
    if kind != "data" or not answer.get("chart"):
        return None
    from . import fmt
    chart = answer["chart"]
    period = (answer.get("how") or {}).get("period", "")
    rows = chart.get("data") or []
    series = chart.get("series") or []
    if chart["type"] == "kpi" and rows and series:
        s0 = series[0]
        value = fmt.value(rows[0].get(s0["id"]), s0["unit"]) if s0["id"] != "count" else answer.get("headline")
        return say(f"{period}: {s0['label']} {value}.", f"{period}: {s0['label']} {value}।")
    if not series or not rows:
        return None
    s0 = series[0]
    head = answer.get("headline")
    summary = answer.get("summary", "")
    if re.search(r"moved from|The total for the period", summary) and head:
        return say(f"{period} mein {s0['label']} ka total {head} raha.", f"{period} में {s0['label']} का कुल {head} रहा।")
    top = rows[0]
    val = fmt.value(top.get(s0["id"]), s0["unit"])
    if " vs " in summary and len(rows) >= 2:
        other = fmt.value(rows[1].get(s0["id"]), s0["unit"])
        return say(f"{s0['label']}: {top['label']} {val}, {rows[1]['label']} {other}.",
                   f"{s0['label']}: {top['label']} {val}, {rows[1]['label']} {other}।")
    low = "lowest" in summary
    return say(f"Sabse {'kam' if low else 'zyada'} {s0['label']}: {top['label']} ({val}).",
               f"सबसे {'कम' if low else 'ज़्यादा'} {s0['label']}: {top['label']} ({val})।")
