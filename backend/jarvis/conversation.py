"""Conversation state + reply generation (LLM streaming, sentence chunking, tools, confirmations)."""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid
from datetime import datetime
from typing import AsyncIterator

from .config import DATA_DIR, PERSONALITIES, Settings
from .interfaces import LLM
from .tools import APP_ALIASES, ToolBox, ToolResult, normalize_name

YES = re.compile(r"^\W*(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|please do|confirm|affirmative|that one|this one|"
                 r"the first one|first one|open it|that's it|correct|right)\b", re.I)
NO = re.compile(r"^\W*(no|nope|nah|don't|do not|cancel|stop|never mind|negative)\b", re.I)
_TOOL_HINT = re.compile(
    r"\b(open|launch|start|remind|reminder|time|date|day is it|search|find|file|run|command|website|site|"
    r"summar|ip address|disk|lock|flush|calculator|notepad|browser|youtube|google|github)\b", re.I)
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


_CLAIM = re.compile(
    r"\b(?:i'?ve|i have|i'?ll|i will|i'?m|i am|let me|i just|you'?ve|you have|i went ahead and)\s+(?:just\s+|now\s+|already\s+)?"
    r"(?:open(?:ed|ing)?|typ(?:e|ed|ing)|click(?:ed|ing)?|search(?:ed|ing)?|post(?:ed|ing)?|sen[dt]|sending|navigat\w*|"
    r"play(?:ed|ing)?|delet\w*|comment(?:ed|ing)?|scroll\w*|launch\w*|went to|go(?:ne)? to (?:the|your)|look(?:ed)? up|"
    r"checked your|read your|access(?:ed)?|shut(?:ting)? (?:it |the \w+ )?down|restart\w*|lock(?:ed|ing)?|set (?:a|an|the|your|it|up)|"
    r"turn(?:ed|ing)? (?:it |the \w+ )?(?:up|down|on|off)|mut(?:e|ed|ing)|paus(?:e|ed|ing)|skip(?:ped|ping)?|clos(?:e|ed|ing)|"
    r"remind(?:ed)? you|book(?:ed)?|order(?:ed)?|check(?:ing)?(?!\s+(?:in|out|back)\b)|"
    r"find (?:it |that )?out|look (?:it |that )?up|look into)\b", re.I)
# "Searching on YouTube for…", "Opening WhatsApp now." — narrating an action that never happened.
_CLAIM_START = re.compile(r"^\W*(?:ok(?:ay)?|sure|alright|right|done)?\W*(?:set (?:a |the |your )?(?:reminder|timer|alarm)|"
                          r"reminder set|added|scheduled|created|booked|searching|opening|typing|clicking|playing|sending|"
                          r"posting|navigating|launching|loading|pulling up|"
                          # "Scrolling down the page." (it hadn't) — but not "Skipping breakfast isn't great."
                          r"(?:scrolling|pausing|skipping|muting|refreshing|reloading)\s+(?:down|up|it|the|this|that|"
                          r"ahead|forward|back|now|again|through|for you)\b|going back to the (?:page|video|previous)|"
                          r"(?:i(?:'ve| have)?\s+)?(?:found|picked|chosen|queued(?: up)?)\s+(?:you\s+)?(?:a|an|the|some|one)\b"
                          r"(?=.*\b(?:songs?|tracks?|videos?|playlists?|results?|music)\b)|"
                          r"(?:just\s+|now\s+)?(?:skipped|paused|muted|unmuted|scrolled|clicked|opened|searched|typed|sent|"
                          r"played|launched|closed|locked|reloaded|refreshed|rewound|returned|went back|shut down|restarted))\b|"
                          r"^\W*(?:now,?\s+|and\s+)?(?:it'?s|it is|that'?s)\s+(?:now\s+)?(?:open|opened|playing|paused|muted|"
                          r"sent|done)\W*$", re.I)
# "The laptop is shutting down.", "A 5-minute timer has been set.", "The screen is now locked."
_CLAIM_PASSIVE = re.compile(
    r"\b(?:is|are|has been|have been|was|were)\s+(?:now\s+|being\s+)?(?:set|started|locked|unlocked|closed|"
    r"shut(?:ting)?\s+down|restart(?:ed|ing)|turned\s+(?:up|down|on|off)|muted|unmuted|opened|sent|playing|paused|"
    r"updated|deleted|saved|scheduled|booked|ordered|cancelled|increased|decreased)\b|"
    r"\b(?:timer|alarm|reminder)\s+(?:is\s+|has\s+been\s+)?(?:started|set|on)\b|\bi can (?:play|put|send) it\b|"
    r"\bi(?:'ve| have)\s+(?:got|turned|put|set|cranked)\s+(?:it|that|the \w+)\s+(?:up|down|on|off|open|playing)\b", re.I)
HONEST_CANTS = [
    "I can't actually do that from a chat. Ask me straight out, like “search YouTube for anime fights” or “type hello”.",
    "That's not something I did, to be honest. Try telling me directly, say “open WhatsApp” or “click the first video”.",
    "I didn't do that. If you want, tell me exactly what to open, search or type and I'll handle it.",
]
HONEST_CANT = HONEST_CANTS[0]


# Self-descriptions that are wrong for a voice assistant that can act ("I'm a text-based AI", "I can only respond to
# what you type", "I don't have a physical presence"). Those sentences are dropped from spoken replies.
_ROBOTIC = re.compile(r"\b(?:text-based|text based|chat ?window|respond to what you type|"
                      r"only (?:respond|reply) (?:to|in) (?:text|chat)|as an ai language model|"
                      r"don'?t have (?:the ability|the capability|access) to (?:click|open|type|search|play|scroll|set)|"
                      r"not (?:capable|able) of (?:clicking|opening|typing|searching|playing))", re.I)


def is_robotic(sentence: str) -> bool:
    return bool(_ROBOTIC.search(sentence))


# A bare "Done." / "There you go." after a request is a claim too (seen after "lock my computer").
_CLAIM_DONE = re.compile(r"^\W*(?:(?:ok(?:ay)?|alright|sure|right)\W+)?(?:all\s+)?(?:done|it'?s done|there you go|there you are|"
                         r"all set)\W*$", re.I)


def claims_action(sentence: str) -> bool:
    """True when a chat reply pretends JARVIS did something on the computer (only tools can do things)."""
    return bool(_CLAIM.search(sentence) or _CLAIM_START.search(sentence) or _CLAIM_PASSIVE.search(sentence)
                or _CLAIM_DONE.search(sentence))


# Requests JARVIS can't do: answer honestly, and offer the nearest thing it can do (asked as a yes/no).
CANT_DO = [
    (r"\b(?:shut\s*down|switch off|turn off|power off|power down|restart|reboot|hibernate|log\s*off|sign\s*out)\b.*"
     r"\b(?:laptop|computer|pc|system|windows|machine)\b|^(?:shut\s*down|restart|reboot)\b",
     "I can't shut down or restart the laptop. I can lock the screen though. Want me to?",
     ("run_command", {"name": "lock screen"})),
    (r"\b(?:wi-?fi|bluetooth|airplane mode|night light|battery saver)\b",
     "I can't change settings like that yet. I can change the volume and the screen brightness though.", None),
    (r"^(?:close|quit|exit|kill)\s+(?!(?:this\s+|the\s+)?tab\b)(?:it|that|this|the app|[a-z0-9 ]+)$",
     "I can't close apps yet. I can close tabs in my Edge window though.", None),
    (r"\b(?:weather|forecast|temperature outside|will it rain|is it raining)\b",
     "I can't check live weather myself, but I can look it up on Google. Want me to?", "SEARCH_TEXT"),
    (r"\b(?:latest news|news today|today'?s news|live score|cricket score|stock price|share price|bitcoin price)\b",
     "I can't see live news or prices, but I can search Google for that. Want me to?", "SEARCH_TEXT"),
    # Reading someone's messages/mail: JARVIS can't see app contents. Offer to open the app instead.
    (r"\b(?:read|check|see|show)\b.*\b(?:messages?|texts?|chats?|e-?mails?|mails?|inbox|notifications?|dms?)\b"
     r"(?!.*\b(?:page|tab|screen)\b)",
     "I can't read your {app} messages myself. I can open {app} in my Edge window so you can check. Want me to?", "MSG_APP"),
    (r"^(?:call|ring|dial|phone|video call)\s+(?!it\b|that\b|this\b)(?:my\s+|to\s+)?[a-z]+(?:\s+[a-z]+)?$|"
     r"\b(?:make|place)\s+a\s+(?:phone\s+|video\s+)?call\b",
     "I can't make phone calls from here. I can open WhatsApp in my Edge window so you can call from there. Want me to?",
     ("open_website", {"url": "whatsapp"})),
    (r"^(?:write|send|compose|draft|reply to)\b.*\b(?:e-?mails?|mails?|gmail)\b",
     "I can't send emails for you. I can open Gmail in my Edge window so you can write it. Want me to?",
     ("open_website", {"url": "gmail"})),
    (r"^(?:post|upload|share|tweet)\b.*\b(?:story|stories|post|reel|photo|pic|picture|video|status|tweet)\b|"
     r"^(?:like|comment on|follow)\s+(?:the|this|my|his|her)\b",
     "I can't post or react on social media for you. I can open {app} in my Edge window so you can. Want me to?", "SOCIAL"),
    (r"^(?:delete|erase|wipe|format|uninstall|install|rename|move)\b(?!\s+(?:the\s+)?(?:last\s+)?(?:word|letter|text)\b)",
     "I can't delete, format, move or install anything. That's on purpose, so nothing gets lost by accident. "
     "I can open File Explorer so you can do it yourself. Want me to?", ("open_app", {"name": "file explorer"})),
    (r"\b(?:convert|exchange rate|conversion rate)\b.*\b(?:dollars?|usd|euros?|pounds?|yen|dirhams?|rupees?|inr|bitcoin)\b|"
     r"\bhow much is\b.*\b(?:dollars?|usd|euros?|pounds?|yen|dirhams?)\b.*\b(?:in|to)\s+(?:rupees?|inr)\b",
     "I can't check live exchange rates, but I can search Google for it. Want me to?", "SEARCH_TEXT"),
    (r"^(?:order|buy|purchase|book|pay)\b(?!\s+(?:of|in|attention|a visit)\b)(?!.*\b(?:is|are|was)\b)",
     "I can't place orders or pay for things. I can open {app} so you can do it yourself. Want me to?", "ORDER"),
]
MSG_APPS = [(r"\b(?:e-?mails?|mails?|inbox|gmail)\b", "gmail", "Gmail"), (r"\b(?:insta\w*|dms?)\b", "instagram", "Instagram"),
            (r"\btelegram\b", "telegram", "Telegram"), (r"\blinked\s*in\b", "linkedin", "LinkedIn"), (r".", "whatsapp", "WhatsApp")]
SOCIAL_APPS = [(r"\b(?:insta\w*|reels?)\b", "instagram", "Instagram"), (r"\b(?:facebook|fb)\b", "facebook", "Facebook"),
               (r"\b(?:twitter|tweet|x)\b", "twitter", "X"), (r"\blinked\s*in\b", "linkedin", "LinkedIn"),
               (r"\b(?:youtube|video)\b", "youtube", "YouTube"), (r"\bwhatsapp\b", "whatsapp", "WhatsApp")]
ORDER_SITES = [(r"\b(?:swiggy|food|pizza|burger|biryani|dinner|lunch)\b", "swiggy", "Swiggy"), (r"\bzomato\b", "zomato", "Zomato"),
               (r"\bflipkart\b", "flipkart", "Flipkart"), (r"\b(?:amazon|buy|purchase)\b", "amazon", "Amazon")]


def _cant_offer(t: str, reply: str, offer):
    """Fill in a CANT_DO reply's {app} and offer from what the user actually asked."""
    if offer == "SEARCH_TEXT":
        q = re.sub(r"^(?:what(?:'s| is)|how(?:'s| is)|tell me|show me|check|give me)\s+(?:the\s+)?", "", t).strip(" ?")
        return reply, ("web_search", {"query": q or t})
    if offer == "MSG_APP":
        key, name = next((k, n) for p, k, n in MSG_APPS if re.search(p, t))
        return reply.format(app=name), ("open_website", {"url": key})
    if offer == "SOCIAL":
        key, name = next(((k, n) for p, k, n in SOCIAL_APPS if re.search(p, t)), ("instagram", "Instagram"))
        return reply.format(app=name), ("open_website", {"url": key})
    if offer == "ORDER":
        hit = next(((k, n) for p, k, n in ORDER_SITES if re.search(p, t)), None)
        if hit:
            return reply.format(app=hit[1]), ("open_website", {"url": hit[0]})
        return "I can't place orders or pay for things. I can search Google for it so you can do it yourself. Want me to?", \
            ("web_search", {"query": re.sub(r"^(?:order|buy|purchase|book|pay)\s+(?:me\s+)?", "", t)})
    return reply, offer
QUESTION_START = re.compile(r"^(?:how|what|why|when|where|who|which|should|shall|would|is it|are|does|do i|can i|could i|"
                            r"i think|i'll|i will|i might|i'm going to|i am going to|i want to|i wanna)\b")


SEARCH_SITES = {"youtube", "amazon", "flipkart", "wikipedia", "github", "google", "spotify web", "google maps", "reddit",
                "instagram", "swiggy"}

_FILLER = re.compile(r"^(?:so|okay|ok|yeah|yes|yep|cool|well|um+|uh+|hey|hi|hello|no|nah|alright|right|now|just|please|"
                     r"jarvis|can you hear me|are you (?:still )?there|are you listening|can you|could you|would you|will you|i want you to|i need you to|i'd like you to|"
                     r"go ahead and|i said|i asked you to)\b[\s,.!?]*", re.I)
BROWSERS = r"(?:google\s+)?chrom(?:e|a|e\s+bed)|microsoft\s+edge|edge|firefox|brave|(?:the\s+|my\s+)?browser"
OPEN_VERB = r"(?:open(?:\s+up)?|launch|start|go\s+to|pull\s+up|bring\s+up|show\s+me)"


