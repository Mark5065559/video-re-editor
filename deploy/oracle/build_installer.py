#!/usr/bin/env python3
"""Builds the one-file server installer with the bot token and Telegram API keys inside.

    python deploy/oracle/build_installer.py --out video-re-editor-oracle-install.sh

Values come from --token/--api-id/--api-hash, then environment variables, then the .env file
in the project folder. The result contains secrets: keep it private and never commit it
(*.secret.sh and video-re-editor-oracle-install*.sh are in .gitignore).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
TEMPLATE = HERE / "install.sh"
DEFAULT_REPO = "https://github.com/Mark5065559/video-re-editor.git"
DEFAULT_BRANCHES = "claude/great-franklin-xpqj4a main"
# Oracle keeps instance metadata (including the script) under 32 000 bytes after base64.
PASTE_LIMIT_BYTES = 23_000

TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
API_ID_RE = re.compile(r"^\d{1,12}$")
API_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")
REPO_RE = re.compile(r"^(https://[\w.-]+/[\w./-]+|/[\w./-]+|file://[\w./-]+)$")
BRANCHES_RE = re.compile(r"^[\w./-]+( [\w./-]+)*$")


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def build(token: str, api_id: str, api_hash: str, repo_url: str, branches: str) -> str:
    if not TOKEN_RE.fullmatch(token):
        raise ValueError("BOT_TOKEN выглядит неправильно (нужен вид 123456789:AA...)")
    if bool(api_id) != bool(api_hash):
        raise ValueError("Нужны оба значения TG_API_ID и TG_API_HASH (или ни одного)")
    if api_id and not API_ID_RE.fullmatch(api_id):
        raise ValueError("TG_API_ID должен состоять из цифр")
    if api_hash and not API_HASH_RE.fullmatch(api_hash):
        raise ValueError("TG_API_HASH должен быть строкой из 32 шестнадцатеричных символов")
    if not REPO_RE.fullmatch(repo_url):
        raise ValueError("Неподходящий адрес репозитория")
    if not BRANCHES_RE.fullmatch(branches):
        raise ValueError("Неподходящий список веток")
    text = TEMPLATE.read_text(encoding="utf-8")
    for placeholder, value in {
        "__BOT_TOKEN__": token, "__TG_API_ID__": api_id, "__TG_API_HASH__": api_hash.lower(),
        "__REPO_URL__": repo_url, "__BRANCHES__": branches,
    }.items():
        if placeholder not in text:
            raise RuntimeError(f"В шаблоне нет {placeholder}")
        text = text.replace(placeholder, value)
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="куда сохранить готовый установщик")
    parser.add_argument("--token")
    parser.add_argument("--api-id")
    parser.add_argument("--api-hash")
    parser.add_argument("--repo-url", default=DEFAULT_REPO)
    parser.add_argument("--branches", default=DEFAULT_BRANCHES, help="ветки по порядку, через пробел")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    args = parser.parse_args(argv)

    env_file = read_env_file(Path(args.env_file))

    def pick(cli: str | None, name: str) -> str:
        return (cli or os.environ.get(name) or env_file.get(name) or "").strip()

    try:
        text = build(
            pick(args.token, "BOT_TOKEN"), pick(args.api_id, "TG_API_ID"), pick(args.api_hash, "TG_API_HASH"),
            args.repo_url, args.branches,
        )
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    try:
        out.chmod(0o600)
    except OSError:
        pass
    size = len(text.encode("utf-8"))
    print(f"Готово: {out} ({size} байт). В файле секреты — не публикуй его.")
    if size > PASTE_LIMIT_BYTES:
        print("Внимание: файл великоват для поля cloud-init в Oracle.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
