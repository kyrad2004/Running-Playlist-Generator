"""Diagnose why a .env file isn't being picked up.

python-dotenv fails quietly on several common editor artifacts — a UTF-8 BOM
makes the first key unreadable, smart quotes end up inside the value, and a
UTF-16 save can't be decoded at all. None of that produces a useful error on its
own, so this module inspects the file and says what's actually wrong.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from .config import REPO_ROOT

SMART_QUOTES = "“”‘’"

# Filenames people end up with instead of ".env". Windows hides known
# extensions, so "Save As -> .env" in Notepad silently writes ".env.txt".
NEAR_MISS_NAMES = (".env.txt", ".env.example", "env", ".ENV", ".env.local", "env.txt")


@dataclass
class EnvProblem:
    summary: str
    fix: str


def _decode(raw: bytes) -> tuple[str | None, EnvProblem | None]:
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return None, EnvProblem(
            ".env is saved as UTF-16, which python-dotenv cannot read at all.",
            "Re-save it as UTF-8. In VS Code: click the encoding in the status bar "
            "→ 'Save with Encoding' → 'UTF-8'.",
        )
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig"), EnvProblem(
            ".env begins with a UTF-8 byte-order mark (BOM). python-dotenv reads the "
            "first key as '\\ufeffSPOTIFY_CLIENT_ID', so it never matches — this is the "
            "single most common cause of this error on Windows.",
            "Re-save as 'UTF-8' rather than 'UTF-8 with BOM'. In VS Code: click the "
            "encoding in the status bar → 'Save with Encoding' → 'UTF-8'. "
            "In Notepad: Save As → Encoding: UTF-8.",
        )
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return None, EnvProblem(
            f".env is not valid UTF-8 ({exc.reason}).",
            "Re-save the file as UTF-8.",
        )


def diagnose(required_keys: tuple[str, ...] = ("SPOTIFY_CLIENT_ID",)) -> list[EnvProblem]:
    """Return everything wrong with the .env file, most decisive first."""
    problems: list[EnvProblem] = []
    env_path = REPO_ROOT / ".env"

    # -- does it exist at all? -------------------------------------------
    if not env_path.exists():
        found = [n for n in NEAR_MISS_NAMES if (REPO_ROOT / n).exists()]
        summary = f"No .env file at {env_path}."
        fix = "Create it at the repo root, next to README.md — see .env.example."

        if ".env.txt" in found or "env.txt" in found:
            summary += (
                " A file named '.env.txt' is there instead. Windows hides known "
                "extensions, so a file that shows as '.env' in Explorer is often "
                "really '.env.txt'."
            )
            fix = "Rename it to exactly '.env':  ren .env.txt .env  (macOS/Linux: mv .env.txt .env)"
        elif ".env.example" in found:
            summary += " Only '.env.example' is there, and that template is never read."
            fix = "Copy it:  cp .env.example .env   (Windows: copy .env.example .env)"

        problems.append(EnvProblem(summary, fix))
        return problems

    raw = env_path.read_bytes()
    if not raw.strip():
        problems.append(EnvProblem(f"{env_path} exists but is empty.", "Fill it in from .env.example."))
        return problems

    text, encoding_problem = _decode(raw)
    if encoding_problem:
        problems.append(encoding_problem)
    if text is None:
        return problems

    # -- did they edit the template instead? ------------------------------
    example = REPO_ROOT / ".env.example"
    if example.exists():
        example_values = dotenv_values(example)
        for key in required_keys:
            if example_values.get(key) and not dotenv_values(env_path).get(key):
                problems.append(
                    EnvProblem(
                        f"{key} is filled in inside .env.example but not in .env.",
                        "You edited the template. .env.example is only a sample and is "
                        "never read — move your value into .env.",
                    )
                )
                break

    # -- line-level syntax ------------------------------------------------
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip().lstrip("﻿")
        if not stripped or stripped.startswith("#"):
            continue

        if "=" not in stripped and ":" in stripped:
            problems.append(
                EnvProblem(
                    f"Line {number} uses a colon: {stripped[:48]}",
                    "A .env file needs KEY=value, not KEY: value.",
                )
            )
            continue

        if "=" not in stripped:
            problems.append(
                EnvProblem(f"Line {number} has no '=': {stripped[:48]}", "Use KEY=value.")
            )
            continue

        key, _, value = stripped.partition("=")
        if any(q in value for q in SMART_QUOTES):
            problems.append(
                EnvProblem(
                    f"Line {number} ({key.strip()}) contains curly “smart quotes”, which "
                    "become part of the value.",
                    "Delete the quotes entirely — values need no quoting. If your editor "
                    "keeps inserting them, turn off smart/curly quotes (this is on by "
                    "default in Word, Pages, and Notes — use a code editor instead).",
                )
            )

    # -- did the required keys actually load? -----------------------------
    values = dotenv_values(env_path)
    for key in required_keys:
        if key in text and not (values.get(key) or "").strip():
            if not any(key in p.summary for p in problems):
                problems.append(
                    EnvProblem(
                        f"{key} appears in .env but parses as empty.",
                        f"Check the line reads exactly:  {key}=your_value_here",
                    )
                )

    return problems


def format_problems(problems: list[EnvProblem]) -> str:
    lines = []
    for i, problem in enumerate(problems, start=1):
        lines.append(f"  {i}. {problem.summary}")
        lines.append(f"     → {problem.fix}")
    return "\n".join(lines)