def clean_command(text: str) -> str:
    """'So, okay. Can you open up WhatsApp?' → 'open up whatsapp'."""
    t = re.sub(r"[.!?]+$", "", text.strip().lower())
    prev = None
    while prev != t:
        prev, t = t, _FILLER.sub("", t).strip(" ,.")
        t = re.sub(rf"\b{OPEN_VERB}\s+(?:and\s+)?(?={OPEN_VERB}\b)", "", t)         # "open and open up X" → "open up X"
        t = re.sub(r"\s+(?:in|on|with|using|inside)(?:\s+(?:the|a|my))?$", "", t)  # cut off: "open X in the"
    # Self-correction: "open notepad, no, calculator" / "open vs code. actually no, open cursor" → the last version wins.
    marks = list(re.finditer(r"(?:\bactually,?\s+no\b|\bno,?\s+wait\b|\bwait,?\s+no\b|\bno,?\s+no\b|\bsorry\b|"
                             r"[,.]\s*no\b|\bi mean\b|\binstead\b)(?:[\s,.]*\b(?:wait|actually|sorry|i mean)\b)*[\s,.]*", t))
    if marks:
        before, after = t[:marks[-1].start()], t[marks[-1].end():].strip(" ,.")
        if after:
            verb = re.match(rf"^(?:{OPEN_VERB}|search(?:\s+for)?|play|type)\b", before.strip())
            if verb and not re.match(rf"^(?:{OPEN_VERB}|search|play|type|click)\b", after):
                after = f"{verb.group(0)} {after}"
            t = after
            prev = None
            while prev != t:
                prev, t = t, _FILLER.sub("", t).strip(" ,.")
    return t


def _browser(word: str | None) -> str | None:
    if not word:
        return None
    w = word.lower()
    return "chrome" if "chrom" in w else "edge" if ("edge" in w or "browser" in w or "internet" in w) else \
        "firefox" if "firefox" in w else "brave" if "brave" in w else None


SITE_NAMES = r"youtube|you tube|amazon|flipkart|wikipedia|github|google|spotify|google maps|maps|reddit|instagram|insta|swiggy"
BROWSER_WORDS = {"browser", "the browser", "web browser", "internet", "edge", "microsoft edge", "edge browser", "jarvis edge"}
_ORD = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "fifth": 4, "last": -1}


def _site(word: str) -> str:
    w = word.lower().strip()
    return {"you tube": "youtube", "maps": "google maps", "spotify": "spotify web", "insta": "instagram"}.get(w, w)


def _clean_query(q: str) -> str:
    q = re.sub(r"^(?:the\s+)?(?:videos?|songs?|clips?|results?)\s+(?:of|for|about|on)\s+", "", q.strip())
    q = re.sub(r"\s+and\s+(?:explain|tell me|show me).*$", "", q)
    return re.sub(r"^(?:for|the|a|an|some|about)\s+", "", q).strip(" .,")


_N = r"(\d{1,3})\s*(?:%|percent|per cent)?"      # unnamed: it appears twice in one pattern


def route_system(t: str) -> list[tuple[str, dict]] | None:
    """Laptop volume and screen brightness: 'turn it up', 'volume to 40', 'mute the laptop', 'dim the screen'."""
    if re.search(r"\b(?:brightness|screen|display)\b", t):
        m = re.search(rf"\bbrightness\s+(?:to|at|on)?\s*{_N}$|^(?:set|put|change|make|turn)\s+(?:the\s+)?(?:screen\s+)?"
                      rf"brightness\s+(?:to|at|on)?\s*{_N}$", t)
        if m:
            return [("system_brightness", {"action": "set", "value": int(next(g for g in m.groups() if g))})]
        if re.search(r"\b(?:max(?:imum)?|full|100)\s+brightness\b|\bbrightness\s+(?:to\s+)?(?:max|full)\b", t):
            return [("system_brightness", {"action": "set", "value": 100})]
        if re.search(r"^what(?:'s| is)\s+the\s+(?:screen\s+)?brightness", t):
            return [("system_brightness", {"action": "get"})]
        lot = 30 if re.search(r"\ba lot\b|\bway\b", t) else 15
        if re.search(r"\b(?:brightness|screen)\s+up\b|\b(?:increase|raise|turn up|bump up)\s+(?:the\s+)?(?:screen\s+)?"
                     r"brightness\b|\b(?:brighter|brighten)\b", t):
            return [("system_brightness", {"action": "up", "value": lot})]
        if re.search(r"\b(?:brightness|screen)\s+down\b|\b(?:decrease|lower|reduce|turn down)\s+(?:the\s+)?(?:screen\s+)?"
                     r"brightness\b|\b(?:darker|dimmer|dim)\b", t):
            return [("system_brightness", {"action": "down", "value": lot})]
    if re.match(r"^(?:mute|silence)\s+(?:the\s+)?(?:laptop|computer|pc|system|volume|speakers?|everything|all (?:the )?sound)$|"
                r"^turn\s+(?:the\s+)?(?:sound|volume)\s+off$", t):
        return [("system_volume", {"action": "mute"})]
    if re.match(r"^unmute\s+(?:the\s+)?(?:laptop|computer|pc|system|volume|speakers?|everything|sound)$|"
                r"^turn\s+(?:the\s+)?(?:sound|volume)\s+(?:back\s+)?on$", t):
        return [("system_volume", {"action": "unmute"})]
    if not re.search(r"\b(?:volume|sound|louder|quieter|softer|loud|turn it up|turn it down)\b", t):
        return None
    m = re.search(rf"\b(?:volume|sound)\s+(?:to|at|on)?\s*{_N}$|^(?:set|put|change|make|turn)\s+(?:the\s+)?(?:volume|sound)"
                  rf"\s+(?:to|at|on)?\s*{_N}$", t)
    if m:
        return [("system_volume", {"action": "set", "value": int(next(g for g in m.groups() if g))})]
    if re.search(r"\b(?:max(?:imum)?|full)\s+(?:volume|sound)\b|\b(?:volume|sound)\s+(?:to\s+)?(?:max|full)\b", t):
        return [("system_volume", {"action": "set", "value": 100})]
    if re.search(r"^(?:what(?:'s| is)\s+the\s+volume|how loud is it)\b", t):
        return [("system_volume", {"action": "get"})]
    lot = 25 if re.search(r"\ba lot\b|\bway\b|\breally\b", t) else 10
    if re.search(r"^(?:turn|crank|bring|put|pump)\s+(?:the\s+)?(?:volume|sound|it|music)\s+up\b|\b(?:volume|sound)\s+up\b|"
                 r"\b(?:increase|raise)\s+(?:the\s+)?(?:volume|sound)\b|\blouder\b", t):
        return [("system_volume", {"action": "up", "value": lot})]
    if re.search(r"^(?:turn|bring|put)\s+(?:the\s+)?(?:volume|sound|it|music)\s+down\b|\b(?:volume|sound)\s+down\b|"
                 r"\b(?:decrease|lower|reduce)\s+(?:the\s+)?(?:volume|sound)\b|\b(?:quieter|softer)\b", t):
        return [("system_volume", {"action": "down", "value": lot})]
    return None


def route_form(t: str) -> list[tuple[str, dict]] | None:
    """Forms and multi-step web tasks, checked before plain 'type …' so 'type Yash in the name field' fills that field."""
    # Several steps on a page: the browser agent ("fill this form", "on this site, sign up with my details")
    if re.match(r"^(?:(?:fill|fill in|fill out|fill up|complete|do)\s+(?:this|the|that)\s+(?:form|sign ?up|application|"
                r"registration)\b.*|(?:on|in)\s+(?:this|the)\s+(?:page|site|website|browser|tab)\b,?\s+.+|"
                r"do this in the browser:?\s+.+|(?:sign up|register|create an account)\s+(?:on|for|at|with)\b.+)$", t):
        return [("browser_task", {"goal": t})]
    # One field at a time: "type Yash in the name field", "fill email with x@y.com", "choose India from the country list"
    m = re.match(r"^(?:type|enter|put|write|fill(?:\s+in)?)\s+(?P<v>.+?)\s+(?:in|into|as|for)\s+(?:the\s+|my\s+)?"
                 r"(?P<f>[a-z0-9' -]+?)\s+(?:field|box|input|section|column)$", t) or \
        re.match(r"^(?:fill|fill in|set|put)\s+(?:the\s+|my\s+)?(?P<f>[a-z0-9' -]+?)(?:\s+field|\s+box)?\s+(?:with|to|as)\s+"
                 r"(?P<v>.+)$", t)
    if m and not re.match(r"^(?:set|put)\s+(?:the\s+)?(?:volume|brightness|timer|alarm|reminder)\b", t) and \
            m.group("f").strip() not in ("search", "search bar", "search box", "message", "message box", "comment", "chat"):
        return [("browser_fill", {"field": m.group("f").strip(), "value": m.group("v").strip(" '\"“”")})]
    m = re.match(r"^(?:select|choose|pick)\s+(?P<v>.+?)\s+(?:in|from|for)\s+(?:the\s+)?(?P<f>[a-z0-9' -]+?)"
                 r"(?:\s+(?:dropdown|drop down|list|menu|options?|field))$", t)
    if m:
        return [("browser_fill", {"field": m.group("f").strip(), "value": m.group("v").strip()})]
    m = re.match(r"^(?P<a>tick|check|untick|uncheck)\s+(?:the\s+)?(?P<l>.+?)\s+(?:box|checkbox|check box|option)$", t)
    if m:
        return [("browser_check", {"label": m.group("l"), "on": m.group("a") in ("tick", "check")})]
    return None


