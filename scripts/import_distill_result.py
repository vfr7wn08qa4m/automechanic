"""Импорт готового JSON-кейса дистилляции НАЗАД в ADO (замыкает петлю).

Берёт JSON, который вернула любая модель по задаче из export_distill_task.py,
ДОТЯГИВАЕТ блок `source` из самого тикета (video_id/url/канал/тип — модель его
обычно не заполняет), валидирует по схеме RepairCase, кладёт кейс в тело тикета и
переводит его в ReadyForEmbeding (distilled) — дальше этап embed/index сам
векторизует. off_topic=true -> Removed(offtopic), брак не векторизуем.

    python scripts/import_distill_result.py 3222 case.json
    python scripts/import_distill_result.py 3222 -          # JSON из stdin
"""
from __future__ import annotations

import json
import os
import re
import sys
import html as _html
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_acc = ROOT / "accounts.json"
if _acc.exists():
    _az = json.loads(_acc.read_text(encoding="utf-8")).get("azure") or {}
    if _az.get("org"):
        os.environ.setdefault("ADO_ORG", _az["org"])
        os.environ.setdefault("ADO_PROJECT", _az["project"])
        os.environ.setdefault("ADO_PAT", _az["pat"])

from pipeline import config                       # noqa: E402
from pipeline.ado import AdoClient                 # noqa: E402
from pipeline.case_schema import RepairCase        # noqa: E402
from pipeline.store import archive_blob            # noqa: E402


def _enrich_source(case: dict, wi: dict, wi_id: int) -> dict:
    """Проставить source из тикета, если модель его не заполнила."""
    src = case.get("source") or {}
    title = wi["fields"].get("System.Title", "") or ""
    m = re.search(r"vid:([A-Za-z0-9_-]{6,})", title)
    vid = m.group(1) if m else ""
    is_forum = "vid:frm-" in title
    clean_title = re.sub(r"^\s*\[vid:[^\]]+\]\s*", "", title).strip()
    url = AdoClient.source_url(wi) or (
        f"https://www.youtube.com/watch?v={vid}" if vid and not is_forum else "")
    src.setdefault("type", "forum" if is_forum else "youtube")
    src.setdefault("video_id", "" if is_forum else vid)
    src.setdefault("url", url)
    src.setdefault("title", clean_title)
    src.setdefault("lang", case.get("lang", ""))
    case["source"] = src
    return case


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: import_distill_result.py <wi_id> <case.json|->"); return
    wi_id = int(sys.argv[1])
    raw = (sys.stdin.read() if sys.argv[2] == "-"
           else Path(sys.argv[2]).read_text(encoding="utf-8"))
    case_dict = json.loads(raw)

    ado = AdoClient()
    wi = ado.get(wi_id)
    case_dict = _enrich_source(case_dict, wi, wi_id)

    case = RepairCase.model_validate(case_dict)     # падаёт при несоответствии схеме
    case.distill_model = case.distill_model or "manual-import"

    vid = case.source.video_id or f"wi-{wi_id}"
    key = archive_blob(f"cases/{vid}.json", case.model_dump_json())
    ado.replace_case_block(wi_id,
        f"<hr><b>RepairCase</b> (system: {_html.escape(case.system or '')}, "
        f"conf {case.confidence}) <pre>{_html.escape(case.model_dump_json())}</pre>")
    state = "distilled" if not case.off_topic else "offtopic"
    ado.set_state(wi_id, state,
                  comment=f"case: {case.system} | {case.problem_summary[:120]}",
                  link=f"s3://{config.S3_BUCKET}/{key}" if key else "")
    n_rules, n_pit = len(case.rules), len(case.pitfalls)
    print(f"#{wi_id}: -> {state} | rules={n_rules} pitfalls={n_pit} "
          f"conf={case.confidence} | source={case.source.type}:{case.source.video_id}")
    if state == "distilled":
        print("готово: тикет в ReadyForEmbeding — embed/index векторизует его.")


if __name__ == "__main__":
    main()
