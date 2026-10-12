"""Fails if a file in the repo contains something personal: an email address,
something that looks like a Zotero API key, or a private Claude session link."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_EMAILS = {"noreply@anthropic.com", "you@example.com"}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
ZOTERO_KEY = re.compile(r"\b[A-Za-z0-9]{24}\b")
SESSION = re.compile(r"claude\.ai/code/session", re.I)


def repo_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
        names = [n for n in out.stdout.splitlines() if n]
    except (OSError, subprocess.CalledProcessError):
        names = []
    if not names:  # not a git checkout: scan everything except caches
        names = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                 if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]
    return [n for n in names if n != "LICENSE"]


def test_no_personal_data_in_repo():
    problems = []
    for name in repo_files():
        text = (ROOT / name).read_text(errors="ignore")
        for m in EMAIL.finditer(text):
            if m.group(0).lower() not in ALLOWED_EMAILS and not m.group(0).endswith(".pdf"):
                problems.append(f"{name}: email {m.group(0)}")
        for m in ZOTERO_KEY.finditer(text):
            if re.search(r"\d", m.group(0)) and re.search(r"[A-Za-z]", m.group(0)):
                problems.append(f"{name}: key-like text {m.group(0)}")
        if SESSION.search(text):
            problems.append(f"{name}: Claude session link")
    assert not problems, "\n".join(problems)
