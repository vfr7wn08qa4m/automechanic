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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import config                          # noqa: E402
from pipeline.ado import AdoClient                   # noqa: E402
from pipeline.case_schema import RepairCase          # noqa: E402
from pipeline.embed import embed                     # noqa: E402
from pipeline.store import CASES_JSONL, qdrant_upsert, s3_client  # noqa: E402


def _case_from_body(wi: dict) -> RepairCase | None:
    """Кейс из ТЕЛА тикета (save-case кладёт RepairCase-JSON в <pre> после
    маркера). Это основной источник: облачный Claude-агент пишет только сюда."""
    desc = wi.get("fields", {}).get("System.Description", "") or ""
    m = _re.search(r"RepairCase.*?<pre>(.*?)</pre>", desc, _re.S)
    if not m:
        return None
    try:
        return RepairCase.model_validate_json(_html.unescape(m.group(1)))
    except Exception:  # noqa: BLE001
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
    Возвращает число проиндексированных (для idle-halt кольца)."""
    ids = ado.query_by_state("distilled", top=batch, partition=partition)
    print(f"work items в state:distilled: {len(ids)}")

    done = 0
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
            vec = embed([case.search_text()])[0]
            qdrant_upsert(case, vec)
            ado.set_state(wi_id, "indexed")
            done += 1
            print(f"  #{wi_id} {vid}: indexed")
        except Exception as e:  # noqa: BLE001
            ado.set_state(wi_id, "failed", comment=f"embed error: {e}")
            print(f"  #{wi_id} {vid}: FAIL {e}")

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
