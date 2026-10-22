"""Fast tests for the text clean-up applied before speech, and voice listing."""
import pytest

from jarvis.components import available_voices, normalize_for_speech


@pytest.mark.parametrize("text, spoken", [
    ("It is 32°C outside.", "It is 32 degrees outside."),
    ("Wind at 15 km/h.", "Wind at 15 kilometres per hour."),
    ("Tom & Jerry", "Tom and Jerry"),
    ("Read https://example.com/a?b=1 later.", "Read example.com later."),  # site name, not a hole mid-sentence
    ("Visit https://www.youtube.com/watch?v=abc for more.", "Visit youtube.com for more."),
    ("Mail yash@example.com.", "Mail yash at example.com."),
    ("Low of -5\u00b0C tonight.", "Low of minus 5 degrees tonight."),
    ("Call 9876543210 now.", "Call 9 8 7 6 5 4 3 2 1 0 now."),
    ("Call +91 98765 43210.", "Call plus 9 1 9 8 7 6 5 4 3 2 1 0."),
    ("Budget \u20b91,00,000 or \u20b92,50,00,000.", "Budget 1 lakh rupees or 2 crore 50 lakh rupees."),
    ("Worth $1.5M now.", "Worth 1.5 million dollars now."),
    ("Learn C++ or C#.", "Learn C plus plus or C sharp."),
    ("Take 500mg, 1/2 a tablet.", "Take 500 milligrams, one half a tablet."),
    ("A 50/50 chance, ~20 minutes, #1 song, 5 x 3.", "A 50 50 chance, about 20 minutes, number 1 song, 5 times 3."),
    ("Pi is 3.14159, year 1990-1995.", "Pi is 3.14159, year 1990 to 1995."),
    ("Done 😀👍", "Done"),
    ("That costs ₹1,200, roughly $15.", "That costs 1,200 rupees, roughly 15 dollars."),
    ("Battery at 80%.", "Battery at 80 percent."),
    ("Open 9-5, e.g. weekdays.", "Open 9 to 5, for example weekdays."),
    ("**Bold** and `code`", "Bold and code"),
    ("Good morning, sir.", "Good morning, sir."),
    ("🙂", ""),
])
def test_normalize_for_speech(text, spoken):
    assert normalize_for_speech(text) == spoken


def test_available_voices_lists_both_engines():
    voices = available_voices()
    assert any(not v.startswith("kokoro:") for v in voices)          # Piper voices always listed
    kokoro = [v for v in voices if v.startswith("kokoro:")]
    if kokoro:
        assert "kokoro:am_michael" in kokoro and "kokoro:af_heart" in kokoro
        assert all(v.split(":")[1][:2] in ("am", "af", "bm", "bf") for v in kokoro)
