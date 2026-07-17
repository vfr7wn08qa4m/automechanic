"""CI-этап «титры»: state:new -> транскрипт -> архив -> state:subs.

Работает на ОБЛАЧНОМ CircleCI-агенте: цепочка провайдеров транскрипта
(pipeline/subtitle_providers.py) не требует чистого IP — yt-dlp быстро
фейлится в датацентре, цепочка уходит в Invidious/Supadata/прокси.

Парные аккаунты: у аккаунта A расписание по чётным минутам + PARTITION=even,
у аккаунта B по нечётным + PARTITION=odd. Партиция делит work items по
чётности id, плюс каждый айтем атомарно клеймится (rev-test) — гонок нет.

    python scripts/ci_fetch_subs.py --batch 10 [--partition even|odd]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import html as _html                                          # noqa: E402

from pipeline import config                                   # noqa: E402
from pipeline.ado import AdoClient                            # noqa: E402
from pipeline.store import archive_blob                       # noqa: E402
from pipeline.subtitle_providers import (close_tubetranscript,  # noqa: E402
                                         transcript_for_item)
from pipeline.subtitles import to_prompt_text                 # noqa: E402


def fetch_subs_batch(ado, batch: int = 10, partition: str | None = None) -> int:
    """Разобрать батч state:new -> транскрипт -> тело тикета -> state:subs.
    Возвращает число успешно затранскрибированных (для idle-halt кольца)."""
    worker = f"subs-{partition or os.getenv('CI_ACCOUNT', 'solo')}"
    ids = ado.query_by_state("new", top=batch, partition=partition)
    print(f"work items в state:new (partition={partition}): {len(ids)}")

    ok = 0
    for wi_id in ids:
        if not ado.claim(wi_id, worker):
            print(f"  #{wi_id}: уже занят, пропуск")
            continue
        wi = ado.get(wi_id)
        vid = ado.video_id_from_title(wi["fields"]["System.Title"])
        if not vid:
            ado.set_state(wi_id, "failed", "no [vid:] marker in title")
            continue
        url = (AdoClient.source_url(wi)
               or f"https://www.youtube.com/watch?v={vid}")
        try:
            tr = transcript_for_item(url, vid)
            text = to_prompt_text(tr.lines)
            # транскрипт -> В ТЕЛО ТИКЕТА (ADO = база; Claude-агент читает body,
            # не пере-фетчит YouTube из облака, где DC-IP заблокирован)
            ado.append_description(wi_id,
                f"<hr><b>Transcript</b> ({_html.escape(tr.provider)}, "
                f"{_html.escape(tr.lang or '')}, {len(tr.lines)} строк)"
                f"<pre>{_html.escape(text[:150000])}</pre>")
            # топ-комменты YouTube -> тоже в тело тикета (золото: «у меня было то же…»)
            try:
                from pipeline.youtube_discovery import video_comments_api
                comments = video_comments_api(vid, 60)
                if comments:
                    chtml = "".join(
                        f"<p><small>{_html.escape(c['author'])} (+{c['likes']})</small>: "
                        f"{_html.escape(c['text'])}</p>" for c in comments)
                    ado.append_description(
                        wi_id, f"<hr><b>Top comments</b> ({len(comments)})<div>{chtml}</div>")
                    print(f"       +{len(comments)} комментов")
            except Exception:  # noqa: BLE001 — комменты не критичны
                pass
            key = archive_blob(f"subs/{vid}.{tr.lang or 'xx'}.{tr.raw_ext}", tr.raw)  # R2 опц.
            ado.set_state(
                wi_id, "subs",
                comment=(f"transcript ok: provider={tr.provider}, lang={tr.lang}, "
                         f"{len(tr.lines)} lines (в теле тикета)"),
                link=f"s3://{config.S3_BUCKET}/{key}" if key else "")
            ok += 1
            print(f"  #{wi_id} {vid}: ok ({tr.provider}, {tr.lang})")
        except Exception as e:  # noqa: BLE001
            ado.set_state(wi_id, "failed", comment=f"subs error: {e}")
            print(f"  #{wi_id} {vid}: FAIL {e}")
        time.sleep(config.YTDLP_SLEEP_SECONDS)  # пауза против 429 (для ytdlp-ветки)

    close_tubetranscript()   # закрыть headless-браузер провайдера, если поднимался
    print(f"итог батча: успешно {ok}/{len(ids)}")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--partition", choices=["even", "odd", "solo"],
                    default=os.getenv("PARTITION") or None)
    args = ap.parse_args()
    if args.partition == "solo":
        args.partition = None

    from pipeline.ci_budget import guard
    from pipeline.ci_trigger import ring_handoff
    if not guard(15):     # лимит исчерпан -> пропуск тика, эстафету передаём дальше
        ring_handoff("subs", worked=False,
                     partition=args.partition or "solo", batch=args.batch)
        return

    ado = AdoClient()
    ok = fetch_subs_batch(ado, args.batch, args.partition)
    ring_handoff("subs", worked=bool(ok),
                 partition=args.partition or "solo", batch=args.batch)
    # авто-индексация отдельным лейном: накопился distilled -> пнуть index-эстафету.
    if len(ado.query_by_state("distilled", top=3)) >= 3:
        ring_handoff("index", worked=True, batch=20)


if __name__ == "__main__":
    main()