def route_browser(t: str) -> list[tuple[str, dict]] | None:
    """Commands that act on JARVIS's Edge window (type, click, scroll, video controls, page reading)."""
    sysctl = route_system(t) or route_form(t)
    if sysctl:
        return sysctl
    if re.search(r"\b(summar\w*|read|what'?s|what is|describe|tell me|explain)\b.*\b(page|screen|video|article|website|site|tab)\b", t):
        return [("browser_read", {"summary": bool(re.search(r"summar|describe|read|about|happening|explain", t))})]
    m = re.match(rf"^play\s+(?P<q>.+?)\s+(?:on|in)\s+(?:the\s+)?(?:youtube|you tube)$", t)
    if m:
        return [("web_search", {"query": _clean_query(m.group("q")), "site": "youtube"}),
                ("browser_click", {"target": "first video"})]
    m = None if re.match(r"^write\s+(?:me\s+)?(?:an?|the|some|my)\s+", t) else \
        re.match(r"^(?:type|write|enter|put)(?!\s+on\b)\s+(?P<t>.+?)"
                 r"(?:\s+(?:in|into|on)\s+(?:the\s+)?(?:search(?:\s*bar|\s*box)?|box|field|text\s*box|chat|message(?:\s*box)?|"
                 r"comments?(?:\s*box)?|youtube|whatsapp|it|there|this))?"
                 r"(?P<sub>\s+and\s+(?:press\s+enter|hit\s+enter|search(?: it)?|send(?: it)?|submit|enter|post(?: it)?))?$", t)
    if m and m.group("t").strip() not in ("it", "something", "the", "a", "this"):
        return [("browser_type", {"text": m.group("t").strip(" '\"“”"), "submit": bool(m.group("sub"))})]
    if m or re.match(r"^(?:type|write)(?:\s+(?:it|something|the|a|this))?$", t):
        return [("ask_verb", {"verb": "type", "text": "Sure, what should I type?"})]
    # A bare verb ("open", "search", "click it"): ask what, and treat the next answer as the rest of the command.
    m = re.match(r"^(?P<v>open|launch|search|search for|google|look up|click|click on|tap|remind me|set a reminder)"
                 r"(?:\s+(?:it|that|this|something|one|up|for me|please))?$", t)
    if m:
        v = m.group("v")
        verb, ask = ("open", "Sure, what should I open?") if v in ("open", "launch") else \
            ("search for", "Sure, what should I search for?") if v.startswith(("search", "google", "look")) else \
            ("click", "Sure, what should I click?") if v.startswith(("click", "tap")) else \
            ("remind me to", "Sure, what should I remind you about, and when?")
        return [("ask_verb", {"verb": verb, "text": ask})]
    if re.match(r"^(?:send|send it|send the message|send message|post it|submit|submit it)$", t):
        return [("browser_key", {"key": "enter"})]
    m = re.match(r"^(?:press|hit)\s+(?:the\s+)?(?P<k>enter|escape|esc|space|tab|backspace|down|up|left|right|page down|page up)(?:\s+key)?$", t)
    if m:
        return [("browser_key", {"key": "escape" if m.group("k") == "esc" else m.group("k")})]
    m = re.match(r"^play\s+(?:me\s+)?(?:a|some|any)\s+(?:random\s+|good\s+|nice\s+)?(?P<q>(?:[a-z0-9']+\s+){0,3}?(?:songs?|music))"
                 r"(?:\s+for\s+me)?(?:\s+(?:on|in)\s+(?:the\s+)?(?:youtube|you tube))?$", t)
    if m:  # "can you play a song for me", "play some lofi music"
        q = m.group("q")
        return [("web_search", {"query": "songs" if q in ("song", "songs") else q, "site": "youtube"}),
                ("browser_click", {"target": "first video"})]
    m = re.match(r"^(?P<a>pause|stop|play|resume|unpause|continue|next|skip|previous)(?:\s+(?:the\s+)?(?:song|music|track))?"
                 r"\s+(?:on\s+|in\s+)?spotify$|^(?P<b>next|previous)\s+(?:song|track)\s+(?:on|in)\s+spotify$", t)
    if m:  # "pause spotify", "next song on spotify": the Spotify app, wherever you are
        a = m.group("a") or m.group("b")
        action = {"stop": "pause", "resume": "play", "unpause": "play", "continue": "play", "skip": "next"}.get(a, a)
        return [("browser_media", {"action": action, "app": "spotify"})]
    media = [(r"^(?:pause|stop)(?:\s+(?:the|this|it))?(?:\s+(?:video|music|song|playback))?$", "pause"),
             (r"^(?:play|resume|continue|unpause)(?:\s+(?:the|this|it))?(?:\s+(?:video|music|song))?(?:\s+again)?$", "play"),
             (r"^mute(?:\s+(?:the|this|it))?(?:\s+(?:video|sound|audio))?$", "mute"),
             (r"^unmute(?:\s+(?:the|this|it))?(?:\s+(?:video|sound|audio))?$", "unmute"),
             (r"\b(?:next video|skip (?:this )?video|next song|play (?:the )?next)\b", "next"),
             (r"\bskip (?:the )?ads?\b", "skip ad"),
             (r"\b(?:fast forward|skip ahead|(?:skip|go|jump)?\s*forward (?:ten|10) seconds?|"
              r"skip (?:ahead )?(?:ten|10|a few) seconds?)\b", "forward"),
             (r"\b(?:rewind|back (?:ten|10) seconds?)\b", "back")]
    for pattern, action in media:
        if re.search(pattern, t):
            return [("browser_media", {"action": action})]
    nav = [(r"^(?:go\s+)?back(?:\s+(?:a|one)\s+page|\s+to\s+(?:the\s+)?(?:previous|last)\s+(?:page|video|tab|site))?$|"
            r"^(?:go\s+to\s+)?(?:the\s+)?previous\s+page$", "back"), (r"^go\s+forward(?:\s+a\s+page)?$", "forward"),
           (r"^(?:refresh|reload)(?:\s+(?:the|this)\s+page)?$", "reload"), (r"^close\s+(?:this\s+|the\s+)?tab$", "close"),
           (r"^(?:open\s+)?(?:a\s+)?new\s+tab$", "new tab")]
    for pattern, action in nav:
        if re.match(pattern, t):
            return [("browser_nav", {"action": action})]
    # "keep on scrolling" means keep going until told to stop (in JARVIS's Edge it scrolls by itself)
    m = re.match(r"^(?:keep|continue|start|go on)\s+(?:on\s+)?scrolling(?:\s+(?:down|through\b.*|the page|on))?"
                 r"(?:\s+(?P<s>slowly|slow|fast|quickly))?$|^(?:auto[\s-]?scroll|scroll\s+(?:continuously|nonstop|non-stop|"
                 r"automatically|by itself)|keep going down)(?:\s+(?P<s2>slowly|slow|fast|quickly))?$|"
                 r"^scroll\s+(?P<s3>slowly|fast|quickly)$", t)
    if m:
        s = m.group("s") or m.group("s2") or m.group("s3") or ""
        speed = "slow" if s.startswith("slow") else "fast" if s in ("fast", "quickly") else "normal"
        return [("browser_scroll", {"direction": "down", "continuous": True, "speed": speed})]
    m = re.search(r"\bscroll(?:ing)?\s+(?:it\s+|the page\s+|a bit\s+|a little\s+)?(?P<d>down|up)\b|"
                  r"^(?:go|scroll)\s+to\s+the\s+(?P<e>top|bottom)$|^(?:go|move)\s+(?P<g>down|up)(?:\s+a bit|\s+a little)?$", t)
    if m:
        return [("browser_scroll", {"direction": m.group("d") or m.group("e") or m.group("g")})]
    if re.match(r"^(?:stop|pause|quit|end)\s+(?:the\s+)?scroll(?:ing)?$|^(?:that'?s\s+)?enough(?:\s+scrolling)?$|"
                r"^(?:stop|okay stop|ok stop) (?:there|now|it)$", t):
        return [("browser_scroll", {"direction": "stop"})]
    m = re.match(r"^(?:scroll\s+)?(?P<x>faster|slower)(?:\s+please)?$|^scroll\s+(?:a\s+bit\s+)?(?P<y>faster|slower)$", t)
    if m:
        return [("browser_scroll", {"direction": "down", "speed": m.group("x") or m.group("y")})]
    if re.match(r"^scroll(?:ing)?(?:\s+(?:more|a bit|a little|again|it|the page|through\b.*|on|further))?$|"
                r"^scroll\s+more\b.*$", t):
        return [("browser_scroll", {"direction": "down"})]        # one page: "scroll", "scroll more"
    # "click the video with high likes" / "play the most viewed one": YouTube results show views, so pick by views.
    if re.match(r"^(?:click|tap|press|select|choose|open|play|watch|pick)\b", t) and \
            re.search(r"\b(?:most|highest|high|max(?:imum)?|top|more|lots of|many)\s+(?:likes?|liked|views?|viewed|popular)\b|"
                      r"\b(?:most|highest)\s+(?:liked|viewed|popular)\b|\bpopular\s+(?:video|one|song)\b", t):
        return [("browser_click", {"target": "most viewed video", "asked": "likes" if "like" in t else "views"})]
    # "click the first video to play it" → the purpose at the end isn't part of what to click.
    t = re.sub(r"^((?:click|tap|press|select|choose|open|play|watch|pick)\b.+?)\s+(?:to|and)\s+(?:play|watch|open|start)\s+(?:it|that)$",
               r"\1", t)
    m = re.match(r"^(?:click|tap|press|select|choose|open|play|watch|pick)\s+(?:on\s+|in\s+)?(?:the\s+|a\s+|any\s+|some\s+)?"
                 r"(?P<r>(?:random|any)\s+)?(?:video|song)s?(?:\s+(?:in|on|from)\s+(?:the\s+)?(?:list|hit|page|results?|screen))?$", t) \
        or re.match(r"^(?:click|tap|select|choose|play|watch|pick)\s+(?:on\s+)?(?:a\s+|any\s+)?random\s+(?:one|thing)$", t)
    if m:  # "click in the video", "click random video", "play any random video"
        rnd = "random" in t or re.match(r"^\w+\s+(?:on\s+)?any\b", t)
        return [("browser_click", {"target": "first video", "nth": random.randint(0, 4) if rnd else 0})]
    m = re.match(r"^(?:click|tap|press|select|choose|open|play|watch)\s+(?:on\s+)?(?:the\s+)?(?P<ord>first|second|third|fourth|fifth|1st|2nd|3rd|last)\s+"
                 r"(?P<x>video|result|link|song|one)s?$", t)
    if m:
        kind = {"song": "video", "one": "video", "link": "result"}.get(m.group("x"), m.group("x"))
        return [("browser_click", {"target": f"first {kind}" if kind in ("video", "result") else kind,
                                   "nth": max(0, _ORD[m.group("ord")]) if _ORD[m.group("ord")] >= 0 else 99})]
    m = re.match(r"^(?:click|tap|press|select)\s+(?:on\s+)?(?:the\s+)?(?P<x>.+?)(?:\s+(?:button|link|tab|option))?$", t)
    if m:
        return [("browser_click", {"target": m.group("x").strip(" '\"“”")})]
    return None


CITY_TZ = {
    "india": "Asia/Kolkata", "delhi": "Asia/Kolkata", "mumbai": "Asia/Kolkata", "bangalore": "Asia/Kolkata",
    "hyderabad": "Asia/Kolkata", "chennai": "Asia/Kolkata", "kolkata": "Asia/Kolkata", "punjab": "Asia/Kolkata",
    "london": "Europe/London", "uk": "Europe/London", "england": "Europe/London", "paris": "Europe/Paris",
    "france": "Europe/Paris", "berlin": "Europe/Berlin", "germany": "Europe/Berlin", "moscow": "Europe/Moscow",
    "dubai": "Asia/Dubai", "uae": "Asia/Dubai", "singapore": "Asia/Singapore", "tokyo": "Asia/Tokyo", "japan": "Asia/Tokyo",
    "beijing": "Asia/Shanghai", "china": "Asia/Shanghai", "hong kong": "Asia/Hong_Kong", "sydney": "Australia/Sydney",
    "australia": "Australia/Sydney", "new york": "America/New_York", "usa": "America/New_York", "america": "America/New_York",
    "washington": "America/New_York", "toronto": "America/Toronto", "canada": "America/Toronto",
    "chicago": "America/Chicago", "los angeles": "America/Los_Angeles", "california": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles", "seattle": "America/Los_Angeles", "nepal": "Asia/Kathmandu",
    "pakistan": "Asia/Karachi", "bangladesh": "Asia/Dhaka", "sri lanka": "Asia/Colombo", "saudi": "Asia/Riyadh",
}


def _hours(delta_h: float) -> str:
    h = abs(delta_h)
    whole, frac = int(h), h - int(h)
    word = {0.5: " and a half", 0.75: " and three quarters", 0.25: " and a quarter"}.get(round(frac, 2), "")
    return f"{whole}{word} hour{'s' if h != 1 else ''}"


def world_time(t: str) -> str | None:
    """Exact answers for 'what time is it in London' / 'time difference between India and London'."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    names = "|".join(sorted(CITY_TZ, key=len, reverse=True))
    m = re.search(rf"\b(?:time difference|difference in time|how many hours)\b.*?\b(?P<a>{names})\b.*?\b(?P<b>{names})\b", t) or \
        re.search(rf"\bhow (?:far )?(?:ahead|behind) is (?P<a>{names})\b.*?\b(?P<b>{names})\b", t)
    try:
        if m:
            a, b = m.group("a"), m.group("b")
            now = _dt.now(ZoneInfo("UTC"))
            off = lambda z: now.astimezone(ZoneInfo(CITY_TZ[z])).utcoffset().total_seconds() / 3600
            d = off(a) - off(b)
            if abs(d) < 0.01:
                return f"{a.title()} and {b.title()} are on the same time right now."
            return f"{a.title()} is {_hours(d)} {'ahead of' if d > 0 else 'behind'} {b.title()} right now."
        m = re.search(rf"\btime (?:is it )?(?:right now |now )?in (?P<a>{names})(?:,?\s+(?:and|&)\s+(?:in\s+)?(?P<b>{names}))+\b", t)
        if m:  # "what's the time in new york and london"
            cities = re.findall(rf"\b(?:{names})\b", t[m.start():])
            said = [f"{_dt.now(ZoneInfo(CITY_TZ[c])).strftime('%I:%M %p').lstrip('0')} in {c.title()}" for c in dict.fromkeys(cities)]
            if len(said) > 1:
                return f"It's {', '.join(said[:-1])} and {said[-1]} right now."
        m = re.search(rf"\b(?:what(?:'s| is) the )?time (?:is it )?(?:right now |now )?in (?P<a>{names})\b|\b(?P<c>{names}) time\b", t)
        if m and re.search(r"\btime\b", t):
            city = m.group("a") or m.group("c")
            local = _dt.now(ZoneInfo(CITY_TZ[city]))
            return f"It's {local.strftime('%I:%M %p').lstrip('0')} in {city.title()} right now."
    except Exception:
        return None
    return None


def route_site_search(t: str) -> tuple[str, dict] | None:
    t = re.sub(r"\b(?:that'?s\s+)?(?:such|sorts?|surge|sir?ch)\s+(?=for\b)", "search ", t)  # Whisper mishearings of "search"
    t = re.sub(r"\bsearch\s+(?:for\s+)?search\b", "search", t)                           # "search such for X"
    m = re.match(rf"^(?:on|in)\s+(?:the\s+)?(?P<s>{SITE_NAMES})\s*,?\s+(?:search|look up|find|look for)\s+(?:for\s+)?(?P<q>.+)$", t)
    if m:  # "on youtube search lofi beats"
        return "web_search", {"query": _clean_query(m.group("q")), "site": _site(m.group("s"))}
    m = re.search(rf"\b(?:search|look up|find|look for)\s+(?:for\s+)?(?P<q>.+?)\s+(?:on|in)\s+(?:the\s+)?(?P<s>{SITE_NAMES})\b", t)
    if m and (not _command_start(t, m) or m.group("q") in ("it", "that", "this", "them")):
        m = None  # "I told him to search for it on google"
    m = m or \
        re.match(rf"^(?:search|check)\s+(?:on\s+)?(?P<s>{SITE_NAMES})\s+for\s+(?P<q>.+)$", t) or \
        re.match(rf"^(?P<s>youtube|you tube|amazon|flipkart|wikipedia)\s+(?:search\s+)?(?:for\s+)?(?P<q>.+)$", t)
    if m:
        return "web_search", {"query": _clean_query(m.group("q")), "site": _site(m.group("s"))}
    return None


def route_all(text: str, settings: Settings, find_app=None) -> list[tuple[str, dict]] | None:
    """Split 'open chrome and then open whatsapp' into steps; every step must be understood."""
    if HEAR_ME.match(text.strip()):
        return [("say", {"text": random.choice(HEAR_ME_REPLIES)})]
    if CAPABILITIES_Q.match(clean_command(text)):
        return [("say", {"text": CAPABILITIES})]
    if BRIEFING_Q.match(re.sub(r"[.!?,]+", "", text.lower()).strip()) or BRIEFING_Q.match(clean_command(text)):
        return [("briefing", {})]
    mem = route_memory(clean_command(text))
    if mem:
        if mem[0][1].get("text"):               # keep your own capitals ("Friday", "Rahul"): the router lowercases
            low = mem[0][1]["text"]
            i = text.lower().find(low)
            if i >= 0:
                mem[0][1]["text"] = text[i:i + len(low)]
        return mem
    if CLICK_TYPE_Q.match(clean_command(text)):
        return [("say", {"text": CLICK_TYPE})]
    steps = _route_all(text, settings, find_app)
    if steps is None:
        # "Jarvis, can you hear me? Just click on the first video." → the command is in the last sentence.
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if re.search(r"[a-z]", s, re.I)]
        if len(sentences) > 1:
            steps = _route_all(sentences[-1], settings, find_app)
    if steps is None or any(s[0] == "clarify_open" for s in steps):
        # "open the calculator and then tell me a joke" / "what's 5 plus 5 and also open notepad":
        # do the command part, and answer the question part (as a "chat" step).
        t = clean_command(text)
        for m in re.finditer(r"\s*,?\s+\b(?:and then|and also|and|then|also)\s+", t):
            a, b = t[:m.start()].strip(), t[m.end():].strip()
            for cmd, chat, order in ((a, b, 1), (b, a, -1)):
                if not _CHAT_PART.match(chat) and not maybe_math(chat):
                    continue
                done = _route_all(cmd, settings, find_app)
                if done and all(s[0] not in ("clarify_open", "say", "ask_verb") for s in done):
                    return done + [("chat", {"text": chat})] if order == 1 else [("chat", {"text": chat})] + done
    return steps


_FAILED = re.compile(r"^(?:I can't|I couldn't|JARVIS's Edge window isn't|Browser control isn't|Sorry, that didn't work|"
                     r"WhatsApp isn't linked|'.+' isn't one of|.+ isn't on your approved list|There's no video)", re.I)
_CHAT_PART = re.compile(r"^(?:tell me|what|what's|whats|how|why|who|when|where|explain|give me|can you tell|do you know|"
                        r"is it|are you|calculate)\b")


HEAR_ME = re.compile(r"^\W*(?:(?:hey|hello|hi|okay|ok|so)\W+)*(?:jarvis\W+)?(?:hello\W+)?(?:can you hear me|are you (?:still )?there|"
                     r"are you listening|you there|do you hear me)(?:\s+(?:jarvis|now|properly|clearly))?\W*$", re.I)
# "What can you do?" — the 3B model overclaims ("basically everything you can do"), so this answer is fixed.
CAPABILITIES_Q = re.compile(r"^(?:so\s+)?(?:what (?:all )?(?:can|could) you (?:actually |really |even )?do|what are you (?:capable of|able to do)|"
                            r"what do you do|how can you help(?: me)?|what are your (?:features|skills|abilities))(?: for me)?(?: here)?$")
CAPABILITIES = ("I can open your apps, websites, files and folders, and search Google, YouTube or Spotify. I can click, type, "
                "press keys and scroll in your apps, play or pause music and videos, change the volume and screen "
                "brightness, and sum up web pages. I can set reminders and timers, give you a morning briefing, remember "
                "things about you, do quick maths and tell the time anywhere. Or we can just chat.")
HEAR_ME_REPLIES = ["Yep, I can hear you.", "Loud and clear.", "Yes, I'm here."]
# Sounds like it wants something done (only these are worth a trip to the cloud interpreter).
_COMMANDISH = re.compile(r"\b(?:open\w*|launch\w*|start\w*|play\w*|put on|search\w*|look up|google|find|click\w*|tap\w*|"
                         r"press\w*|typ(?:e|ing)|writ(?:e|ing)|message|text|send|tell|ask|whatsapp|remind\w*|timer|alarm|"
                         r"paus\w*|stop|skip\w*|next|previous|resum\w*|mut\w*|scroll\w*|go back|go down|go up|close|tab|"
                         r"spotify|youtube|song|music|video\w*)\b", re.I)
INTERPRET_PROMPT = """You turn what someone said to their voice assistant into ONE plain command it understands.
Speech-to-text often mishears words, so guess sensibly from context (e.g. "watch up" = WhatsApp).

