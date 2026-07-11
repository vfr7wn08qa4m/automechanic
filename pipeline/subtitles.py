"""Извлечение титров через yt-dlp + нормализация VTT в чистый текст с таймкодами.

Выверено на реальных авто-титрах YouTube (июль 2026):
- качаем РОВНО ОДНУ лучшую дорожку (ручные титры > авто-титры на языке оригинала),
  иначе YouTube быстро отдаёт 429 даже с домашнего IP;
- между видео обязательна пауза (config.YTDLP_SLEEP_SECONDS);
- на раннерах нужен deno (JS runtime для yt-dlp) и yt-dlp[default,curl-cffi].
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from . import config


def _run_ytdlp(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    cmd = ["yt-dlp", "--no-update", "--no-warnings"]
    if config.YTDLP_PROXY:
        cmd += ["--proxy", config.YTDLP_PROXY]
    cmd += args
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def video_meta(url: str) -> dict:
    """Метаданные видео без скачивания (включая список доступных дорожек титров)."""
    p = _run_ytdlp(["-J", "--skip-download", url])
    if p.returncode != 0:
        raise RuntimeError(f"yt-dlp meta failed: {p.stderr[-500:]}")
    return json.loads(p.stdout)


def pick_subtitle_track(meta: dict) -> tuple[str, bool] | None:
    """Выбрать лучшую дорожку: (lang, is_auto). Ручные приоритетнее авто.

    Порядок предпочтения: язык оригинала видео, затем языки из SUB_LANGS.
    """
    manual = set(meta.get("subtitles") or {})
    auto = set(meta.get("automatic_captions") or {})
    orig = (meta.get("language") or "").split("-")[0]

    prefer = [orig] + [l.strip() for l in config.SUB_LANGS] if orig else list(config.SUB_LANGS)
    seen: list[str] = []
    for lang in prefer:
        if lang and lang not in seen:
            seen.append(lang)

    for lang in seen:
        for cand in manual:
            if cand == lang or cand.startswith(lang + "-"):
                return cand, False
    for lang in seen:
        for cand in auto:
            if cand == lang or cand.startswith(lang + "-"):
                return cand, True
    if manual:
        return sorted(manual)[0], False
    if auto:
        # у авто-титров куча вариантов "xx-orig"/переводов; берём orig, если есть
        origs = [l for l in auto if l.endswith("-orig")]
        return (sorted(origs)[0] if origs else sorted(auto)[0]), True
    return None


def fetch_subtitles(url: str, workdir: Path | None = None) -> tuple[str, str]:
    """Скачать одну лучшую дорожку. Возвращает (lang, vtt_text)."""
    meta = video_meta(url)
    track = pick_subtitle_track(meta)
    if not track:
        raise RuntimeError(f"no subtitles available for {url}")
    lang, is_auto = track

    workdir = Path(workdir or tempfile.mkdtemp(prefix="subs_"))
    out = workdir / "%(id)s.%(ext)s"
    flag = "--write-auto-subs" if is_auto else "--write-subs"
    p = _run_ytdlp([
        "--skip-download", flag, "--sub-langs", lang, "--sub-format", "vtt",
        "-o", str(out), url,
    ])
    vtts = list(workdir.glob("*.vtt"))
    if not vtts:
        raise RuntimeError(f"subtitle download failed ({lang}): {p.stderr[-500:]}")
    return lang, vtts[0].read_text(encoding="utf-8", errors="replace")


_TS_RE = re.compile(r"(\d+):(\d\d):(\d\d)\.(\d\d\d)\s+-->")
_TAG_RE = re.compile(r"<[^>]+>")


def vtt_to_lines(vtt: str) -> list[tuple[int, str]]:
    """VTT -> [(секунда, реплика)]. Убирает пословные теги и «плывущие» дубли
    авто-титров YouTube (каждый кью повторяет предыдущую строку)."""
    lines: list[tuple[int, str]] = []
    cur_sec = 0
    last_emitted = ""
    for raw in vtt.splitlines():
        m = _TS_RE.match(raw.strip())
        if m:
            h, mi, s, _ = m.groups()
            cur_sec = int(h) * 3600 + int(mi) * 60 + int(s)
            continue
        if "-->" in raw or raw.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        text = _TAG_RE.sub("", raw).strip()
        if not text or text == last_emitted:
            continue
        last_emitted = text
        lines.append((cur_sec, text))
    return lines


def to_prompt_text(lines: list[tuple[int, str]], max_chars: int | None = None) -> str:
    """Текст для LLM: '[mm:ss] реплика'. При переполнении равномерно прореживаем
    середину (начало и конец видео обычно самые информативные: жалоба и итог)."""
    max_chars = max_chars or config.DISTILL_MAX_INPUT_CHARS
    rendered = [f"[{sec // 60:02d}:{sec % 60:02d}] {text}" for sec, text in lines]
    full = "\n".join(rendered)
    if len(full) <= max_chars:
        return full
    head_n = int(len(rendered) * 0.25)
    tail_n = int(len(rendered) * 0.25)
    mid = rendered[head_n : len(rendered) - tail_n : 2]  # каждая вторая строка середины
    parts = rendered[:head_n] + ["[...пропуски...]"] + mid + rendered[len(rendered) - tail_n:]
    out = "\n".join(parts)
    return out[:max_chars]
