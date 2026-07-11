"""Этап «индексация»: state:distilled -> вектор -> Qdrant (+реплики) -> state:indexed.

Лёгкий сетевой этап — можно на любом агенте (CircleCI docker, ADO hosted, локально).

    python scripts/embed_index_batch.py --batch 50
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import config                          # noqa: E402
from pipeline.ado import AdoClient                   # noqa: E402
from pipeline.case_schema import RepairCase          # noqa: E402
from pipeline.embed import embed                     # noqa: E402
from pipeline.store import CASES_JSONL, qdrant_upsert, s3_client  # noqa: E402


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
    if not guard(10):     # месячный лимит минут исчерпан -> тихий выход
        return

    ado = AdoClient()
    ids = ado.query_by_state("distilled", top=args.batch, partition=args.partition)
    print(f"work items в state:distilled: {len(ids)}")

    for wi_id in ids:
        if not ado.claim(wi_id, f"embed-{args.partition or 'solo'}"):
            continue
        wi = ado.get(wi_id)
        vid = ado.video_id_from_title(wi["fields"]["System.Title"]) or ""
        case = load_case(vid)
        if case is None:
            ado.set_state(wi_id, "failed", comment="case json not found")
            continue
        try:
            vec = embed([case.search_text()])[0]
            qdrant_upsert(case, vec)
            ado.set_state(wi_id, "indexed")
            print(f"  #{wi_id} {vid}: indexed")
        except Exception as e:  # noqa: BLE001
            ado.set_state(wi_id, "failed", comment=f"embed error: {e}")
            print(f"  #{wi_id} {vid}: FAIL {e}")


if __name__ == "__main__":
    main()
