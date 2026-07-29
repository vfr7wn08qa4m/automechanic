"""ASR-бэкфилл: видео, у которых НЕ нашлось титров (state:failed), прогнать
через Whisper и вернуть в конвейер (state:subs), чтобы дистилляция их подобрала.

Закрывает те самые ~10-20% видео без субтитров (нишевые тачки и т.п.), которые
сейчас копятся в браке. Whisper-путь берётся из pipeline/asr.py (env ASR_PROVIDERS:
remote=Kaggle T4 / cloudflare=CF free / local=faster-whisper).

ГНАТЬ ЛОКАЛЬНО (домашний IP): yt-dlp качает аудио с YouTube — в датацентре 429,
как и на титрах. Распознавание можно куда угодно (CF free / локально / Kaggle).

Идемпотентно: успех -> state:subs + снимаем тег 'failed'; неудача -> тег
'asr-failed' (второй раз это видео не берём). Форумные тикеты не трогаем.

    python scripts/asr_backfill.py --batch 20
    ASR_PROVIDERS=cloudflare python scripts/asr_backfill.py --batch 50   # только CF
    ASR_PROVIDERS=local ASR_LOCAL_MODEL=large-v3 python scripts/asr_backfill.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Запуск из .bat идёт в cp1252 -> print() с кириллицей роняет весь скрипт
# (UnicodeEncodeError) ЕЩЁ ДО работы. Принудительно UTF-8, как в local_crawl_*.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Бутстрап кредов из accounts.json (как local_crawl_*/local_fetch_subs): дома .env
# нет, ADO + shared_secrets (в т.ч. CF_* для Whisper) кладём в env ДО импорта
# pipeline (config читает env при импорте). Уже заданный env (ASR_PROVIDERS и т.п.)
# НЕ перетираем (setdefault). На ADO-агенте accounts.json НЕТ — креды приходят из
# variable group через env; тогда файл просто пропускаем (env уже расставлен пайплайном).
_acc = ROOT / "accounts.json"
if _acc.exists():
    _cfg = json.loads(_acc.read_text(encoding="utf-8"))
    _az = _cfg.get("azure") or {}
    if _az.get("org"):
        os.environ.setdefault("ADO_ORG", _az["org"])
        os.environ.setdefault("ADO_PROJECT", _az["project"])
        os.environ.setdefault("ADO_PAT", _az["pat"])
    for _k, _v in (_cfg.get("shared_secrets") or {}).items():
        if _v:
            os.environ.setdefault(_k, str(_v))
os.environ.setdefault("ASR_PROVIDERS", "cloudflare,local")  # дома: CF free -> faster-whisper

import html as _html                                          # noqa: E402

from pipeline import config                                   # noqa: E402
from pipeline.ado import AdoClient                            # noqa: E402
from pipeline.store import archive_blob                       # noqa: E402
from pipeline.subtitle_providers import asr_transcript        # noqa: E402
from pipeline.subtitles import to_prompt_text                 # noqa: E402
from pipeline.transient import is_transient                   # noqa: E402


def _candidates(ado, batch: int) -> list[int]:
    """Свежий брак с [vid:]-видео (не форум), ещё не пробованный ASR'ом."""
    return ado._wiql(
        "SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject]='{ado.project}' "
        "AND [System.WorkItemType]='Task' "
        "AND [System.State]='Removed' "
        "AND [System.Tags] CONTAINS 'auto-mech' "
        "AND [System.Tags] CONTAINS 'failed' "
        "AND [System.Tags] NOT CONTAINS 'asr-failed' "
        "AND [System.Title] CONTAINS '[vid:' "
        "AND [System.Title] NOT CONTAINS 'vid:frm-' "
        "ORDER BY [System.ChangedDate] DESC", top=batch)


def _retag(ado, wi_id: int, add=(), remove=()) -> None:
    wi = ado.get(wi_id)
    tags = [t.strip() for t in (wi["fields"].get("System.Tags") or "").split(";")
            if t.strip()]
    for t in remove:
        if t in tags:
            tags.remove(t)
    for t in add:
        if t not in tags:
            tags.append(t)
    ado._patch(wi_id, [{"op": "add", "path": "/fields/System.Tags",
                        "value": "; ".join(tags)}])


def backfill(ado, batch: int) -> int:
    ids = _candidates(ado, batch)
    print(f"брак-видео без титров к ASR: {len(ids)}")
    ok = 0
    for wi_id in ids:
        wi = ado.get(wi_id)
        vid = ado.video_id_from_title(wi["fields"]["System.Title"])
        if not vid:
            continue
        print(f"  #{wi_id} {vid}: качаю аудио + Whisper...")
        try:
            tr = asr_transcript(vid)
            text = to_prompt_text(tr.lines)
            ado.append_description(
                wi_id,
                f"<hr><b>Transcript</b> ({_html.escape(tr.provider)}, "
                f"{len(tr.lines)} строк, ASR)"
                f"<pre>{_html.escape(text[:150000])}</pre>")
            key = archive_blob(f"subs/{vid}.asr.{tr.raw_ext}", tr.raw)  # R2 опц.
            ado.set_state(
                wi_id, "subs",
                comment=(f"ASR ok: provider={tr.provider}, {len(tr.lines)} lines "
                         f"(Whisper, в теле тикета)"),
                link=f"s3://{config.S3_BUCKET}/{key}" if key else "")
            _retag(ado, wi_id, remove=("failed",), add=("asr",))
            ok += 1
            print(f"    ✅ #{wi_id} {vid}: {len(tr.lines)} строк -> state:subs")
        except Exception as e:  # noqa: BLE001
            if is_transient(e):
                # 429 у CF Whisper / временный отказ convert1s. Тег asr-failed
                # НЕ вешаем: он навсегда выводит видео из выборки, а причина —
                # исчерпанный на минуту бесплатный тир, а не негодное видео.
                print(f"    ~ #{wi_id} {vid}: отложено (временно) {str(e)[:180]}")
            else:
                _retag(ado, wi_id, add=("asr-failed",))
                print(f"    ✗ #{wi_id} {vid}: {str(e)[:200]}")
        time.sleep(float(config.YTDLP_SLEEP_SECONDS or 0))
    print(f"итог: {ok}/{len(ids)} видео восстановлено через ASR")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=20)
    args = ap.parse_args()
    ado = AdoClient()
    backfill(ado, args.batch)


if __name__ == "__main__":
    main()
