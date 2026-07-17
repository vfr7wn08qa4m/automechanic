#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Раскатка репо ночного Claude-агента дистилляции (claudeautomation).

Собирает МИНИМАЛЬНЫЙ самодостаточный субсет (pipeline-подмножество + CLAUDE.md/
README/requirements/.env.example/.gitignore) в чистую папку и пушит в приватный
репо агента. Реквизиты — из accounts.json (секретный пульт), секция:

    "claude_repo": { "repo": "<owner>/<repo>", "github_token": "ghp_..." }

Код pipeline берётся СВЕЖИМ из этого репозитория (актуальные правки), мета —
из ранее собранной scratchpad-папки claudeautomation (валидные CLAUDE.md/README),
с подстановкой имени целевого репо в README. Токен нигде не печатается.

    python scripts/deploy_claude_repo.py --accounts accounts.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path


def _force_rmtree(path: Path) -> None:
    """rmtree, снимающий read-only бит (Windows: .git-объекты только для чтения)."""
    def _onerr(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:  # noqa: BLE001
            pass
    if not path.exists():
        return
    try:
        shutil.rmtree(path, onexc=_onerr)       # py3.12+
    except TypeError:
        shutil.rmtree(path, onerror=_onerr)     # старее

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
# Субсет pipeline, который реально нужен агенту (проверено: next-subs/save-case
# работают только с ADO_*; импорты замкнуты внутри этих модулей).
SUBSET = ["__init__", "config", "ado", "case_schema", "store",
          "subtitles", "subtitle_providers", "tools"]
OLD_SLUGS = ["x2n873ju8wd21w4/claudeautomation", "vfr7wn08qa4m/claudeautomation"]

REQUIREMENTS = ("requests>=2.32\n"
                "pydantic>=2.7\n"
                "python-dotenv>=1.0\n"
                "boto3>=1.34        # только если задан S3_* (архив кейсов); иначе не нужен\n")
ENV_EXAMPLE = ("# Для claude.ai — задаётся в Environment variables рутины, НЕ в файле.\n"
               "# ADO_* — обязательны.\n"
               "ADO_ORG=gpsgroupagent12\n"
               "ADO_PROJECT=AutoMechanic\n"
               "ADO_PAT=\n\n"
               "# Опционально: архив кейсов в R2 (не обязателен — кейс и так в теле тикета)\n"
               "# S3_ENDPOINT=\n# S3_KEY=\n# S3_SECRET=\n# S3_BUCKET=automech-archive\n")
GITIGNORE = ".env\n__pycache__/\n*.pyc\n*.log\ndata/\n"


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def _git(args: list[str], cwd: Path, token: str = "") -> None:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        msg = _scrub((r.stderr or r.stdout or "").strip(), token)
        raise RuntimeError(f"git {args[0]} упал: {msg[:300]}")


def build(build_dir: Path, repo: str) -> None:
    _force_rmtree(build_dir)
    (build_dir / "pipeline").mkdir(parents=True)

    # 1) свежий код pipeline из главного репо
    for name in SUBSET:
        shutil.copy2(ROOT / "pipeline" / f"{name}.py",
                     build_dir / "pipeline" / f"{name}.py")

    # 2) CLAUDE.md (инструкция агента) и README — самодостаточные версии для репо
    # агента (claude/agent_repo_*.md); fallback на общие claude/*.md.
    src_claude = ROOT / "claude" / "agent_repo_CLAUDE.md"
    if not src_claude.exists():
        src_claude = ROOT / "claude" / "DISTILL_AGENT.md"
    src_readme = ROOT / "claude" / "agent_repo_README.md"
    if not src_readme.exists():
        src_readme = ROOT / "claude" / "CLOUD_AGENT_SETUP.md"
    if not src_claude.exists():
        raise RuntimeError("нет claude/agent_repo_CLAUDE.md — нечего дать агенту")
    (build_dir / "CLAUDE.md").write_text(
        src_claude.read_text(encoding="utf-8"), encoding="utf-8")

    readme = (src_readme.read_text(encoding="utf-8") if src_readme.exists()
              else "# claudeautomation — ночной Claude-агент дистилляции AutoMech\n")
    for old in OLD_SLUGS:                    # перецелить ссылки на новый репо
        readme = readme.replace(old, repo)
    (build_dir / "README.md").write_text(readme, encoding="utf-8")

    # 3) мелкие мета — генерим (стабильно, без зависимости от scratchpad)
    (build_dir / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    (build_dir / ".env.example").write_text(ENV_EXAMPLE, encoding="utf-8")
    (build_dir / ".gitignore").write_text(GITIGNORE, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", required=True)
    ap.add_argument("--branch", default="main")
    args = ap.parse_args()

    cfg = json.loads(Path(args.accounts).read_text(encoding="utf-8"))
    cr = cfg.get("claude_repo") or {}
    repo = (cr.get("repo") or "").strip()
    token = (cr.get("github_token") or "").strip()
    if not repo or "/" not in repo:
        sys.exit("! в accounts.json нет claude_repo.repo вида '<owner>/<repo>'")
    if not token or "REPLACE" in token or "PASTE" in token.upper():
        sys.exit("! в accounts.json не заполнен claude_repo.github_token")

    build_dir = ROOT / ".claude_repo_build"
    print(f"# сборка репо агента -> {repo}")
    print(f"#   код: свежий pipeline ({len(SUBSET)} модулей) из этого репо")
    print("#   мета: CLAUDE.md<-claude/agent_repo_CLAUDE.md, README<-agent_repo_README.md")
    build(build_dir, repo)
    files = sorted(p.relative_to(build_dir).as_posix()
                   for p in build_dir.rglob("*") if p.is_file())
    print(f"#   файлов: {len(files)} :: {', '.join(files)}")

    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    try:
        _git(["init", "-q"], build_dir, token)
        _git(["checkout", "-q", "-B", args.branch], build_dir, token)
        _git(["add", "-A"], build_dir, token)
        _git(["-c", "user.email=deploy@automech", "-c", "user.name=automech-deploy",
              "commit", "-q", "-m", "AutoMech: ночной Claude-агент дистилляции"],
             build_dir, token)
        _git(["remote", "add", "origin", url], build_dir, token)
        _git(["push", "-f", "origin", args.branch], build_dir, token)
    except RuntimeError as e:
        sys.exit(f"! {e}")
    finally:
        # затираем remote с токеном на всякий (папка временная, но чистим)
        try:
            _git(["remote", "set-url", "origin",
                  f"https://github.com/{repo}.git"], build_dir)
        except Exception:
            pass
    print(f"# ✓ запушено: https://github.com/{repo} (ветка {args.branch})")


if __name__ == "__main__":
    main()