Reply with exactly one line and nothing else:
- the command(s), using only these phrasings (join several with " and then "):
  open <app or website> | open <file or folder name> | search <site> for <words> | search for <words>
  play <song or artist> on youtube | play <song or artist> on spotify | search for <words> and play it
  click <thing> | click <thing> in <app> | click the first video | click the second video | click the most viewed video
  type <text> | type <text> and press enter | type <text> in <app> | press enter | press escape | scroll down | scroll up
  pause | play | next video | mute | pause spotify | next song on spotify | go back | new tab | close the tab
  keep scrolling | stop scrolling | type <value> in the <field name> field | select <option> from the <field> list
  tick the <label> checkbox | do this in the browser: <a multi-step web task in their words, e.g. fill the form with …>
  message <name> on whatsapp that <message> | remind me to <task> in <time> | remind me to <task> at <time>
  set a timer for <n> minutes | what time is it in <city> | lock my computer
  set the volume to <n> | turn the volume up | turn the volume down | mute the laptop | unmute the laptop
  set the brightness to <n> | brightness up | brightness down | good morning (a briefing of their day)
- CHAT if it's a question, a story, small talk or anything that isn't asking for an action
- UNCLEAR: <one short question> if it wants an action but you can't tell what

For "the most popular / most viewed / most liked one" on YouTube use: search youtube for <words> and then click the most viewed video
Never add things they didn't ask for. Keep names and message text as they said them.
Their apps: {apps}
Recent conversation:
{recent}"""
# Said right after a scroll / skip / key press: do the same thing again.
_REPEAT = re.compile(r"^(?:do (?:that|it|this|the same)(?: again| one more time)?|again|(?:one )?more(?: time)?|once more|"
                     r"keep going|keep it going|continue|go on|more please|same again|another one|"
                     r"(?:a little|a bit|some) more)$")
_REPEATABLE = {"browser_scroll", "browser_key", "browser_nav"}
AGENT_PROMPT = """You control a web browser for the person you help, like a careful human with a mouse and keyboard.
Their task: {goal}

You know nothing about them except what their task says (never invent or guess any personal detail):
{known}

Done so far:
{done}

Current page: {title} | {url}
Page text (start): {text}

Elements you can act on, by number:
{elements}

Reply with JSON only:
{{"actions": [ ... up to 6 ... ], "status": "continue" | "done" | "ask", "say": "one short spoken sentence"}}
Actions: {{"do": "fill", "id": N, "value": "..."}} | {{"do": "select", "id": N, "value": "option text"}} |
{{"do": "check", "id": N, "value": true}} | {{"do": "click", "id": N}} | {{"do": "scroll"}} | {{"do": "open", "url": "..."}}
Rules:
- Only use details from their task or from what you know. If a required field needs something you don't have
  (email, phone, address, password, date of birth…), use status "ask" and ask for exactly what's missing.
- Never fill passwords, PINs, OTPs, card numbers, CVV, Aadhaar, PAN or bank details: the system refuses them anyway.
  Leave those fields, and in "say" tell them which ones they need to type themselves.
- Never enter personal details on a page that looks like a scam, a fake login or a prize/lottery page: status "done"
  and warn them instead.
