"""Этап «индексация»: state:distilled -> вектор -> Qdrant (+реплики) -> state:indexed.

Лёгкий сетевой этап — можно на любом агенте (CircleCI docker, ADO hosted, локально).

    python scripts/embed_index_batch.py --batch 50
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re as _re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Бутстрап кредов из accounts.json ДО импорта pipeline.config (он читает env при
# импорте): даёт локальный прогон эмбеддинга с дома (CF_* для Cloudflare bge-m3 +
# QDRANT_* + ADO_*). На CI-агенте accounts.json нет — env приходит из окружения.
_acc = ROOT / "accounts.json"
if _acc.exists():
    import os as _os
    import json as _json
    _cfg = _json.loads(_acc.read_text(encoding="utf-8"))
    _az = _cfg.get("azure") or {}
    if _az.get("org"):
        _os.environ.setdefault("ADO_ORG", _az["org"])
        _os.environ.setdefault("ADO_PROJECT", _az["project"])
        _os.environ.setdefault("ADO_PAT", _az["pat"])
    for _k, _v in (_cfg.get("shared_secrets") or {}).items():
        if _v:
            _os.environ.setdefault(_k, str(_v))

from pipeline import config                          # noqa: E402
from pipeline.ado import AdoClient                   # noqa: E402
from pipeline.case_schema import RepairCase          # noqa: E402
from pipeline.embed import embed                     # noqa: E402
from pipeline.store import CASES_JSONL, qdrant_upsert, s3_client  # noqa: E402
from pipeline.transient import is_transient          # noqa: E402


def _case_from_body(wi: dict) -> RepairCase | None:
    """Кейс из ТЕЛА тикета (save-case кладёт RepairCase-JSON в <pre> после
    маркера). Это основной источник: облачный Claude-агент пишет только сюда."""
    desc = wi.get("fields", {}).get("System.Description", "") or ""
    blocks = _re.findall(r"RepairCase.*?<pre>(.*?)</pre>", desc, _re.S)
    if not blocks:
        return None
    # ПОСЛЕДНИЙ блок, а не первый. Тело копит ИСТОРИЮ попыток дистилляции
    # (append_description дописывает новый блок, старые не убирает): найдено
    # 2026-07-31 — до 9 блоков в одном тикете, тело 232 КБ. re.search брал первый,
    # то есть в Qdrant уезжал самый СТАРЫЙ кейс, а свежая пере-дистилляция
    # выбрасывалась. Конкретно это хоронило переход Kimi -> Gemini: у тикетов
    # #2547/#2661/#2671/#2701 старый k3-блок стоит off_topic=true, свежий
    # gemini-блок — false. Идём с конца: если последний битый, откатываемся глубже.
    for raw in reversed(blocks):
        try:
            return RepairCase.model_validate_json(_html.unescape(raw))
        except Exception:  # noqa: BLE001 — битый блок пропускаем, берём предыдущий
            continue
    return None


def load_case(vid: str) -> RepairCase | None:
    if config.S3_ENDPOINT:
        try:
            body = s3_client().get_object(
                Bucket=config.S3_BUCKET, Key=f"cases/{vid}.json")["Body"].read()
            return RepairCase.model_validate_json(body)
        except Exception:  # noqa: BLE001 — попробуем локальный jsonl
            pass
    if CASES_JSONL.exists():
        for line in CASES_JSONL.read_text(encoding="utf-8").splitlines():
            data = json.loads(line)
            if data.get("source", {}).get("video_id") == vid:
                return RepairCase.model_validate(data)
    return None


def embed_batch(ado, batch: int = 50, partition: str | None = None) -> int:
    """Разобрать батч state:distilled -> вектор -> Qdrant -> state:indexed.
    Возвращает число проиндексированных (для idle-halt кольца).

    Если вся CF квота исчерпана на ALL токенах (10027), выбрасывает ошибку
    — батч отложится на следующий день (when RING_IDLE восстановится).
    """
    from pipeline.embed import _check_cf_quota

    has_quota, msg = _check_cf_quota()
    if not has_quota:
        raise RuntimeError(f"embeddings: {msg} — отложение на следующий день")
    if "unclear" not in msg and "network" not in msg:
        print(f"✓ {msg}")

    ids = ado.query_by_state("distilled", top=batch, partition=partition)
    print(f"work items в state:distilled: {len(ids)}")

    done = 0
    skipped = 0
    for wi_id in ids:
        if not ado.claim(wi_id, f"embed-{partition or 'solo'}"):
            continue
        wi = ado.get(wi_id)
        vid = ado.video_id_from_title(wi["fields"]["System.Title"]) or ""
        case = _case_from_body(wi) or load_case(vid)   # тело тикета -> фолбэк S3/jsonl
        if case is None:
            ado.set_state(wi_id, "failed", comment="case json not found")
            continue
        try:
            import time as _time
            for attempt in range(2):
                try:
                    vec = embed([case.search_text()])[0]
                    qdrant_upsert(case, vec)
                    ado.set_state(wi_id, "indexed")
                    done += 1
                    print(f"  #{wi_id} {vid}: indexed")
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 0 and is_transient(e) and "429" in str(e):
                        _time.sleep(2)
                        continue
                    raise
        except Exception as e:  # noqa: BLE001
            if is_transient(e):
                skipped += 1
                print(f"  #{wi_id} {vid}: ОТЛОЖЕН (временная ошибка) {str(e)[:120]}")
                continue
            ado.set_state(wi_id, "failed", comment=f"embed error: {e}")
            print(f"  #{wi_id} {vid}: FAIL {e}")

    if skipped:
        print(f"отложено из-за временных ошибок провайдера: {skipped}")
    print(f"итог: проиндексировано {done}/{len(ids)}")
    return done


def main() -> None:
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--partition", choices=["even", "odd", "solo"],
                    default=os.getenv("PARTITION") or None)
    args = ap.parse_args()
    if args.partition == "solo":
        args.partition = None

    from pipeline.ci_budget import guard
    from pipeline.ci_trigger import ring_handoff
    if not guard(10):     # лимит исчерпан -> пропуск тика, эстафету передаём дальше
        ring_handoff("index", worked=False,
                     partition=args.partition or "solo", batch=args.batch)
        return

    ado = AdoClient()
    done = embed_batch(ado, args.batch, args.partition)
    ring_handoff("index", worked=bool(done),
                 partition=args.partition or "solo", batch=args.batch)


if __name__ == "__main__":
    main()
