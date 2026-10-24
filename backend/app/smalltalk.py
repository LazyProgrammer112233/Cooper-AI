"""Greetings and small talk get a warm reply, never the policy refusal.

    analyse("hi Cooper")                                  -> pure greeting: reply, no data queries
    analyse("kaise ho Cooper, aaj PC kam aa raha hai")    -> warm line + the question "aaj PC kam aa raha hai"
    analyse("aaj mujhe bahut dikkat hai")                 -> kind reply asking what's wrong, with chips
    analyse("who is the prime minister")                  -> not small talk: the normal gate refuses it

Runs before the question gate. Anything left after the greeting is stripped still goes through every
guardrail, so "hi, show me company 10601" is refused as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .hindi import DEVANAGARI, detect

# (category, pattern). Matched on lower-cased text with Devanagari nukta removed.
_PATTERNS: list[tuple[str, str]] = [
    ("how", r"how are you( doing)?|how r u|how are u|how's it going|how is it going|how do you do|what'?s up|wassup|sup\b|"
            r"kaise ho|kaise hain|kaise hai|kaisa hai|kaisi ho|kaise ho aap|aap kaise (ho|hain)|kya haal( chaal)? hai|kya haal|"
            r"kya hal hai|sab (theek|thik|badhiya|badiya)( hai)?|कैसे हो|कैसे हैं|कैसा है|क्या हाल है|क्या हाल|सब ठीक"),
    ("greet", r"hi+|hello+|hey+|hiya|yo|hola|good (morning|afternoon|evening|day)|gm|namaste|namaskar|namaskaar|"
              r"ram ram|jai shri (ram|krishna)|pranam|pranaam|sat sri akal|salaam|salam|adaab|vanakkam|"
              r"नमस्ते|नमस्कार|राम राम|प्रणाम|जय श्री राम"),
    ("thanks", r"thank you( so much| very much| a lot)?|thanks?( a lot| so much)?|thx|ty|shukriya|dhanyavaad|dhanyawad|dhanyavad|"
               r"great job|well done|awesome|nice work|good job|perfect|बढ़िया|शुक्रिया|धन्यवाद"),
    ("bye", r"bye+|goodbye|good night|see you|see ya|take care|alvida|phir milte hain|chalo bye|अलविदा|फिर मिलते हैं"),
    ("who", r"who are you|what are you|what is your name|what'?s your name|your name|aap kaun (ho|hain)|tum kaun ho|"
            r"tumhara naam|aapka naam|आप कौन हो|आप कौन हैं|तुम कौन हो|तुम्हारा नाम"),
    ("can", r"what can you do|what do you do|how can you help|help me|help|tum kya kar sakte ho|aap kya kar sakte (ho|hain)|"
            r"kya kar sakte ho|kya madad|madad karo|तुम क्या कर सकते हो|आप क्या कर सकते हैं|मदद"),
    ("feel", r"i'?m (so |very |really )?(tired|exhausted|bored|stressed|busy|sad|happy|good|fine|great)|"
             r"i am (so |very |really )?(tired|exhausted|bored|stressed|busy|sad|happy|good|fine|great)|feeling (tired|low|good)|"
             r"(bahut|bohot) kaam hai|thak gaya|thak gayi|thaka hua|mood (accha|acha|achha|kharab|off) hai|"
             r"tension (hai|ho rahi)|pareshan hoon|bore ho raha|बहुत काम है|थक गया|थक गई|मूड (अच्छा|खराब) है"),
    ("vague", r"(aaj )?(mujhe )?(bahut |bohot )?(dikkat|problem|pareshani|samasya|issue) (hai|ho rahi hai|aa rahi hai)|"
              r"i have a problem|i'?ve got a problem|something is wrong|kuch gadbad hai|kuch problem hai|"
              r"(मुझे )?(बहुत )?(दिक्कत|समस्या|परेशानी) है"),
]
# "aaj mujhe ye dikkat hai ki ...", "my problem is that ...": a problem statement in front of a question
_PROBLEM_LEAD = (r"(aaj )?(mujhe |meri |hamari |humari )?(ye |yeh |ek )?(badi |bahut )?(dikkat|problem|samasya|issue|pareshani|"
                 r"tension) (ye )?(hai|h|ho rahi hai|aa rahi hai) (ki|ke)|my (problem|issue|worry) is( that)?|"
                 r"the (problem|issue) is( that)?|i'?m worried (that|about)|(आज )?(मुझे )?(ये |यह )?(दिक्कत|समस्या|परेशानी) "
                 r"(है|हो रही है) कि")
_LEADS = (r"i want to know|mujhe jaanna hai( ki)?|actually|basically|so|and|ok|okay|achha|acha|haan|ji|sir|bhai|yaar|"
          r"dear|please|aap|tum|toh|to|well|hmm+|umm+|aaj|mujhe")
_NAME = r"cooper( ai)?|कूपर"
_HINGLISH_HINT = re.compile(r"\b(namaste|namaskar|namaskaar|kaise|kaisa|kaisi|kya|haal|shukriya|dhanyavaad|dhanyawad|"
                            r"ram ram|bahut|bohot|kaam|hai|hain|accha|achha|acha|thak|mujhe|dikkat|aap|tum|hoon|nahi|"
                            r"pranam|alvida|milte|theek|thik|badhiya)\b", re.I)
_FMCG_HINT = re.compile(
    r"\b(sales?|sale|sell\w*|sold|revenue|orders?|invoices?|billing|outlets?|retailers?|stores?|shops?|distributors?|"
    r"stockists?|reps?|salesm[ae]n|team|skus?|products?|brands?|categor(y|ies)|zones?|regions?|territor(y|ies)|states?|"
    r"cit(y|ies)|targets?|stock|coverage|penetration|calls?|visits?|ubo|tc|pc|lpc|mtd|ytd|qtd|lmtd|drop|growth|"
    r"bikri|dukaan|vitrak|primary|secondary|productive|productivity|kpi|numbers?|data|report)\b|"
    r"बिक्री|सेल|पीसी|टीसी|कवरेज|ऑर्डर|डिस्ट्रीब्यूटर|आउटलेट|ज़?ोन|जोन|ब्रांड|प्राइमरी|सेकेंडरी|प्रोडक्टिव|यूबीओ|दुकान",
    re.I)


@dataclass
class SmallTalk:
    kinds: list[str] = field(default_factory=list)
    rest: str = ""  # the question left after the greeting, if any
    lang: str = "en"

    @property
    def pure(self) -> bool:
        return bool(self.kinds) and not self.rest

    @property
    def found(self) -> bool:
        return bool(self.kinds)


def _clean(text: str) -> str:
    t = text.lower().replace("़", "").replace("’", "'")
    t = re.sub(r"[!?.,;:।…]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def analyse(question: str) -> SmallTalk:
    t = _clean(question)
    lang = detect(question)
    kinds: list[str] = []
    lead = re.compile(rf"(?<![\wऀ-ॿ])(?:{_PROBLEM_LEAD})(?![\wऀ-ॿ])", re.I)
    if lead.search(t):
        kinds.append("problem")
        t = lead.sub(" ", t)
    for kind, pat in _PATTERNS:
        rx = re.compile(rf"(?<![\wऀ-ॿ])(?:{pat})(?![\wऀ-ॿ])", re.I)
        if rx.search(t):
            if kind == "can" and _FMCG_HINT.search(rx.sub(" ", t)):
                continue  # "help me find low PC zones" is a data question, not "what can you do"
            kinds.append(kind)
            t = rx.sub(" ", t)
    name_only = re.fullmatch(rf"\s*(?:{_NAME})\s*", t, flags=re.I)
    if not kinds and name_only:
        kinds.append("greet")  # just "Cooper": a hello, never the off-topic filter
    if not kinds:
        return SmallTalk(lang=lang)
    if lang == "en" and _HINGLISH_HINT.search(question):
        lang = "hinglish"
    rest = re.sub(rf"(?<![\wऀ-ॿ])(?:{_NAME})(?![\wऀ-ॿ])", " ", t, flags=re.I)
    prev = None
    while prev != rest:  # strip lead-ins and fillers from the front only
        prev = rest
        rest = re.sub(rf"^\s*(?:{_LEADS})(?![\wऀ-ॿ])", " ", rest, flags=re.I).strip()
    rest = re.sub(r"\s+", " ", rest).strip()
    if rest and not _FMCG_HINT.search(rest):
        rest = ""  # "hi there", "good morning sir ji": nothing to answer (the gate still checks the whole message)
    if "problem" in kinds:
        if rest and _FMCG_HINT.search(rest):
            if not re.search(r"(why|kyu|kyun|kyon)|क्यों", rest):
                rest += " kyun" if lang != "en" else " why"  # a stated problem is a "why" question
        else:
            kinds[kinds.index("problem")] = "vague"
    return SmallTalk(kinds=kinds, rest=rest, lang=lang)


def has_question(st: SmallTalk) -> bool:
    return bool(st.rest) and bool(_FMCG_HINT.search(st.rest))


# --------------------------------------------------------------------------- replies


def _pick(options: list[str], seed: str) -> str:
    return options[sum(map(ord, seed)) % len(options)]


def chips(company: str, lang: str) -> list[str]:
    if lang == "en":
        return ["Show productive calls for this week", "Why is my primary sales low this week?",
                "Which zone has the lowest coverage?", "What are sales reps doing wrong?"]
    return ["Is hafte ke productive calls dikhao", "Is hafte primary sales kam kyun hai?",
            "Sabse kam coverage wala zone kaunsa hai?", "Sales reps kya galat kar rahe hain?"]


def problem_chips(lang: str) -> list[str]:
    if lang == "en":
        return ["Why are my productive calls low this week?", "Why did secondary sales drop this month?",
                "Why did coverage drop this month?", "What are sales reps doing wrong?"]
    return ["Is hafte PC kam kyun hai?", "Is mahine secondary sales kyun giri?", "Coverage kyun gira is mahine?",
            "Sales reps kya galat kar rahe hain?"]


def reply(st: SmallTalk, company: str, question: str) -> tuple[str, list[str]]:
    """Text and suggestion chips for a message that is only small talk."""
    hi = st.lang != "en"
    deva = st.lang == "hi"
    k = st.kinds
    if "vague" in k:
        if deva:
            return (f"अरे, कोई बात नहीं, मिलकर देखते हैं। बताइए क्या दिक्कत है: PC कम है, सेल्स गिरी है, कवरेज कम है, या "
                    f"रिप्स ठीक से काम नहीं कर रहे? मैं {company} का डेटा देखकर वजह बताऊँगा।", problem_chips("hinglish"))
        if hi:
            return (f"Arre, koi baat nahi, milkar dekhte hain. Batayiye kya dikkat hai: PC kam hai, sales giri hai, coverage "
                    f"kam hai, ya reps theek se kaam nahi kar rahe? Main {company} ka data dekhkar wajah bataunga.",
                    problem_chips("hinglish"))
        return (f"I'm sorry to hear that, let's sort it out together. What's going wrong: low productive calls, a sales "
                f"drop, falling coverage, or reps not performing? I'll look at {company}'s data and find the reason.",
                problem_chips("en"))
    if "feel" in k:
        tired = re.search(r"tired|exhausted|thak|kaam|busy|stress|tension|pareshan|bore|kharab|sad|थक|काम", question, re.I)
        if hi:
            line = (_pick(["Samajh sakta hoon, kaam ka load kabhi kabhi bahut hota hai. Thoda break le lijiye!",
                           "Arre, aaram se! Ek chai ho jaaye, phir saath mein dekhte hain."], question) if tired else
                    _pick(["Waah, yeh sunkar accha laga!", "Badhiya! Aise hi energy bani rahe."], question))
            return line + f" Jab chahein, main {company} ke numbers jaldi nikal dunga.", chips(company, "hinglish")
        line = (_pick(["That sounds like a long day. Take a short break if you can!",
                       "I hear you, some days are heavy. Let me take some of the load off."], question) if tired else
                _pick(["Glad to hear it!", "Love the energy!"], question))
        return line + f" Whenever you're ready, I can pull {company}'s numbers in seconds.", chips(company, "en")
    if "bye" in k:
        if hi:
            return _pick(["Alvida! Phir milte hain. Jab bhi zaroorat ho, main yahin hoon.",
                          "Theek hai, take care! Kabhi bhi numbers chahiye toh bas poochiye."], question), []
        return _pick(["Bye! I'm here whenever you need numbers.", "Take care! Come back anytime."], question), []
    if "thanks" in k:
        if hi:
            return _pick(["Aapka swagat hai! Aur kuch dekhna hai?", "Koi baat nahi, khushi hui madad karke! Aur kya dekhein?"],
                         question), chips(company, "hinglish")
        return _pick(["You're welcome! Anything else you'd like to look at?", "Happy to help! What's next?"],
                     question), chips(company, "en")
    if "who" in k or "can" in k:
        if hi:
            return (f"Main Cooper hoon, {company} ka sales analytics saathi. Main sales, PC, TC, coverage, UBO, distributors, "
                    f"brands aur reps ke numbers bata sakta hoon, aur yeh bhi ki koi number kyun gira ya badha, aur kya "
                    f"karna chahiye. Hindi, Hinglish ya English, jaise chahein poochiye.", chips(company, "hinglish"))
        return (f"I'm Cooper, {company}'s sales analytics assistant. I can show sales, calls, coverage, UBO, distributors, "
                f"brands and rep performance for any period, explain why a number went up or down, and suggest what to "
                f"do. Ask in English, Hindi or Hinglish.", chips(company, "en"))
    # greeting and / or "how are you"
    if hi:
        lead = "Namaste!" if "greet" in k or "how" not in k else ""
        how = _pick(["Main badhiya hoon, shukriya!", "Main bilkul theek hoon, poochne ke liye shukriya!"], question) \
            if "how" in k else ""
        ask = _pick([f"Aaj {company} ke baare mein kya dekhna hai?", f"Bataiye, aaj {company} ka kaunsa number dekhein?"],
                    question)
        return " ".join(x for x in (lead, how, ask) if x), chips(company, "hinglish")
    lead = _pick(["Hi there!", "Hello!", "Hey!"], question) if "greet" in k or "how" not in k else ""
    how = _pick(["I'm doing great, thanks for asking!", "All good here, thank you!"], question) if "how" in k else ""
    ask = _pick([f"What would you like to know about {company} today?", f"Which of {company}'s numbers shall we look at?"],
                question)
    return " ".join(x for x in (lead, how, ask) if x), chips(company, "en")


def prefix(st: SmallTalk) -> str:
    """One warm line before answering the real question in a mixed message."""
    hi = st.lang != "en"
    k = st.kinds
    if "how" in k:
        return "Main theek hoon, shukriya! Chaliye dekhte hain." if hi else "I'm doing well, thanks! Let's take a look."
    if "vague" in k or "feel" in k or "problem" in k:
        return "Koi baat nahi, chaliye dekhte hain." if hi else "Sorry to hear that, let's take a look."
    if "greet" in k:
        return "Namaste! Chaliye dekhte hain." if hi else "Hi! Let's take a look."
    return ""
