"""Final review fixes: invisible characters can't dodge redaction, paste counts as typing, long risky labels ask."""
from backend.jarvis.gate import Action, Gate
from backend.jarvis.untrusted import clean, redact


def test_zero_width_cannot_split_a_card_number():
    sneaky = "card 4111\u200b1111\u200b1111\u200b1111"
    assert "4111" not in redact(clean(sneaky))


def test_paste_into_signin_browser_page_is_denied():
    g = Gate(None, None, strict=True)
    d = g.authorize(Action("combo", 1, "msedge.exe", "Sign in to your account - Edge", key="ctrl+v"), {"name": "Amount"})
    assert d.verdict == "deny"


def test_long_risky_button_label_still_asks():
    g = Gate(None, None, strict=True)
    d = g.authorize(Action("click", 1, "whatsapp.exe", "Chat", "Send this message to everyone now", ctype="Button"), None)
    assert d.verdict == "confirm"
