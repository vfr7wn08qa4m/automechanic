"""Фаза 0: сквозная проверка ценности на нескольких видео.

    python -m pipeline.run_phase0 --url https://www.youtube.com/watch?v=XXXX
    python -m pipeline.run_phase0 --search "не заводится диагностика" -n 3
    python -m pipeline.run_phase0 --url ... --embed   # + вектор и Qdrant, если настроен

Каждый кейс печатается в консоль и дописывается в data/cases.jsonl.
Критерий успеха фазы: на 15-20 видео >70%% кейсов полезны на глаз
(симптомы/причина/шаги/нюансы извлечены верно).
"""
from __future__ import annotations

import argparse
import time

from . import config
from .case_schema import Source
from .distill import distill
from .store import append_jsonl, archive_blob, qdrant_upsert
from .subtitles import _run_ytdlp, fetch_subtitles, to_prompt_text, video_meta, vtt_to_lines


def process_video(url: str, do_embed: bool = False) -> None:
    print(f"\n=== {url}")
    meta = video_meta(url)
    source = Source(
        type="youtube", url=url, video_id=meta.get("id", ""),
        channel=meta.get("channel", ""), channel_id=meta.get("channel_id", ""),
        title=meta.get("title", ""), lang=meta.get("language") or "",
        published_at=str(meta.get("upload_date", "")),
        duration_sec=meta.get("duration"),
    )
    print(f"  {source.channel} | {source.title}")

    lang, vtt = fetch_subtitles(url)
    lines = vtt_to_lines(vtt)
    transcript = to_prompt_text(lines)
    print(f"  титры: lang={lang}, реплик={len(lines)}, символов={len(transcript)}")
    archive_blob(f"subs/{source.video_id}.{lang}.vtt", vtt)

    case = distill(transcript, source)
    case.lang = case.lang or lang

    print(f"  авто: {case.vehicle.make} {case.vehicle.model} {case.vehicle.engine}")
    print(f"  система: {case.system} | DTC: {', '.join(case.dtc_codes) or '-'}")
    print(f"  проблема: {case.problem_summary}")
    print(f"  причина:  {case.root_cause}")
    print(f"  шагов диагностики/ремонта: {len(case.diagnostic_steps)}/{len(case.repair_steps)}"
          f", замеров: {len(case.measurements)}, нюансов: {len(case.pitfalls)}")
    for p in case.pitfalls[:5]:
        ts = f" [{p.timestamp_sec // 60}:{p.timestamp_sec % 60:02d}]" if p.timestamp_sec else ""
        print(f"    ! {p.text}{ts}")
    print(f"  off_topic={case.off_topic} fixed={case.fixed} confidence={case.confidence}")

    append_jsonl(case)
    archive_blob(f"cases/{source.video_id}.json", case.model_dump_json())

    if do_embed and not case.off_topic:
        from .embed import embed
        vec = embed([case.search_text()])[0]
        qdrant_upsert(case, vec)
        print(f"  вектор: {len(vec)}d, записан в Qdrant" if config.QDRANT_URL
              else f"  вектор: {len(vec)}d (Qdrant не настроен)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", action="append", default=[], help="URL видео (можно несколько)")
    ap.add_argument("--search", help="взять первые N видео из поиска YouTube")
    ap.add_argument("-n", type=int, default=3, help="сколько видео из поиска")
    ap.add_argument("--embed", action="store_true", help="считать эмбеддинг и писать в Qdrant")
    args = ap.parse_args()

    urls = list(args.url)
    if args.search:
        p = _run_ytdlp([f"ytsearch{args.n}:{args.search}", "--flat-playlist",
                        "--print", "%(url)s"])
        urls += [u for u in p.stdout.splitlines() if u.startswith("http")]

    if not urls:
        ap.error("нужен --url или --search")

    ok = fail = 0
    for i, url in enumerate(urls):
        try:
            process_video(url, args.embed)
            ok += 1
        except Exception as e:  # noqa: BLE001 — фаза 0: логируем и идём дальше
            fail += 1
            print(f"  FAIL: {e}")
        if i < len(urls) - 1:
            time.sleep(config.YTDLP_SLEEP_SECONDS)  # не дразним YouTube (429)

    print(f"\nГотово: ok={ok}, fail={fail}. Кейсы в {config.DATA_DIR / 'cases.jsonl'}")


if __name__ == "__main__":
    main()