- Put the final submit/send/pay/sign-up/order click LAST; the system will ask them before pressing it.
- If the page shows the task is already finished, use status "done" and say what happened.
- If something is in the way (cookie banner, popup), close or accept the minimal option first."""

_FINAL_BUTTON = re.compile(r"\b(?:submit|send|pay|buy|purchase|order|checkout|check out|place|confirm|sign ?up|register|"
                           r"create account|book|subscribe|post|publish|delete|remove|apply|transfer|donate|log ?in|sign ?in)\b", re.I)


def _is_final_button(label: str) -> bool:
    return bool(_FINAL_BUTTON.search(label or ""))


def _describe_element(e: dict) -> str:
    kind = e.get("type") or e.get("tag")
    bits = [f"[{e['id']}] {e.get('kind')} {kind}: \"{e.get('label', '')}\""]
    if e.get("value"):
        bits.append(f"value=\"{e['value']}\"")
    if "checked" in e:
        bits.append(f"checked={e['checked']}")
    if e.get("options"):
        bits.append("options=" + "|".join(e["options"][:12]))
    if e.get("required"):
        bits.append("required")
    if e.get("disabled"):
        bits.append("disabled")
    return " ".join(bits)


def _parse_json(text: str | None) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


_SEARCH_OFFER = re.compile(r"\b(?:want|shall|should|would you like) (?:me to|i) (?:search|look (?:it|that) up|google)\b", re.I)
# "Can't you do clicks and typing?" — the model answered "I don't have a mouse or keyboard", which is wrong.
_CT = r"(?:clicks?|clicking|typ(?:e|ing)|scroll(?:s|ing)?|press(?:ing)?\s+(?:buttons?|keys?|enter))"
CLICK_TYPE_Q = re.compile(rf"^(?:(?:can'?t|cannot|can|could|don'?t|do|are|aren'?t|will|won'?t)\s+you\s+|you\s+(?:can'?t|cannot|can)\s+)"
                          rf"(?:even\s+|actually\s+|really\s+|not\s+)?(?:do\s+|able\s+to\s+|be\s+able\s+to\s+)?(?:any\s+)?{_CT}"
                          rf"(?:\s*(?:,|and|or|&)\s*{_CT})*(?:\s+(?:things|stuff|for me|at all|on (?:the )?(?:screen|page|browser)))?$|"
                          rf"^(?:do|doing)\s+{_CT}(?:\s+(?:and|or)\s+{_CT})*$|"
                          rf"^{_CT}(?:\s*(?:,|and|or|&)\s*{_CT})+$|"      # "(can you) click and type"
                          rf"^{_CT}\s+(?:in|on|into)\s+(?:a|an|any|the|other)?\s*(?:websites?|apps?|pages?|browsers?|"
                          rf"windows?|programs?|text\s*box(?:es)?|boxes|things|stuff|everything|anything)$")  # "can you type in a website?"
CLICK_TYPE = ("Yes. I can click buttons and links, type, press Enter and scroll, in my Edge window and in your other apps. "
              "Just tell me straight out, like “click the first video”, “click play in Spotify” or “type hello in "
              "Notepad”. I won't type in terminals or password boxes, and I ask before anything like Send, Buy or Delete.")


BRIEFING_Q = re.compile(r"^(?:good morning|morning|gm)(?:\s+(?:jarvis|buddy|bro|mate|man))?$|^(?:brief me|give me (?:a|my|the) "
                        r"(?:daily |morning )?(?:briefing|rundown|update)|(?:daily|morning) (?:briefing|update|rundown)|"
                        r"what(?:'s| is| do i have)?\s+(?:on\s+)?(?:for\s+)?(?:my|the)\s+(?:day|plan|plans|schedule)(?:\s+(?:for\s+)?today)?|"
                        r"what(?:'s| is) (?:on )?(?:for )?today|what do i have (?:on )?today|what'?s up for today)$")


def route_memory(t: str) -> list[tuple[str, dict]] | None:
    """'remember that my exam is on Friday', 'what do you remember about me', 'forget that…', 'forget everything about me'."""
    if re.match(r"^(?:forget|delete|erase|wipe|clear)\s+(?:everything|all(?: of it)?|all that)\s+(?:you know\s+|you remember\s+)?"
                r"about me$|^(?:clear|wipe|erase|delete|reset)\s+(?:your|the|my)\s+memory$", t):
        return [("forget_all", {})]
    if re.match(r"^(?:what|what all|what else|tell me what)\s+(?:do|did)\s+you\s+(?:remember|know|recall)\s+about\s+me$|"
                r"^what\s+(?:have\s+you|did\s+you)\s+(?:remembered|saved|learn(?:ed|t))(?:\s+about\s+me)?$|"
                r"^do\s+you\s+remember\s+(?:me|anything\s+about\s+me)$|^(?:show|list|read)\s+(?:me\s+)?your\s+memory$", t):
        return [("recall", {})]
    m = re.match(r"^forget\s+(?:that|about|the fact that)\s+(?P<x>.{3,})$", t)
    if m and m.group("x").strip() not in ("it", "that", "this", "everything"):
        return [("forget", {"text": m.group("x").strip()})]
    m = re.match(r"^(?:remember|note|keep in mind|don'?t forget)\s+(?:that\s+)?(?P<x>(?!to\b|me\b|it\b|this\b).{3,})$", t)
    if m:
        return [("remember", {"text": m.group("x").strip()})]
    return None


def _about_user(fact: str) -> str:
    """'my exam is on Friday' → 'Their exam is on Friday' (facts are stored in the third person)."""
    swaps = [(r"\bi'm\b", "they're"), (r"\bi am\b", "they are"), (r"\bi've\b", "they've"), (r"\bi was\b", "they were"),
             (r"\bi\b", "they"), (r"\bmy\b", "their"), (r"\bme\b", "them"), (r"\bmine\b", "theirs"), (r"\bmyself\b", "themselves")]
    out = fact
    for a, b in swaps:
        out = re.sub(a, b, out, flags=re.I)
    return out


def _to_you(fact: str) -> str:
    """Stored 'Their exam is on Friday' → spoken 'Your exam is on Friday'."""
    swaps = [(r"\bthey're\b", "you're"), (r"\bthey are\b", "you are"), (r"\bthey've\b", "you've"),
             (r"\bthey were\b", "you were"), (r"\bthemselves\b", "yourself"), (r"\btheirs\b", "yours"),
             (r"\btheir\b", "your"), (r"\bthem\b", "you"), (r"\bthey\b", "you"), (r"\bthe user'?s\b", "your"),
             (r"\bthe user\b", "you")]
    out = fact
    for a, b in swaps:
        out = re.sub(a, lambda m, b=b: b.capitalize() if m.group(0)[0].isupper() else b, out, flags=re.I)
    return out


_GENERIC_MUSIC = re.compile(r"^(?:(?:a|any|some)\s+)?(?:random\s+|good\s+|nice\s+)?(?:songs?|music|tracks?|something)$")


def route_play_media(t: str) -> list[tuple[str, dict]] | None:
    """'search for lofi and play it' / 'play lofi on spotify' / 'play a random song in spotify': search, then actually
    start the top result (YouTube: first video; Spotify: the top result's play button)."""
    m = re.match(r"^(?:search|look up|find)\s+(?:for\s+)?(?P<q>.+?)\s+and\s+(?:then\s+)?play(?:\s+(?:it|that|one|the (?:first|top) one))?"
                 r"(?:\s+(?:on|in)\s+(?:the\s+)?(?P<s>youtube|you tube|spotify|edge|e|the browser))?$", t) or \
        re.match(r"^(?:play|put on)\s+(?:me\s+)?(?:some\s+)?(?P<q>.+?)\s+(?:on|in)\s+(?:the\s+)?(?P<s>spotify)$", t)
    if not m:
        return None
    q, s = _clean_query(m.group("q")), (m.group("s") or "").replace("you tube", "youtube")
    site = {"spotify": "spotify web", "youtube": "youtube"}.get(s)
    if _GENERIC_MUSIC.match(q):
        q = "today's top hits" if site == "spotify web" else "songs"
    return [("web_search", {"query": q, "site": site, "media": True}), ("browser_media", {"action": "play_result"})]


_WA_WHO = r"(?P<who>(?!(?:him|her|them|it|me)\b)[a-z]+(?:\s+(?!saying|that|on|in|to|and)[a-z]+)?)"


def route_whatsapp_message(t: str) -> list[tuple[str, dict]] | None:
    """'message rahul on whatsapp that I'm coming' → open WhatsApp, open Rahul's chat, type it, then ask before sending."""
    if not re.search(r"\b(?:whatsapp|whats app|watch up|watsapp)\b", t):
        return None
    t = re.sub(r"\b(?:whats app|watch up|watsapp)\b", "whatsapp", t)
    t = re.sub(r"^open\s+(?:the\s+)?whatsapp\s+(?:and\s+(?:then\s+)?)?", "", t)
    wa = r"(?:\s+(?:on|in|via|over|through)\s+whatsapp)"
    m = re.match(rf"^(?:send|message|text|ping|whatsapp)\s+(?:a\s+message\s+to\s+|to\s+)?{_WA_WHO}{wa}?\s*,?\s+"
                 r"(?:saying|that|to say|and say|and tell (?:him|her|them))\s+(?P<msg>.+)$", t) or \
        re.match(rf"^(?:send|say|text)\s+(?P<msg>.+?)\s+to\s+{_WA_WHO}{wa}?$", t) or \
        re.match(rf"^(?:tell|ask)\s+{_WA_WHO}{wa}\s+(?:that\s+)?(?P<msg>.+)$", t)
    if not m:
        return None
    msg = re.sub(r"\bi(?=\b|')", "I", m.group("msg").strip(" '\"“”"))
    return [("open_website", {"url": "whatsapp"}), ("browser_click", {"target": m.group("who")}),
            ("browser_type", {"text": msg[0].upper() + msg[1:] if msg else msg, "submit": True})]


def _route_all(text: str, settings: Settings, find_app=None) -> list[tuple[str, dict]] | None:
    t = clean_command(text)
    wa = route_whatsapp_message(t)
    if wa:
        return wa
    media = route_play_media(t)          # before the "and play…" split below would cut it in two
    if media:
        return media
    # Split only before a verb that has something after it: "type lofi music and search" stays one step.
    parts = [p for p in re.split(rf"(?:\s*,\s*(?:and\s+|then\s+)*|\s*\b(?:and then|and also|and|then|also)\s+)"
                                 rf"(?=(?:{OPEN_VERB}|search|remind|play|type|click|fill|select|choose|tick|scroll|"
                                 rf"keep scrolling|sign up)\b\s*\S)", t) if p]
    routed: list = []
    for p in parts:
        steps = route_browser(p) or [route_intent(p, settings, find_app)]
        routed.extend(steps)
    if not routed or not all(routed):
        return None
    # "open chrome and open whatsapp" → just open WhatsApp in Chrome (one window, not two).
    merged: list[tuple[str, dict]] = []
    for step in routed:
        prev = merged[-1] if merged else None
        if prev and prev[0] == "open_app" and _browser(prev[1].get("name")) and step[0] in ("open_website", "web_search") \
                and not step[1].get("browser"):
            merged[-1] = (step[0], {**step[1], "browser": _browser(prev[1]["name"])})
        elif prev and prev[0] == "open_website" and step[0] == "web_search" and not step[1].get("site") \
                and prev[1].get("url") in SEARCH_SITES:
            # "open YouTube and search for X" → search on YouTube, not Google
            merged[-1] = ("web_search", {**step[1], "site": prev[1]["url"], "browser": step[1].get("browser") or prev[1].get("browser")})
        elif prev and prev[0] == "web_search" and step == ("browser_media", {"action": "play"}):
            # "search for X and play it" split in two: a results page has nothing playing yet → start the top result
            merged[-1] = ("web_search", {**prev[1], "media": True})
            merged.append(("browser_media", {"action": "play_result"}))
        else:
            merged.append(step)
    return merged


def _command_start(t: str, m: re.Match) -> bool:
    """A command verb found mid-sentence doesn't count when what comes before makes it a story or a question
    ("I was trying to open the jar", "explain what open source means", "I told him to search for it")."""
    return not re.search(r"\b(?:i|i'm|i'd|i've|he|she|they|we|my|his|her|our|their|trying|tried|try|explain|what|how|why|"
                         r"about|means?|when|if|because|told|asked|tells?|said|says|wants?|wanted)\b", t[:m.start()])


def route_intent(text: str, settings: Settings, find_app=None) -> tuple[str, dict] | None:
    """Fast, deterministic command recognition so a small CPU model never has to pick tools."""
    from .tools import resolve_web_app
    t = clean_command(text)
    if re.search(r"\b(start (?:a )?new conversation|new conversation|forget (?:everything|all of (?:it|that)|that|the past)|"
                 r"reset (?:the )?(?:conversation|chat)|start over|clear (?:the )?(?:chat|conversation))\b", t):
        return "reset_conversation", {}
    site_search = route_site_search(t)
    if site_search:
        return site_search
    wt = world_time(t)
    if wt:
        return "say", {"text": wt}
    m = re.search(r"\b(?:set|start|put|create)\s+(?:a\s+|an\s+)?(?:timer|alarm)\s+(?:for\s+)?(?P<n>.+)$", t)
    if m:
        n = m.group("n").strip()
        clock = re.search(r"\d\s*(?:am|pm|a\.m\.|p\.m\.)|\d:\d|\b(?:morning|evening|tonight|o'?clock)\b", n)
        return "create_reminder", {"text": "your timer's up" if "timer" in t else "your alarm",
                                   "when": n if re.match(r"^(?:in|at|tomorrow)\b", n) else f"at {n}" if clock else f"in {n}"}
    for pattern, reply, offer in CANT_DO:
        if re.search(pattern, t) and not re.match(r"^(?:search|google|look up)\b", t):
            reply, offer = _cant_offer(t, reply, offer)
            return "cant", {"text": reply, "offer": offer}
    if re.search(r"\b(what('s| is)? (the )?(time|date)|time is it|what day is (it|today)|today's date)\b", t) \
            and not re.search(r"\b(difference|between|zone|in [a-z]+(?: [a-z]+)?$|at [a-z]+$)\b", t):
        return "get_time", {}
    if re.search(r"^summar\w*$|\bsummar\w*\s+(?:(?:our|this|the)\s+(?:conversation|chat|discussion)|what we\b)", t):
        return "summarize_conversation", {}
    if re.search(r"\b(what are|list|show|read)( me)? (my )?(upcoming )?reminders\b|\bany reminders\b", t):
        return "list_reminders", {}
    m = re.search(r"\bremind me (?:to |about )?(?P<text>.+?)\s+(?P<when>(?:in|at|tomorrow)\b.+)$", t) or \
        re.search(r"\bremind me (?P<when>(?:in|at|tomorrow)\b.*?) (?:to |about )(?P<text>.+)$", t)
    if m and m.group("when").strip() in ("tomorrow", "in", "at"):
        return "ask_reminder_time", {"text": f"{m.group('text')} tomorrow"}
    if m:
        return "create_reminder", {"text": m.group("text"), "when": m.group("when").strip()}
    m = re.search(r"\bremind me (?:to |about )?(?P<text>.+)$", t)
    if m:  # no time given: ask for it
        return "ask_reminder_time", {"text": m.group("text")}
    if "lock screen" in settings.get("allowed_commands") and \
            re.search(r"^lock\s+(?:my\s+|the\s+|this\s+)?(?:computer|laptop|pc|system|windows|machine|screen)(?:\s+now)?$", t):
        return "run_command", {"name": "lock screen"}
    for name in settings.get("allowed_commands"):
        words = r"\s+(?:my\s+|the\s+)?".join(re.escape(w) for w in name.split())
        if re.search(rf"\b{words}\b", t) and re.search(r"\b(run|check|what|show|tell|do|lock|flush|my|get|find)\b", t):
            return "run_command", {"name": name}
    m = re.search(r"\b(?:find|search for|look for|locate)\s+(?:a |the |my )?(?:files?|documents?)\s*(?:called|named|about|with)?\s+(?P<q>.+)$", t)
    m = m or re.search(r"\b(?:do i have|is there|are there|have i got|have you got)\s+(?:a|an|any|some)\s+"
                       r"(?P<k>(?:files?|documents?|pdfs?|folders?|word (?:files?|documents?)|photos?|images?))\s+"
                       r"(?:called|named|about|with|for)\s+(?P<q>.+?)(?:\s+(?:on|in) (?:my|the) (?:laptop|computer|pc|desktop))?$", t)
    if m:  # "do I have any pdf named gencv" — a 3B model just makes up an answer
        kind = re.sub(r"s$", "", (m.groupdict().get("k") or "").split()[0]) if m.groupdict().get("k") else ""
        kind = {"pdf": "pdf", "folder": "folder", "word": "word", "photo": "photo", "image": "image"}.get(kind, "")
        return "search_files", {"query": f"{m.group('q').strip(' ' + chr(39) + chr(34))} {kind}".strip()}

    # "open whatsapp in chrome", "open up of watch up web in the chrome itself"
    m = re.search(rf"\b{OPEN_VERB}\s+(?:(?:the|my|of|a|up)\s+)*(?P<x>.+?)\s+(?:in|on|with|using|inside|from)\s+(?:(?:the|a|my)\s+)?"
                  rf"(?P<b>{BROWSERS})(?:\s+(?:browser|itself|please|now|app))*$", t)
    if m and _command_start(t, m):
        x, browser = m.group("x").strip(" '\""), _browser(m.group("b"))
        web = resolve_web_app(x)
        if web or ("." in x and " " not in x):
            return "open_website", {"url": web or x, "browser": browser}
        return "web_search", {"query": x, "browser": browser}

    m = re.search(rf"^(?:search(?:\s+the\s+web)?(?:\s+for)?|google|look\s+up)\s+(?P<q>.+?)(?:\s+(?:in|on|using)\s+(?P<b>{BROWSERS}))?$", t)
    if m:
        return "web_search", {"query": m.group("q"), "browser": _browser(m.group("b"))}

    m = re.search(rf"\b(?P<verb>{OPEN_VERB})\s+(?:(?:the|my|of)\s+)?(?P<x>.+?)"
                  r"(?:\s+(?:app|application|website|site|for me|please|now))*$", t)
    # "How do I open a bank account?", "I think I'll open up to my friend" are conversation, not commands.
    if m and (QUESTION_START.match(t) or re.search(r"\bopen(?:ing)? up to\b|\bopen (?:a |an )?(?:bank |new )?account\b", t)
              or not _command_start(t, m)):
        m = None  # "I was trying to open the jar…", "can you explain what open source means" are chat
    if m:
        verb, target = m.group("verb"), m.group("x").strip(" '\"")
        if target in BROWSER_WORDS:
            return "open_app", {"name": "edge"}
        file_words = re.search(r"\b(file|folder|document|pdf|docx?|resume|cv|photos?|notes?|image|images)\b", target)
        short = len(target.split()) <= 3
        exact_app = find_app(target) if find_app and short else None
        if exact_app and file_words and (exact_app[0] == normalize_name(target) or
                                         (normalize_name(target) in APP_ALIASES and "note" not in target)):
            return "open_app", {"name": target}  # "open file explorer" is an app even though it says "file"
        if not file_words:
            app = find_app(target) if find_app and short else None
            web = resolve_web_app(target)
            # An installed app with that exact name wins; otherwise a known web app beats a partial app match.
            if app and (app[0] == normalize_name(target) or not web):
                return "open_app", {"name": target}
            if web:
                return "open_website", {"url": web}
            if not find_app and short and any(target == a or a in target for a in settings.get("approved_apps")):
                return "open_app", {"name": target}
            if "." in target and " " not in target or \
                    target.replace(" ", "") in {s.split(".")[0] for s in settings.get("approved_sites")}:
                return "open_website", {"url": target if "." in target else target.replace(" ", "") + ".com"}
        # Anything else after an explicit "open X" is looked up as a file or folder (and confirmed before opening).
        sentence_like = re.search(r"\b(to|into|about|with|for|on|door|window|you|that|i|can't|cannot)\b", target) and not file_words
        if verb.startswith(("open", "show me", "pull up")) and len(target.split()) <= 6 and not sentence_like \
                and not re.match(r"^(a|an|up|it|that|this)\b", target):
            return "open_file", {"query": target}
        if not file_words and len(target.split()) > 3 and verb.startswith("open"):
            return "clarify_open", {"text": target}
    return None


_ORDINALS = {"first": 0, "1": 0, "1st": 0, "second": 1, "two": 1, "2": 1, "2nd": 1,
             "third": 2, "three": 2, "3": 2, "3rd": 2, "last": -1}


def pick_choice(text: str, names: list[str]) -> int | None:
    """'the second one' → 1, 'number 3' → 2, 'the PDF one'/'GenCV' → index of the matching name."""
    t = text.lower()
    # "not the first one, the second" → ignore what was ruled out.
    t = re.sub(r"\b(?:not|isn'?t|wasn'?t)\s+(?:the\s+|that\s+)?\w+(?:\s+one)?", " ", t)
    for word, idx in sorted(_ORDINALS.items(), key=lambda kv: (m.start() if (m := re.search(rf"\b{re.escape(kv[0])}\b", t)) else 1e9)):
        if re.search(rf"\b{re.escape(word)}\b", t):
            idx = len(names) - 1 if idx == -1 else idx
            if idx < len(names):
                return idx
    words = [w for w in re.findall(r"[a-z0-9]+", t) if len(w) > 2 and w not in ("the", "one", "open", "that", "file", "please",
                                                                             "it's", "its", "was", "and")]
    scores = [sum(w in name.lower() for w in words) for name in names]
    best = max(scores, default=0)
    if best and scores.count(best) == 1:  # only a name that matches better than every other one
        return scores.index(best)
    return None


_INCOMPLETE = re.compile(r"\b(and|but|so|or|because|um+|uh+|like|the|a|to|with|then|if|of)\W*$", re.I)


def looks_incomplete(text: str) -> bool:
    t = text.strip()
    return bool(t) and (t.endswith((",", "...", "…", "-")) or bool(_INCOMPLETE.search(t)))


def speakable(text: str) -> str:
    text = re.sub(r"^\s*(?:assistant|jarvis)\s*[:\-]?\s+(?=[A-Z])", "", text)  # leaked role labels
    text = re.sub(r"^[\s.,;:!?\-–—]+", "", text)                                # stray leading punctuation
    text = re.sub(r"[*_#`>|]+", "", text)
    text = re.sub(r"(\.\s*){2,}|…+", ". ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([.,!?])", r"\1", text).strip()
    return text if re.search(r"[A-Za-z0-9]", text) else ""


class SentenceChunker:
    """Splits a token stream into speakable pieces; the first piece may break at a comma for lower latency."""

    def __init__(self):
        self.buf = ""
        self.first = True

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out = []
        while True:
            m = re.search(r"[.!?](\s)", self.buf)
            if not m and self.first and len(self.buf) > 28:
                # Break at a comma only once the phrase is long enough to sound natural on its own.
                # …and only once a few words follow it, so the second piece isn't a lonely "personally."
                m = next((c for c in re.finditer(r"[,;:—](\s)", self.buf)
                          if c.start() >= 24 and len(self.buf[c.end():].split()) >= 3), None)
            if not m:
                break
            piece, self.buf = self.buf[:m.end()], self.buf[m.end():]
            if speakable(piece):
                out.append(speakable(piece))
                self.first = False
        return out

    def flush(self) -> list[str]:
        piece, self.buf = speakable(self.buf), ""
        return [piece] if piece else []


def _part_of_day() -> str:
    # Coarse on purpose: the system prompt only changes a few times a day, so Ollama's prompt cache stays warm.
    h = datetime.now().hour
    return "morning" if 5 <= h < 12 else "afternoon" if h < 17 else "evening" if h < 22 else "night"


# A few short exchanges that show the model how to sound. They live inside the system prompt (cached by Ollama) and
# are labelled as made up: as real chat turns, the model took them for things the user had said ("what's got you
# feeling tired today?").
STYLE_EXAMPLES = [
    ("hey how's it going", "Pretty good, thanks! What are you up to?"),
    ("which is better for a beginner, python or java",
     "I'd go with Python. The syntax is cleaner, so you spend your energy on learning to think like a programmer "
     "instead of fighting the language. Java's great later, especially for Android or big backend jobs."),
    ("why is the sky blue",
     "Sunlight has every colour in it, but the air scatters the short blue wavelengths far more than red ones. "
     "So wherever you look, scattered blue light is coming at you."),
    ("can you check my instagram messages",
     "I can't read your messages, sorry. I can open Instagram in my Edge window though, and you can take it from there."),
    ("i'm so tired today", "Ugh, those days are rough. Did you sleep badly, or has it just been a long one?"),
]


def _model_name(model: str) -> str:
    """'llama3.2:3b' → "Meta's Llama 3.2 model" (said aloud, not spelled out like a tag)."""
    m = re.match(r"^llama\s*(\d+(?:\.\d+)?)", model or "", re.I)
    if m:
        return f"Meta's Llama {m.group(1)} model"
    return f"the {re.split(r'[:]', model or 'local')[0]} model"


def _self_description(settings: Settings) -> str:
    """What JARVIS says it runs on. It must stay true: with a Groq key, chat and voice go to Groq's cloud."""
    from .cloud import groq_key
    local = _model_name(settings.get("llm_model"))
    if settings.get("cloud_ai") and groq_key():
        return (f"you're JARVIS Zero. Your thinking and voice use Groq's free cloud models, with {local} on this laptop as "
                f"the backup when the internet or the free limit runs out. Everything you do on the computer happens "
                f"on this laptop.")
    return f"you're JARVIS Zero, running {local} locally on this laptop, so nothing goes online."


def system_prompt(settings: Settings, channel: str) -> str:
    persona = PERSONALITIES.get(settings.get("personality"), PERSONALITIES["human"])
    user = settings.get("user_name") or "the user"
    base = f"""{persona}
You are talking with {user} by voice{' over a phone call' if channel == 'phone' else ' through their laptop'}. Everything you write is spoken aloud.
How to talk:
- Sound like a real person in a conversation, not an assistant reading a script. Use contractions and everyday words.
- React to what they actually said first when it fits, in your own words (don't reuse the same opener), then answer.
- Usually one to three short sentences. Give more only when they ask you to explain.
- When a question needs thinking (why, how, which is better, should I, a plan or a calculation), work it out, then
  give the answer with the one key reason. Don't lecture.
- Remember what was said earlier in this conversation and use it.
- Ask a short follow-up question now and then, like a friend would, but not every time.
- Never call them "sir" or "madam". No stock phrases like "Certainly!", "Great question!", "How can I assist you today?".
- No lists, markdown, emojis or code. Say numbers and times the way people say them.
- If you didn't understand, say so plainly and ask them to repeat or rephrase.
- If you're not sure about a fact or a number, say you're not certain. Never claim to be a reliable source.
- You are an AI. If asked, say so plainly. Never claim to be human or to have a body, feelings of hunger, etc.
- Only when asked what you are or who made you: {_self_description(settings)} Don't invent other makers or version
  numbers. Don't introduce yourself otherwise.
- When asked which is better or what you think, pick one and give one reason. Don't hide behind "I don't have opinions".
- Never say "old chap", "sir", "parameters", "as an AI language model" or "I'm just a chat window", and never call yourself text-based: you talk by voice.
- Never state the current time, weather, news, prices or scores yourself, and never claim something happened on the computer.
- Today is {datetime.now().strftime('%A %d %B %Y')}, in the {_part_of_day()}. Don't announce the date unless asked."""
    if channel == "local":
        base += """
- Use a tool only when the user asks you to do something on the computer (open, remind, search, run, time). For chat, just talk.
- After a tool runs, tell the user the outcome in one short sentence.
- You cannot see the screen, read messages, browse, or use apps' contents (WhatsApp, email and so on). Never say you did
  something unless the system just told you it happened. If asked for something you can't do, say so briefly and mention
  what you can do: open apps, websites, files and folders, search Google or YouTube, and in your own Edge window type,
  click, scroll, pause videos and summarize the page, set reminders, tell the time. Those happen when the user asks
  directly (for example “type hello”, “click the first video”); in chat you can't do them."""
    else:
        base += (f"\n- On calls you can only talk and take messages. You cannot change calendars, send messages, set reminders "
                 f"or do anything on the computer. Never say you did any of these."
                 f"\n- If the caller leaves a message, repeat the key point back and say you'll pass it on to {user}.")
    base += "\nExamples of how you sound (made up, not part of this conversation, never mention them):\n" + \
        "\n".join(f'They say: "{u}" You say: "{r}"' for u, r in STYLE_EXAMPLES if channel == "local" or "Edge" not in r)
    return base


_REASONING = re.compile(r"(?:^|[,.?!]\s*|\b(?:and|but|so|okay|then)\s+)why\b|\b(how (?:do|does|can|should|would|to|come)|which (?:is|one|would)|better|worse|should i|"
                        r"compare|difference between|explain|what if|pros and cons|plan|advice|recommend|best way|"
                        r"is it (?:good|bad|worth|safe))\b", re.I)
REASONING_NOTE = ("Think this through quietly first. Then answer like a friend would, in two or three natural spoken "
                  "sentences: your answer and the main reason, in your own words. Don't use phrases like 'the key reason "
                  "is' or 'I'm thinking that's because'.")


def needs_reasoning(text: str) -> bool:
    return bool(_REASONING.search(text))


# ---------- exact arithmetic (a 3B model is unreliable at sums) ----------
_NUM_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
              "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
              "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
              "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
              "thousand": 1000, "lakh": 100000, "million": 1000000, "crore": 10000000}


