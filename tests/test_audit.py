"""The action log: secret-free, rotated, hash-chained, safe from several threads."""
import json
import threading

from jarvis.audit import GENESIS, Audit
from jarvis.gate import Action, Decision, text_class


def act(label="Play", **kw):
    return Action("click", 1, "spotify.exe", "Spotify Free", label, **kw)


def test_one_sorted_line_per_record_with_only_lengths_for_typing(tmp_path):
    a = Audit(tmp_path / "actions.log", clock=lambda: 1_700_000_000)
    a.record(Action("type", 1, "notepad.exe", "Untitled - Notepad", "Text editor", text_len=11,
                    text_class=text_class("hello w0rld")), Decision("allow"), "allow")
    raw = (tmp_path / "actions.log").read_text(encoding="utf-8")
    assert raw.count("\n") == 1
    row = json.loads(raw)
    assert list(row) == sorted(row)                                       # sorted keys
    assert row["kind"] == "type" and row["app"] == "notepad.exe" and row["text_len"] == 11 and row["text_class"] == "mixed"
    assert "hello" not in raw and set(row) >= {"time", "kind", "app", "title", "label", "outcome", "source", "prev"}


def test_text_class_never_keeps_text():
    assert text_class("abc def") == "letters" and text_class("1234") == "digits" and text_class("a1") == "mixed"
    assert text_class("") == ""


def test_control_characters_are_stripped_and_long_fields_cut(tmp_path):
    a = Audit(tmp_path / "actions.log")
    a.record(act("Pl\nay\x00\x1b[31m" + "x" * 200, ), Decision("allow"), "ok")
    row = a.tail(1)[0]
    assert "\n" not in row["label"] and "\x00" not in row["label"] and "\x1b" not in row["label"]
    assert len(row["label"]) <= 60 and len(row["title"]) <= 60
    assert len((tmp_path / "actions.log").read_text(encoding="utf-8").splitlines()) == 1     # no injected extra line


def test_rotation_at_the_size_limit_keeps_two_old_copies(tmp_path):
    a = Audit(tmp_path / "actions.log", max_bytes=1500)
    for i in range(80):
        a.record(act(f"Button {i}"), Decision("allow"), "ok")
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["actions.log", "actions.log.1", "actions.log.2"]
    assert (tmp_path / "actions.log").stat().st_size < 3000
    assert a.tail(1)[0]["label"] == "Button 79"
    assert a.verify() is None                                              # the chain carries across a rotation
    assert len(a.tail(5)) == 5


def test_hash_chain_detects_an_edited_or_removed_line(tmp_path):
    p = tmp_path / "actions.log"
    a = Audit(p)
    for i in range(6):
        a.record(act(f"B{i}"), Decision("allow"), "ok")
    assert a.verify() is None
    lines = p.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert first["prev"] == GENESIS
    edited = list(lines)
    edited[2] = edited[2].replace("B2", "B9")
    p.write_text("\n".join(edited) + "\n", encoding="utf-8")
    assert a.verify() == 3                                                 # line 3 no longer follows the edited line 2
    p.write_text("\n".join(lines[:2] + lines[3:]) + "\n", encoding="utf-8")
    assert a.verify() == 2                                                 # a deleted line breaks the chain there


def test_chain_continues_after_a_restart(tmp_path):
    p = tmp_path / "actions.log"
    Audit(p).record(act("one"), Decision("allow"), "ok")
    again = Audit(p)
    again.record(act("two"), Decision("allow"), "ok")
    assert again.verify() is None and [r["label"] for r in again.tail(5)] == ["one", "two"]


def test_four_threads_write_a_clean_chain(tmp_path):
    a = Audit(tmp_path / "actions.log")

    def worker(n):
        for i in range(50):
            a.record(act(f"t{n}-{i}"), Decision("allow"), "ok")
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    lines = (tmp_path / "actions.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 200 and all(json.loads(x) for x in lines)
    assert a.verify() is None


def test_tail_and_spoken_summary(tmp_path):
    a = Audit(tmp_path / "actions.log", clock=lambda: 1_700_000_000)
    assert "haven't done anything" in a.speak()
    a.record(act("Play"), Decision("allow"), "allow")
    a.record(Action("type", 1, "notepad.exe", "x", text_len=5, text_class="letters"), Decision("allow"), "allow")
    a.record(Action("click", 1, "cmd.exe", "x", "Run"), Decision("deny"), "deny")
    said = a.speak()
    assert "clicked Play in spotify" in said and "typed 5 characters in notepad" in said and "(refused)" in said
    assert len(a.tail(2)) == 2 and a.tail(0) == []
