"""superetka.com (ETKA online) — точечный lookup запчастей VAG для обогащения кейсов.

НЕ bulk-зеркало каталога: у сайта app-level throttle (после серии запросов PHP
отдаёт заглушку «Access only after payment» вместо данных — это НЕ подписка, данные
бесплатны, это анти-скрап-ограничение частоты). Поэтому модуль работает адресно:
марка[/модель/группа] -> детали с OEM-номерами, вежливо и с бэкоффом. Задача —
дополнить parts в конкретных RepairCase OEM-номерами, а не выкачать весь ETKA.

Сессия: кука PHPSESSID (живёт до ~ноя-2026). HAR-экспорт её вырезает, поэтому берём
из dump/superetka_cookie.txt (строка вида "PHPSESSID=...") или env SUPERETKA_COOKIE.

РАБОЧИЙ РЕЦЕПТ (проверен вживую 2026-07-11): кука PHPSESSID + обычный Chrome-UA,
БЕЗ referer и БЕЗ userPrivateAccept — иначе первые же запросы уводят в заглушку.
Троттл детектируется по телу ответа (короткий HTML со словами "after payment"/
"только после оплаты") -> экспоненциальный бэкофф, после N подряд — стоп с сохранением.

Карта навигации (механический каталог, VIN НЕ нужен):
  marke -> ajaxTvnModels (модели) -> ajaxYears (годы) -> ajaxShowMainGr (гл.группы)
  -> ajaxShowSubGr (mainGr -> подгруппы) -> ajaxSpareDetailsCurrent (детали: showDetail('OEM'))
  -> ajaxDetailMain (карточка: номер, описание, применимость, кол-во).
Марки: AU Audi, VW, SK Škoda, SE Seat, PO Porsche, BE Bentley, ML Lamborghini.

CLI:
    python -m pipeline.superetka --probe "cat=ajaxTvnModels&marke=AU&lang=EN"
    python -m pipeline.superetka --marke AU --market "" --max-models 3 --dump-raw
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests

from . import config

try:                              # UTF-8 вывод даже в cp1251/cp1252-консоли Windows
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "https://superetka.com/etka/index.php"
SHELL = "https://superetka.com/etka/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

SLEEP = float(os.getenv("SUPERETKA_SLEEP", "7"))       # пауза между запросами, c
MAX_THROTTLE = int(os.getenv("SUPERETKA_MAX_THROTTLE", "4"))  # подряд заглушек -> стоп
COOKIE_FILE = config.ROOT / "dump" / "superetka_cookie.txt"

OUT_JSONL = config.DATA_DIR / "superetka_parts.jsonl"
RAW_DIR = config.ROOT / "dump" / "superetka_raw"
STATE_FILE = config.DATA_DIR / "superetka_state.json"

# Троттл-заглушка: короткий ответ с этими маркерами вместо данных.
THROTTLE_MARKERS = ("after payment", "только после оплаты", "/ads")


class Throttled(Exception):
    """Сайт отдал заглушку вместо данных — надо притормозить."""


def load_cookie() -> str:
    """PHPSESSID-строка из env или dump/superetka_cookie.txt. userPrivateAccept НЕ шлём."""
    ck = os.getenv("SUPERETKA_COOKIE")
    if not ck and COOKIE_FILE.exists():
        ck = COOKIE_FILE.read_text(encoding="utf-8").strip()
    if not ck:
        sys.exit(f"нет куки: положи 'PHPSESSID=...' в {COOKIE_FILE} или env SUPERETKA_COOKIE")
    # оставляем ТОЛЬКО PHPSESSID — userPrivateAccept триггерит заглушку
    m = re.search(r"PHPSESSID=[0-9a-f]+", ck)
    if not m:
        sys.exit(f"в куке нет PHPSESSID: {ck[:40]}...")
    return m.group(0)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ru;q=0.8"})
    s.headers["Cookie"] = load_cookie()          # без referer/userPrivateAccept намеренно
    return s


def _is_throttle(text: str) -> bool:
    return len(text) < 800 and any(m in text for m in THROTTLE_MARKERS)


def _get(s: requests.Session, url: str, *, xhr: bool = True,
         dump_raw: bool = False, tag: str = "") -> str:
    """GET url с паузой, детектом троттла и бэкоффом.

    Бросает Throttled после MAX_THROTTLE подряд заглушек."""
    headers = {"X-Requested-With": "XMLHttpRequest"} if xhr else {}
    backoff = SLEEP
    for attempt in range(MAX_THROTTLE):
        time.sleep(SLEEP + random.uniform(0, SLEEP * 0.4))   # вежливый джиттер
        r = s.get(url, headers=headers, timeout=40)
        text = r.text
        if dump_raw:
            RAW_DIR.mkdir(parents=True, exist_ok=True)
            name = tag or re.sub(r"[^0-9A-Za-z]+", "_", url.split("?", 1)[-1])[:80]
            (RAW_DIR / f"{name}.html").write_text(text, encoding="utf-8")
        if r.status_code == 200 and not _is_throttle(text):
            return text
        backoff = min(backoff * 2, 120)
        print(f"  ~ throttle на '{url[-60:]}' (попытка {attempt+1}), "
              f"бэкофф {backoff:.0f}c", file=sys.stderr)
        time.sleep(backoff)
    raise Throttled(url)


def fetch(s: requests.Session, params: str, *, xhr: bool = True,
          dump_raw: bool = False) -> str:
    """GET ajax-эндпоинта index.php?<params>, напр. 'cat=ajaxShowMainGr&marke=AU&...'."""
    return _get(s, f"{BASE}?{params}", xhr=xhr, dump_raw=dump_raw)


def fetch_shell(s: requests.Session, marke: str, lang: str = "EN",
                dump_raw: bool = False) -> str:
    """GET страницы-оболочки /etka/?lang=&marke= — в ней уже вшит список моделей."""
    return _get(s, f"{SHELL}?lang={lang}&marke={marke}", xhr=False,
                dump_raw=dump_raw, tag=f"shell_{marke}")


# --- парсеры (регексные, устойчивые; сверяются по dump/superetka_raw на 1-м прогоне) ---

_DETAIL_ID = re.compile(r"showDetail\(\s*['\"]([^'\"]+)['\"]")
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_OPTION = re.compile(r"<option\s+value='(\d+)'>([A-Z0-9]+)</option>")


def _clean(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def parse_models(html: str) -> list[dict]:
    """Модели из shell-страницы: код -> числовой id (для след. вызовов) + имя/годы/рынки.

    id берётся из <option value='NNNN'>CODE</option>, остальное — из строки таблицы
    [code, name, production, markets]. Джойн по коду модели."""
    ids = {code: mid for mid, code in _OPTION.findall(html)}
    out = []
    for row in _ROW.findall(html):
        cells = [_clean(c) for c in _CELL.findall(row)]
        cells = [c for c in cells if c]
        if len(cells) >= 2 and re.match(r"^[A-Z0-9]{2,6}$", cells[0]) \
                and ("Audi" in " ".join(cells) or "VW" in " ".join(cells)
                     or ">>" in " ".join(cells)):
            out.append({"code": cells[0], "id": ids.get(cells[0], ""),
                        "name": cells[1],
                        "production": cells[2] if len(cells) > 2 else "",
                        "markets": cells[3] if len(cells) > 3 else ""})
    # если таблицы нет (другая марка/раскладка) — хотя бы коды+id из options
    if not out and ids:
        out = [{"code": c, "id": i, "name": "", "production": "", "markets": ""}
               for c, i in ids.items()]
    return out


def parse_detail_ids(html: str) -> list[str]:
    """OEM-номера из ajaxSpareDetailsCurrent (аргументы showDetail('...'))."""
    seen, out = set(), []
    for pid in _DETAIL_ID.findall(html):
        if pid not in seen:
            seen.add(pid); out.append(pid)
    return out


def parse_detail_main(html: str) -> dict:
    """Карточка детали (ajaxDetailMain): строки таблицы номер/описание/модель/кол-во."""
    rows = []
    for row in _ROW.findall(html):
        cells = [_clean(c) for c in _CELL.findall(row)]
        cells = [c for c in cells if c]
        if cells:
            rows.append(cells)
    return {"rows": rows, "text": _clean(html)[:800]}


# --- lookup ---------------------------------------------------------------------

def lookup_models(s: requests.Session, marke: str, lang: str = "EN",
                  dump_raw: bool = False) -> list[dict]:
    html = fetch_shell(s, marke, lang, dump_raw=dump_raw)
    return parse_models(html)


# Групповая цепочка. Параметры взяты из JS shell'а (tmphrefarr_): model = числовой id
# из parse_models, vin ОПЦИОНАЛЕН (не шлём). market='' = основной рынок (LOCAL).
def _q(**kw) -> str:
    return "&".join(f"{k}={v}" for k, v in kw.items())


def lookup_years(s, marke, model_id, market="", lang="EN", dump_raw=False) -> list[dict]:
    """Годы модели. Ответ — JSON {years:"<option value=ID>YYYY</option>..."}.
    ВНИМАНИЕ: year в след. вызовах — это числовой ID (value), не сам год."""
    raw = fetch(s, _q(cat="ajaxYears", lang=lang, marke=marke, market=market,
                      model=model_id), dump_raw=dump_raw)
    body = raw
    try:                                   # ответ обычно JSON с HTML внутри
        body = json.loads(raw).get("years", raw)
    except (ValueError, AttributeError):
        pass
    out = [{"year": disp, "id": yid}
           for yid, disp in re.findall(r"<option\s+value='(\d+)'>(\d+)</option>", body)]
    return out


_MAINGR = re.compile(r"select(?:Main)?Gr\w*\((\d)\b[^)]*\).*?>\s*(\d\.\s*[^<]+?)\s*<",
                     re.S)


def parse_main_groups(raw: str) -> list[dict]:
    """Главные группы VAG (0-9) из ajaxShowMainGr. Ответ — JSON{rightTable:HTML}."""
    body = raw
    try:
        body = json.loads(raw).get("rightTable", raw)
    except (ValueError, AttributeError):
        pass
    out, seen = [], set()
    for gid, label in re.findall(r"onclick='select\w*Gr\w*\((\d)[^)]*\)'>(.*?)</", body, re.S):
        name = _clean(label)
        if gid not in seen and name:
            seen.add(gid); out.append({"mainGr": gid, "name": name})
    return out


def lookup_main_groups(s, marke, model_id, year, market="", lang="EN", dump_raw=False):
    raw = fetch(s, _q(cat="ajaxShowMainGr", lang=lang, marke=marke, market=market,
                      model=model_id, year=year), dump_raw=dump_raw)
    return parse_main_groups(raw), raw


def lookup_parts(s, marke, model_id, year, market="", lang="EN", dump_raw=False):
    html = fetch(s, _q(cat="ajaxSpareDetailsCurrent", lang=lang, marke=marke,
                       market=market, model=model_id, year=year), dump_raw=dump_raw)
    return parse_detail_ids(html), html


def lookup_detail(s, marke, oem, lang="EN", cnt=1, dump_raw=False) -> dict:
    from urllib.parse import quote
    html = fetch(s, _q(cat="ajaxDetailMain", marke=marke, lang=lang,
                       detail=quote(oem), cnt=cnt), dump_raw=dump_raw)
    return parse_detail_main(html)


def append_parts(records: list[dict]) -> None:
    OUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with OUT_JSONL.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Точечный lookup запчастей superetka/ETKA")
    ap.add_argument("--probe", metavar="PARAMS",
                    help="один запрос (строка после '?'), сырой ответ в stdout+dump")
    ap.add_argument("--marke", help="код марки: AU VW SK SE PO BE ML")
    ap.add_argument("--lang", default="EN")
    ap.add_argument("--max-models", type=int, default=0, help="0 = только список моделей")
    ap.add_argument("--dump-raw", action="store_true",
                    help="сохранять сырые ответы в dump/superetka_raw для сверки парсеров")
    args = ap.parse_args()

    s = make_session()

    if args.probe:
        try:
            html = fetch(s, args.probe, dump_raw=True)
        except Throttled:
            sys.exit("троттл: сайт отдаёт заглушку — дай остыть (минуты) и повтори")
        print(f"# OK, {len(html)} байт (сырьё также в {RAW_DIR})")
        print(html[:2000])
        return

    if not args.marke:
        ap.error("нужен --marke или --probe")

    try:
        models = lookup_models(s, args.marke, args.lang, dump_raw=args.dump_raw)
    except Throttled:
        sys.exit("троттл на списке моделей — дай остыть и повтори (или увеличь SUPERETKA_SLEEP)")
    print(f"# {args.marke}: {len(models)} моделей")
    for m in models[:20]:
        print(f"  {m['code']:8} {m['name']:34} {m['production']:14} {m['markets']}")
    if len(models) > 20:
        print(f"  … +{len(models)-20}")

    # Запись справочника моделей (обогащение parts — следующий слой, по группам).
    append_parts([{"kind": "model", "marke": args.marke, **m} for m in models])
    print(f"# модели -> {OUT_JSONL}")
    if args.max_models:
        print("# TODO групповой обход: сверить формат ajaxShowMainGr/ShowSubGr/"
              "SpareDetailsCurrent по dump/superetka_raw, затем включить.")


if __name__ == "__main__":
    main()
