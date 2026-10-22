r"""Pre-commit secret scan for JARVIS Zero. Refuses a commit that would put a secret or personal data into git history.

Checks every staged file for:
  * the real secret values in .env (GROQ_API_KEY, JARVIS_API_TOKEN …), matched exactly,
  * well-known key formats (Groq, OpenAI, Anthropic, GitHub, AWS, Google, private keys),
  * paths that must never be tracked (data/, .env, memory, profiles, conversations).
Run by .git/hooks/pre-commit; also: python scripts/git_secret_scan.py --all   (scan every tracked file).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "Groq API key": r"gsk_[A-Za-z0-9]{20,}",
    "OpenAI-style key": r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}",
    "GitHub token": r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "Google API key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "private key": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "secret assignment": r"(?i)\b(?:api[_-]?key|secret|token|password)\s*[=:]\s*['\"][A-Za-z0-9_\-]{24,}['\"]",
}
FORBIDDEN_PATHS = re.compile(r"(?:^|/)(?:data/|\.env(?:$|\.(?!example$))|memory\.json$|edge-agent/|edge-profile/|"
                             r"conversations/|reminders[^/]*\.json$)", re.I)
ALLOW_IN_FILE = {"scripts/git_secret_scan.py"}            # this file describes the patterns


def env_secrets() -> dict[str, str]:
    out = {}
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if len(v.strip()) >= 12:
                    out[k.strip()] = v.strip()
    return out


def staged(all_files: bool) -> list[str]:
    cmd = ["git", "ls-files"] if all_files else ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
    return [f for f in subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True).stdout.splitlines() if f]


def content(path: str, all_files: bool) -> str:
    if all_files:
        p = ROOT / path
        return p.read_text(encoding="utf-8", errors="ignore") if p.is_file() else ""
    r = subprocess.run(["git", "show", f":{path}"], cwd=ROOT, capture_output=True)
    return r.stdout.decode("utf-8", errors="ignore")


def scan(all_files: bool = False) -> list[str]:
    problems, secrets = [], env_secrets()
    for f in staged(all_files):
        norm = f.replace("\\", "/")
        if FORBIDDEN_PATHS.search(norm):
            problems.append(f"{f}: this file must never be in git (personal data or secrets)")
            continue
        text = content(f, all_files)
        for name, value in secrets.items():
            if value in text:
                problems.append(f"{f}: contains the real value of {name} from .env")
        if norm in ALLOW_IN_FILE:
            continue
        for label, pat in PATTERNS.items():
            m = re.search(pat, text)
            if m:
                line = text[: m.start()].count("\n") + 1
                problems.append(f"{f}:{line}: looks like a {label}")
    return problems


def main() -> int:
    problems = scan(all_files="--all" in sys.argv)
    if problems:
        print("SECRET SCAN: commit refused. Remove these first:")
        for p in problems:
            print("  - " + p)
        return 1
    print("secret scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