def _words_to_numbers(t: str) -> str:
    def repl(m: re.Match) -> str:
        total, cur = 0, 0
        for w in m.group(0).replace("-", " ").split():
            if w == "and":
                continue
            v = _NUM_WORDS[w]
            if v in (100,):
                cur = (cur or 1) * v
            elif v >= 1000:
                total += (cur or 1) * v
                cur = 0
            else:
                cur += v
        return str(total + cur)
    words = "|".join(sorted(_NUM_WORDS, key=len, reverse=True))
    return re.sub(rf"\b(?:{words})(?:[\s-]+(?:and\s+)?(?:{words}))*\b", repl, t)


def maybe_math(text: str) -> str | None:
    """'what's 17 times 23' → 'That's 391.'  '15 percent of 2400' → '15 percent of 2,400 is 360.'"""
    t = _words_to_numbers(text.lower())
    t = t.replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s+of\s+(\d+(?:\.\d+)?)", t)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return f"{_fmt(a)} percent of {_fmt(b)} is {_fmt(a * b / 100)}."
    m = re.search(r"square root of\s+(\d+(?:\.\d+)?)", t)
    if m:
        return f"The square root of {_fmt(float(m.group(1)))} is {_fmt(float(m.group(1)) ** 0.5)}."
    if not re.search(r"\b(what'?s|what is|calculate|how much is|compute|tell me)\b|^\s*[\d(]", t):
        return None
    expr = t
    for word, op in [(r"multiplied by|times|into|x(?=\s*\d)", "*"), (r"divided by|by|over", "/"), (r"plus|\+|added to", "+"),
                     (r"minus|less", "-"), (r"to the power of|power", "**"), (r"squared", "**2"), (r"cubed", "**3")]:
        expr = re.sub(rf"\s*\b(?:{word})\b\s*" if word[0].isalpha() else rf"\s*(?:{word})\s*", f" {op} ", expr)
    m = re.search(r"[\d(][\d\s.+\-*/()]*\d[\s)]*(?:\*\*\s*\d+)?", expr)
    if not m or not re.search(r"[+\-*/]", m.group(0)):
        return None
    try:
        value = _safe_eval(m.group(0))
    except ZeroDivisionError:
        return "You can't divide by zero. It's undefined."
    except (ValueError, SyntaxError, OverflowError):
        return None
    return f"That's {_fmt(value)}."


def _safe_eval(expr: str) -> float:
    import ast
    import operator as op
    ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv, ast.Pow: op.pow,
           ast.USub: op.neg, ast.UAdd: op.pos}

    def ev(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            if isinstance(node.op, ast.Pow) and abs(ev(node.right)) > 50:
                raise ValueError("exponent too large")
            return ops[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.operand))
        raise ValueError("unsupported")
    return ev(ast.parse(expr.strip(), mode="eval").body)


def _fmt(x: float) -> str:
    if abs(x - round(x)) < 1e-9:
        return f"{int(round(x)):,}"
    return f"{x:,.4f}".rstrip("0").rstrip(".")


class Conversation:
    def __init__(self, settings: Settings, llm: LLM, tools: ToolBox | None, channel: str = "local", memory=None):
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.settings = settings
        self.llm = llm
        self.tools = tools
        self.channel = channel
        self.memory = memory if channel == "local" else None   # callers never get your personal memory
        # pick up the thread you were on before a restart (last few exchanges, if recent)
        self.history: list[dict] = self.memory.recent() if self.memory else []
        self._learning: set = set()
        self.last_step: tuple | None = None      # (tool, args, when) of the last repeatable action, for "again"
        self.awaiting_task: tuple | None = None  # a browser task waiting for details you were asked for
        self.pending: ToolResult | None = None
        self.metrics: dict = {}
        self._spoken_in_turn: list[str] = []
        self.last_action: tuple[str, dict] | None = None     # for "no, I meant …"
        self.awaiting_reminder: str | None = None           # "remind me tomorrow to X" → "what time?"
        self._reminder_tomorrow = False
        self.awaiting_verb: str | None = None               # "open" → "what should I open?" → "youtube"
        self._honest_i = 0
        self.reply_kind = "tool"      # what the sentence being spoken is: "chat" (the model) or "tool" (a command)

    # ---- public ----
    def messages(self) -> list[dict]:
        keep = self.settings.get("history_turns") * 2
        prompt = system_prompt(self.settings, self.channel)
        if self.memory and self.settings.get("memory") is not False:
            prompt += self.memory.prompt_block(self.settings.get("user_name") or "the user")
        return [{"role": "system", "content": prompt}] + self.history[-keep:]

    async def reply(self, user_text: str) -> AsyncIterator[str]:
        """Yield speakable sentences as they are generated; afterwards keep the thread, and learn from chat."""
        async for sentence in self._reply(user_text):
            yield sentence
        if self.memory:
            self.memory.save_recent(self.history)
            if self.reply_kind == "chat":
                self._learn_later(user_text)

    def _learn_later(self, user_text: str) -> None:
        """In the background (no delay for you): let the cloud model note lasting facts you just told it."""
        cloud_only = getattr(self.llm, "cloud_only", None)
        if not cloud_only or not self.settings.get("memory_auto", True) or len(user_text.split()) < 4:
            return
        answer = next((m["content"] for m in reversed(self.history) if m["role"] == "assistant"), "")

        async def learn():
            from .memory import EXTRACT_PROMPT, parse_extraction
            known = "\n".join(f"- {f}" for f in self.memory.facts()[-40:]) or "(nothing yet)"
            out = await cloud_only([{"role": "system", "content": EXTRACT_PROMPT.format(known=known)},
                                    {"role": "user", "content": f"USER: {user_text}\nASSISTANT: {answer[:300]}"}],
                                   max_tokens=120)
            add, remove = parse_extraction(out or "")
            for fact in remove:
                self.memory.remove(fact)
            for fact in add:
                self.memory.add(fact, source="learned")
                self._log("tool", f"memory += {fact}")
        try:
            task = asyncio.get_running_loop().create_task(learn())
        except RuntimeError:
            return
        self._learning.add(task)
        task.add_done_callback(self._learning.discard)

    def mark_interrupted(self, spoken: list[str]) -> None:
        """Barge-in: remember only what the user actually heard."""
        for msg in reversed(self.history):
            if msg["role"] == "assistant":
                msg["content"] = (" ".join(spoken) + " —") if spoken else "—"
                break

    async def _reply(self, user_text: str) -> AsyncIterator[str]:
        t0 = time.perf_counter()
        self.metrics = {"llm_first_token": None, "first_sentence": None}
        self.reply_kind = "tool"
        self._log("user", user_text)

        if self.pending and self.pending.choices:  # waiting on "which one?"
            action, self.pending = self.pending, None
            picked = pick_choice(user_text, [name for name, _ in action.choices])
            if picked is not None:
                result = await asyncio.to_thread(action.choices[picked][1])
                self.history.append({"role": "user", "content": user_text})
                for s in self._say_fixed(result.text):
                    yield s
                return
            if NO.search(user_text) or re.search(r"\bnone\b", user_text, re.I):
                self.history.append({"role": "user", "content": user_text})
                for s in self._say_fixed("Okay, never mind."):
                    yield s
                return
            names = " ".join(name.lower() for name, _ in action.choices)
            said = [w for w in re.findall(r"[a-z0-9]{4,}", user_text.lower()) if w not in ("open", "that", "this", "file", "folder")]
            if YES.search(user_text) or (len(user_text.split()) <= 12 and any(w in names for w in said)):
                # "yes" or a name several options share doesn't say which one: keep the question open
                self.pending = action
                self.history.append({"role": "user", "content": user_text})
                options = "first or second" if len(action.choices) == 2 else "first, second or third"
                for s in self._say_fixed(f"Which one? Say {options}."):
                    yield s
                return
            # Anything else: treat as a new request.
        elif self.pending:  # waiting on a yes/no
            action, self.pending = self.pending, None
            if YES.search(user_text):
                result = await asyncio.to_thread(action.run_confirmed)
                while result.needs_confirmation and result.run_confirmed:  # an offer wrapping a confirmable action
                    result = await asyncio.to_thread(result.run_confirmed)
                self.history.append({"role": "user", "content": user_text})
                for s in self._say_fixed(result.text):
                    yield s
                return
            if NO.search(user_text):
                self.history.append({"role": "user", "content": user_text})
                for s in self._say_fixed("Okay, I won't."):
                    yield s
                return
            # Anything else: treat as a new request and drop the pending action.

        self.history.append({"role": "user", "content": user_text})
        steps = None
        if self.tools and self.awaiting_reminder:
            # Answer to "What time?" for a reminder: "at 9", "9 am", "in the evening at 7"
            from .tools import parse_when
            when = re.sub(r"^\W*(?:(?:ok(?:ay)?|so|um+|uh+|maybe|make it|like)\W+)*", "", user_text.strip().lower()).rstrip(".!?")
            cands = (when, f"at {when}", f"tomorrow {when}", f"tomorrow at {when}")
            if self._reminder_tomorrow and "tomorrow" not in when and not when.startswith("in "):
                cands = (f"tomorrow {when}", f"tomorrow at {when}")  # "remind me tomorrow…" → "9 am" means tomorrow 9 AM
            for cand in cands:
                if parse_when(cand):
                    steps = [("create_reminder", {"text": self.awaiting_reminder, "when": cand})]
                    break
            self.awaiting_reminder = None
        if self.tools and self.awaiting_verb and not steps:
            # Answer to "Sure, what should I open?": "youtube" → "open youtube"
            verb, self.awaiting_verb = self.awaiting_verb, None
            if NO.search(user_text) and len(user_text.split()) <= 4:
                for s in self._say_fixed("Okay, never mind."):
                    yield s
                return
            if verb == "type":  # the answer is the text itself, word for word ("hello there" keeps its "hello")
                text = user_text.strip().rstrip(".!?")
                steps = [("browser_type", {"text": text, "submit": False})] if text else None
            else:
                steps = route_all(user_text, self.settings, self.tools.find_app) or \
                    route_all(f"{verb} {clean_command(user_text)}", self.settings, self.tools.find_app)
        m = re.match(r"^\W*(?:no,?\s*|nah,?\s*)?(?:i meant|i mean|i said)\s+(?P<q>.+?)[.!?]*$", user_text.strip(), re.I)
        if not steps and m and self.last_action:
            name, args = self.last_action
            q = m.group("q").strip()
            if name == "web_search":
                steps = [(name, {**args, "query": q})]
            elif name in ("open_app", "open_file"):
                steps = [route_intent(f"open {q}", self.settings, self.tools.find_app) or (name, args)]
        if not steps and self.awaiting_task and not NO.search(user_text):
            # the details the browser task asked for ("my email is …"): carry on where it stopped
            goal, done = self.awaiting_task
            self.awaiting_task = None
            result = await self._browser_task(f"{goal}\nMore details from them: {user_text}", done)
            self._log("tool", f"browser_task(continued) -> {result.text}")
            if result.needs_confirmation:
                self.pending = result
            for s in self._say_fixed(result.confirm_prompt if result.needs_confirmation else result.text):
                yield s
            return
        self.awaiting_task = None
        if not steps and self.tools and self.last_step and time.monotonic() - self.last_step[2] < 120 \
                and _REPEAT.match(clean_command(user_text) or user_text.lower().strip(" .!?")):
            steps = [self.last_step[:2]]     # "keep going" / "do that again" / "more" after a scroll or a skip
        if not steps:
            steps = route_all(user_text, self.settings, self.tools.find_app) if self.tools else None
        if self.tools and (not steps or any(s[0] == "clarify_open" for s in steps)) and _COMMANDISH.search(user_text):
            # The fixed rules didn't get it: let the cloud model restate it in words they understand.
            understood = await self._interpret(user_text)
            if understood and understood.upper().startswith("UNCLEAR"):
                question = understood.split(":", 1)[-1].strip() or "Sorry, what should I do?"
                for s in self._say_fixed(question):
                    yield s
                return
            if understood and understood.upper() == "CHAT" and steps and all(s[0] == "clarify_open" for s in steps):
                steps = None                    # "open your mind a little…": it's talk, not an app to open
            elif understood and understood.upper() != "CHAT":
                again = route_all(understood, self.settings, self.tools.find_app)
                if again and all(s[0] not in ("clarify_open", "chat") for s in again):
                    self._log("tool", f"interpreted({json.dumps(user_text)}) -> {understood}")
                    steps = again
        if steps:
            steps = [(n, {**a, "goal": user_text.strip()} if n == "browser_task" and len(steps) == 1 else a)
                     for n, a in steps]                 # the agent gets your own words, capitals and all
            for i, (name, args) in enumerate(steps):
                if name == "chat":  # the question half of "open notepad and tell me a joke"
                    answer = maybe_math(args["text"])
                    if answer:
                        for s in self._say_fixed(answer):
                            yield s
                    else:
                        note = (f"The computer part of that is already handled, don't mention it. "
                                f"Just reply to this part: {args['text']}")
                        async for s in self._generate(t0, tool_note=note):
                            yield s
                    continue
                if name in ("web_search", "open_app", "open_file", "open_website"):
                    self.last_action = (name, args)
                async for s in self._run_tool(t0, name, args):
                    yield s
                if self.pending:  # a step needs a yes/no or a choice: stop here
                    break
                if i < len(steps) - 1 and _FAILED.match(self.history[-1]["content"]):
                    break  # "I can't see “rahul” on this page." — typing into the wrong chat next would be worse
            return
        answer = maybe_math(user_text)
        if answer:
            self.metrics["llm_first_token"] = self.metrics["first_sentence"] = time.perf_counter() - t0
            for s in self._say_fixed(answer):
                yield s
            return
        use_tools = bool(self.tools) and self.settings.get("llm_tool_calling") and bool(_TOOL_HINT.search(user_text))
        note = REASONING_NOTE if needs_reasoning(user_text) else ""
        async for s in self._generate(t0, tool_note=note, use_tools=use_tools):
            yield s

    async def _interpret(self, user_text: str) -> str | None:
        """Messy speech → one plain command line the router knows, 'CHAT', or 'UNCLEAR: <question>'. Cloud only."""
        cloud_only = getattr(self.llm, "cloud_only", None)
        if not cloud_only or not self.settings.get("cloud_interpret", True):
            return None
        recent = [f"{m['role']}: {m['content'][:200]}" for m in self.history[-7:-1] if m["role"] in ("user", "assistant")]
        apps = ", ".join(sorted(self.tools.app_names())[:40])
        prompt = INTERPRET_PROMPT.format(apps=apps or "none", recent="\n".join(recent) or "(nothing yet)")
        out = await cloud_only([{"role": "system", "content": prompt}, {"role": "user", "content": user_text}])
        if not out:
            return None
        line = out.strip().splitlines()[0].strip().strip('"“”`').rstrip(".")
        return line[:300] or None

    async def summarize(self) -> str:
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in self.history if m["role"] in ("user", "assistant"))
        if not convo:
            return "Nothing has been said yet."
        return (await self.llm.complete([
            {"role": "system", "content": "Summarize this voice conversation in 3-5 short bullet points: topics, decisions, actions taken, follow-ups."},
            {"role": "user", "content": convo}])).strip()

    # ---- internals ----
    async def _run_tool(self, t0: float, name: str, args: dict) -> AsyncIterator[str]:
        if name == "summarize_conversation":
            result = ToolResult(await self.summarize())
        elif name == "say":
            result = ToolResult(args.get("text", ""))
        elif name == "cant":
            offer = args.get("offer")
            if offer and self.tools:
                result = ToolResult(args["text"], True, args["text"], lambda o=offer: self.tools.call(*o))
            else:
                result = ToolResult(args["text"])
        elif name == "ask_reminder_time":
            self.awaiting_reminder = args.get("text") or "reminder"
            day = "tomorrow" if "tomorrow" in self.awaiting_reminder else ""
            self._reminder_tomorrow = bool(day)
            self.awaiting_reminder = re.sub(r"\s+", " ", re.sub(r"\btomorrow\b", "", self.awaiting_reminder)).strip() or "reminder"
            result = ToolResult(f"Sure, what time{(' ' + day) if day else ''}?")
        elif name == "ask_verb":
            self.awaiting_verb = args.get("verb")
            result = ToolResult(args.get("text", "Sure, what should I do?"))
        elif name == "reset_conversation":
            self.history.clear()
            self.pending = None
            if self.memory:
                self.memory.drop_recent()   # a fresh thread; the facts about you stay (say "forget everything about me")
            result = ToolResult("Okay, fresh start. What do you need?")
        elif name in ("remember", "recall", "forget", "forget_all"):
            result = self._memory_tool(name, args)
        elif name == "browser_task":
            result = await self._browser_task(args.get("goal", ""))
        elif name == "briefing":
            self.reply_kind = "chat"                  # spoken in the natural voice, like a person would
            result = ToolResult(await self._briefing())
        elif name == "browser_read":
            page = await asyncio.to_thread(self.tools.browser_read)
            if not page:
                result = ToolResult("JARVIS's Edge window isn't open. Say “open YouTube” or “open Edge” first.")
            elif not args.get("summary") or not page.get("text"):
                result = ToolResult(f"You're on {page.get('title') or 'a page with no title'}.")
            else:
                summary = await self.llm.complete([
                    {"role": "system", "content": "Summarize this web page for someone listening, in two or three short "
                                                  "spoken sentences. No lists. Only use what's in the text."},
                    {"role": "user", "content": f"Title: {page.get('title')}\n\n{page['text'][:1800]}"}])  # short = fast on CPU
                summary = re.sub(r"^\s*(?:here'?s|here is|this is)\b[^:]{0,140}:\s*", "", summary, flags=re.I)  # drop echoed instructions
                summary = summary.strip().strip('"“”')
                result = ToolResult(speakable(summary) or f"You're on {page.get('title')}.")
        elif name == "clarify_open":
            # Garbled "open …" request: ask rather than guess and open the wrong thing.
            result = ToolResult("I'm not sure what you want me to open. Try something like "
                                "“open WhatsApp in Chrome”, “open Chrome”, or “open the DSA folder”.")
        else:
            result = await asyncio.to_thread(self.tools.call, name, args)
            repeatable = name in _REPEATABLE or (name == "browser_media" and args.get("action") in ("next", "forward", "back"))
            if repeatable and not _FAILED.match(result.text):
                self.last_step = (name, args, time.monotonic())
        self._log("tool", f"{name}({json.dumps(args)}) -> {result.text}")
        if result.needs_confirmation:
            self.pending = result
            text = result.confirm_prompt
        else:
            text = result.text
        self.metrics["llm_first_token"] = self.metrics["first_sentence"] = time.perf_counter() - t0
        self.history.append({"role": "assistant", "content": text})
        self._log("assistant", text)
        for sentence in SENTENCE_SPLIT.split(speakable(text)):
            if sentence:
                yield sentence

    async def _browser_task(self, goal: str, done: list[str] | None = None) -> ToolResult:
        """Do a multi-step job on the page in JARVIS's Edge, like a person with the mouse: look at the numbered
        elements, let the cloud model pick the next actions, do them, look again. Stops to ask before anything final
        (Submit, Send, Pay, Sign up…) and when it needs details you haven't given (it never makes them up)."""
        from .browser import BrowserError
        edge = self.tools.edge if self.tools else None
        cloud_only = getattr(self.llm, "cloud_only", None)
        if not edge or not edge.running():
            return ToolResult("My Edge window isn't open. Tell me which site to open first.")
        if not cloud_only:
            return ToolResult("I can't do several steps on a page without the cloud brain. I can still fill one field "
                              "at a time, like “type Yash in the name field”.")
        # Security: websites only ever get what you said in this command. Nothing from memory, nothing from the laptop.
        known: list[str] = []
        done = list(done or [])
        await asyncio.to_thread(edge.banner, "JARVIS is working on this page. Say stop to cancel.")
        try:
            return await self._browser_task_rounds(goal, edge, cloud_only, known, done)
        finally:
            await asyncio.to_thread(edge.banner, None)

    async def _browser_task_rounds(self, goal, edge, cloud_only, known, done) -> ToolResult:
        from .browser import BrowserError
        for _round in range(8):
            try:
                snap = await asyncio.to_thread(edge.snapshot)
            except BrowserError as exc:
                return ToolResult(str(exc))
            els = {e["id"]: e for e in snap.get("elements", [])}
            # keep the page description small (the free tier counts tokens per minute): on a form, links rarely matter
            shown = snap.get("elements", [])
            if any(e.get("kind") == "field" for e in shown) and not re.search(r"\b(?:link|open|go to|navigate)\b", goal, re.I):
                shown = [e for e in shown if e.get("kind") != "link"]
            listing = "\n".join(_describe_element(e) for e in shown[:90])
            prompt = AGENT_PROMPT.format(goal=goal, known="\n".join(f"- {k}" for k in known) or "- nothing",
                                         done="\n".join(f"- {d}" for d in done[-14:]) or "- nothing yet",
                                         url=snap.get("url", ""), title=snap.get("title", ""),
                                         text=snap.get("text", "")[:700], elements=listing or "(no elements)")
            msgs = [{"role": "system", "content": prompt}, {"role": "user", "content": "Next actions as JSON."}]
            out = await cloud_only(msgs, max_tokens=350)
            breaker = getattr(self.llm, "breaker", None)
            if out is None and breaker and 0 < breaker.until - time.monotonic() <= 40:
                # the free per-minute limit: wait it out once rather than stop halfway through a form
                await asyncio.sleep(breaker.until - time.monotonic() + 0.5)
                out = await cloud_only(msgs, max_tokens=350)
            plan = _parse_json(out)
            if not plan:
                why = "hit the free cloud limit" if breaker and breaker.reason.startswith("rewrite HTTP 429") else \
                    "lost my connection to the cloud brain"
                if done:
                    self.awaiting_task = (goal, done)             # "carry on" picks up from here
                return ToolResult(f"I {why} partway, so I stopped. "
                                  + (f"So far I did: {'; '.join(dict.fromkeys(done[-6:]))}. Say “carry on” in a "
                                     f"minute and I'll continue." if done else ""))
            for act in (plan.get("actions") or [])[:8]:
                what, el = str(act.get("do", "")), els.get(act.get("id"))
                if what == "scroll":
                    await asyncio.to_thread(edge.scroll, "down")
                    done.append("scrolled down")
                    continue
                if what == "open" and act.get("url"):
                    res = await asyncio.to_thread(self.tools.call, "open_website", {"url": str(act["url"])})
                    done.append(f"opened {act['url']} ({res.text})")
                    await asyncio.sleep(2.0)
                    break                                           # new page: look again
                if not el:
                    continue
                label = el.get("label") or el.get("tag")
                if what == "click" and _is_final_button(label):
                    # the last step goes through you: "Should I press Submit?"
                    def press(i=el["id"], lbl=label, d=list(done)) -> ToolResult:
                        hit = edge.act(i, "click")
                        return ToolResult(f"Pressed {lbl}." if hit.get("ok") else f"I couldn't press {lbl}.")
                    left = [d.split(" = ")[0].removeprefix("fill ") for d in done if "left for you" in d]
                    summary = "; ".join(d for d in done[-6:] if not d.startswith("scrolled") and "left for you" not in d) \
                        or "nothing yet"
                    if left:   # their secret goes in by their own hand first
                        return ToolResult(f"Ready to press {label}.", True,
                                          f"I filled what I can ({summary}). Please type your {', '.join(left)} yourself, "
                                          f"then say yes and I'll press “{label}”.", press)
                    return ToolResult(f"Ready to press {label}.", True,
                                      f"I've done this: {summary}. Should I press “{label}”?", press)
                value = act.get("value", "")
                hit = await asyncio.to_thread(edge.act, el["id"], {"fill": "fill", "select": "select", "check": "check"}
                                              .get(what, "click"), value)
                shown = "(not typed)" if hit.get("sensitive") else value
                done.append(f"{what} {label}" + (f" = {shown}" if what in ("fill", "select") else "") +
                            ("" if hit.get("ok") else
                             " (left for you: a sensitive field)" if hit.get("sensitive") else f" (failed: {hit.get('why', '?')})"))
                self._log("tool", f"browser_task: {done[-1]}")
                if what == "click":
                    await asyncio.sleep(1.2)                        # the page may change after a click
                    break
            status = str(plan.get("status", "continue"))
            if status == "ask":
                self.awaiting_task = (goal, done)
                return ToolResult(plan.get("say") or "What details should I use?")
            if status == "done":
                return ToolResult(plan.get("say") or "Done.")
            await asyncio.sleep(0.6)
        return ToolResult("I've done what I can for now: " + "; ".join(done[-5:]) + ". Tell me what to do next.")

    def briefing_facts(self) -> dict:
        """Everything a briefing may mention, and nothing else: the time, today's reminders, what memory knows."""
        now = datetime.now()
        reminders = []
        if self.tools:
            for r in self.tools.reminders.pending():
                try:
                    due = datetime.fromisoformat(r["due"])
                except (KeyError, ValueError):
                    continue
                if due.date() == now.date() or (now.hour >= 18 and (due.date() - now.date()).days == 1):
                    when = "tomorrow " if due.date() != now.date() else ""
                    reminders.append(f"{r.get('text', 'reminder')} at {when}{due.strftime('%I:%M %p').lstrip('0')}")
        facts = self.memory.facts()[-25:] if self.memory else []
        return {"now": now.strftime("%A %d %B, %I:%M %p").replace(" 0", " "), "part": _part_of_day(),
                "reminders": reminders, "facts": facts}

    async def _briefing(self) -> str:
        f = self.briefing_facts()
        user = self.settings.get("user_name") or ""
        plain = (f"Good {f['part'] if f['part'] in ('morning', 'afternoon', 'evening') else 'day'}{', ' + user if user else ''}. "
                 f"It's {f['now']}. " +
                 (f"You've got {len(f['reminders'])} reminder{'s' if len(f['reminders']) != 1 else ''} today: "
                  + "; ".join(f["reminders"]) + "." if f["reminders"] else "No reminders set for today."))
        prompt = (f"Write a short spoken {f['part']} briefing (two to four sentences, no lists) for the person you help"
                  f"{' named ' + user if user else ''}. Use ONLY the facts below. Mention a remembered fact only if it "
                  f"clearly matters today or this week (a plan, an exam, a date, a visit). Never invent plans, weather, "
                  f"news or appointments. If nothing is planned, say so warmly in one sentence.\n\n"
                  f"Now: {f['now']}\nReminders today: {'; '.join(f['reminders']) or 'none'}\n"
                  f"Things you remember about them:\n" + ("\n".join(f"- {x}" for x in f["facts"]) or "- nothing yet"))
        try:
            text = (await self.llm.complete([{"role": "system", "content": prompt},
                                             {"role": "user", "content": "Give me my briefing."}])).strip()
        except Exception:
            text = ""
        text = speakable(text) if text else ""
        # the model's words, but never without the hard facts: if it dropped a reminder, fall back to the plain version
        if not text or any(r.split(" at ")[0].lower()[:12] not in text.lower() for r in f["reminders"]):
            return plain
        return text

    def _memory_tool(self, name: str, args: dict) -> ToolResult:
        if not self.memory or self.settings.get("memory") is False:
            return ToolResult("My memory is switched off, so I can't keep things between conversations.")
        if name == "remember":
            fact = _about_user(args.get("text", ""))
            return ToolResult("Got it, I'll remember that." if self.memory.add(fact, "you") else
                              "Sorry, I didn't catch what to remember.")
        if name == "recall":
            facts = self.memory.facts()
            if not facts:
                return ToolResult("Nothing yet. Tell me things like “remember that my exam is on Friday” and I'll keep them.")
            said = " ".join(_to_you(f) for f in facts[-8:])
            more = f" And {len(facts) - 8} more things." if len(facts) > 8 else ""
            return ToolResult(f"Here's what I remember. {said}{more}")
        if name == "forget":
            gone = self.memory.remove(args.get("text", ""))
            return ToolResult("Okay, I've forgotten that." if gone else "I don't have anything like that saved.")
        count = len(self.memory.facts())

        def wipe() -> ToolResult:
            self.memory.clear()
            self.history.clear()
            return ToolResult("Done. I've forgotten everything I knew about you.")
        if not count:
            return ToolResult("There's nothing saved about you right now.")
        return ToolResult("Waiting for confirmation before wiping memory.", True,
                          f"Forget all {count} things I remember about you? That can't be undone.", wipe)

    def _say_fixed(self, text: str):
        self.history.append({"role": "assistant", "content": text})
        self._log("assistant", text)
        return [text]

    async def _generate(self, t0: float, tool_note: str = "", use_tools: bool = False) -> AsyncIterator[str]:
        self.reply_kind = "chat"      # the natural cloud voice speaks the model's replies
        msgs = self.messages()
        if tool_note:
            # A system message after the user's turn makes llama3.2 print "assistant" into its reply;
            # attach the hint to the user's message instead (only for this request, not the stored history).
            if msgs and msgs[-1]["role"] == "user":
                msgs[-1] = {**msgs[-1], "content": f"{msgs[-1]['content']}\n\n({tool_note})"}
            else:
                msgs.append({"role": "user", "content": f"({tool_note})"})
        spoken: list[str] = []
        last_user = next((m["content"] for m in reversed(self.history) if m["role"] == "user"), "")
        cap = 6 if re.search(r"\b(explain|in detail|tell me more|elaborate|step by step|story)\b", last_user, re.I) else 3
        self.history.append({"role": "assistant", "content": ""})
        entry = self.history[-1]
        stop = False
        for _round in range(3):
            chunker = SentenceChunker()
            calls: list[dict] = []
            async for chunk in self.llm.stream(msgs, self.tools.schemas() if use_tools and self.tools else None):
                if chunk.text and self.metrics["llm_first_token"] is None:
                    self.metrics["llm_first_token"] = time.perf_counter() - t0
                calls.extend(chunk.tool_calls)
                for sentence in chunker.feed(chunk.text):
                    if is_robotic(sentence):
                        continue  # "I'm a text-based AI" — wrong, and it sounds like a bot
                    if self.metrics["first_sentence"] is None:
                        self.metrics["first_sentence"] = time.perf_counter() - t0
                    if claims_action(sentence):
                        # The model is pretending it did something ("I've opened…", "I'll type…"). It didn't.
                        sentence, stop = HONEST_CANTS[self._honest_i % len(HONEST_CANTS)], True
                        self._honest_i += 1
                    spoken.append(sentence)
                    entry["content"] = " ".join(spoken)
                    yield sentence
                    if len(spoken) >= cap or stop:  # a voice reply should never run on
                        break
                if len(spoken) >= cap or stop:
                    break
            for sentence in chunker.flush() if len(spoken) < cap and not stop else []:
                if is_robotic(sentence):
                    continue
                if self.metrics["first_sentence"] is None:
                    self.metrics["first_sentence"] = time.perf_counter() - t0
                if claims_action(sentence):
                    sentence, stop = HONEST_CANTS[self._honest_i % len(HONEST_CANTS)], True
                    self._honest_i += 1
                spoken.append(sentence)
                entry["content"] = " ".join(spoken)
                yield sentence
            if stop or not calls or not self.tools:
                break
            # Execute tool calls, then let the model phrase the outcome.
            msgs.append({"role": "assistant", "content": entry["content"],
                         "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]})
            for call in calls[:3]:
                if call["name"] == "summarize_conversation":
                    result = ToolResult(await self.summarize())
                else:
                    result = await asyncio.to_thread(self.tools.call, call["name"], call["arguments"])
                self._log("tool", f"{call['name']}({json.dumps(call['arguments'])}) -> {result.text}")
                if result.needs_confirmation:
                    self.pending = result
                    spoken.append(result.confirm_prompt)
                    entry["content"] = " ".join(spoken)
                    yield result.confirm_prompt
                    self._log("assistant", entry["content"])
                    return
                msgs.append({"role": "tool", "content": result.text})
            use_tools = False  # one tool round, then answer
        if not entry["content"]:
            entry["content"] = ("I didn't get that as a command. Say it plainly, like “click the first video” or “open WhatsApp”."
                                if _TOOL_HINT.search(last_user) or re.search(r"\b(click|type|play|scroll)\b", last_user, re.I)
                                else "Sorry, I drew a blank there. Could you say that again?")
            yield entry["content"]
        if self.tools and _SEARCH_OFFER.search(entry["content"]) and not self.pending:
            # "I'm not sure… Want me to search Google for it?" → a plain "yes" next really searches
            q = re.sub(r"^\W*(?:hey\s+)?(?:jarvis\W+)?", "", last_user, flags=re.I).strip(" ?.!") or last_user
            self.pending = ToolResult("Offered a Google search.", True, entry["content"],
                                      lambda q=q: self.tools.call("web_search", {"query": q, "site": "google"}))
        self._log("assistant", entry["content"])

    def _log(self, role: str, text: str) -> None:
        if not self.settings.get("save_conversations"):
            return
        path = DATA_DIR / "conversations" / f"{self.id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": datetime.now().isoformat(timespec="seconds"), "channel": self.channel,
                                "role": role, "text": text}) + "\n")
