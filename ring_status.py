#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ring_status.py — монитор КОЛЬЦА AutoMech: статус последнего прогона по каждому
аккаунту + глубина очереди ADO (сколько работы ещё осталось кольцу).

Кольцо последовательное: активен один тик за раз (эстафета acc1->acc2->...->acc1),
остальные показывают статус СВОЕГО последнего пайплайна. «Все зелёные» = каждый
аккаунт хоть раз дал success (первый полный круг пройден). Флоу берётся из имени
воркфлоу (subs/crawl/index/crawl-cn/crawl-js/conveyor).

Очередь ADO (new -> subs -> distilled -> indexed) — это «топливо» кольца: пока в
new/subs/distilled есть тикеты, кольцо крутится; когда всё уходит в indexed и полный
круг проходит вхолостую, эстафета сама встаёт (см. RING_IDLE в pipeline/ci_trigger).

    python ring_status.py              # снимок сейчас
    python ring_status.py --watch      # следить, пока все не позеленеют / не покраснеет
"""
import sys
import json
import time
import argparse
import urllib.request
import urllib.error

API = "https://circleci.com/api/v2"
GREEN = {"success"}
RED = {"failed", "error", "canceled", "cancelled", "unauthorized", "timedout"}
ADO_STATES = ("new", "subs", "distilled", "indexed")

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def _api(url, tok):
    r = urllib.request.Request(url, headers={"Circle-Token": tok,
                                             "Accept": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def latest_run(slug, tok):
    """(number, flow, status, created_at) последнего пайплайна с воркфлоу, или (...,None,...).

    Флоу = имя воркфлоу (в config.yml каждый flow -> одноимённый воркфлоу). Берём самый
    свежий пайплайн, у которого вообще запустился воркфлоу."""
    try:
        pls = _api(f"{API}/project/{slug}/pipeline", tok).get("items", [])
    except Exception:                                # noqa: BLE001
        return None, None, "err", None
    for p in pls[:12]:
        try:
            wfs = _api(f"{API}/pipeline/{p['id']}/workflow", tok).get("items", [])
        except Exception:                            # noqa: BLE001
            continue
        if wfs:
            w = wfs[0]                               # один воркфлоу на flow-пайплайн
            return (p.get("number"), w.get("name"), w.get("status"),
                    (p.get("created_at") or "")[:19])
    return None, None, None, None


def ado_queue(az):
    """Глубина очереди по состояниям (best-effort; None если ADO недоступен).
    Переиспользует pipeline.ado.AdoClient — ставит креды из accounts.json в env."""
    if not (az and az.get("org") and az.get("project") and az.get("pat")):
        return None
    import os
    os.environ.setdefault("ADO_ORG", az["org"])
    os.environ.setdefault("ADO_PROJECT", az["project"])
    os.environ.setdefault("ADO_PAT", az["pat"])
    try:
        from pipeline.ado import AdoClient           # requests + config (env выше)
        ado = AdoClient()
        out = {}
        for st in ADO_STATES:
            n = len(ado.query_by_state(st, top=500))
            out[st] = f"{n}+" if n >= 500 else str(n)
        return out
    except Exception as e:                           # noqa: BLE001
        return {"_err": str(e)[:80]}


def mark(status):
    if status in GREEN:
        return "ЗЕЛЁНЫЙ ✔"
    if status in RED:
        return "КРАСНЫЙ ✘"
    if status in ("running", "on_hold", "on-hold"):
        return "идёт …"
    if status == "err":
        return "опрос не удался"
    return "— (прогонов ещё не было)"


def snapshot(accts):
    rows = []
    for a in accts:
        num, flow, st, when = latest_run(a["circleci_project_slug"], a["circleci_token"])
        rows.append((a["name"], num, flow, st, when))
    return rows


def main():
    ap = argparse.ArgumentParser(description="Монитор кольца AutoMech (статус + очередь ADO)")
    ap.add_argument("--accounts", default="accounts.json")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=int, default=90)
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--no-ado", action="store_true", help="не опрашивать очередь ADO")
    args = ap.parse_args()

    with open(args.accounts, encoding="utf-8") as f:
        cfg = json.load(f)
    accts = [a for a in cfg.get("accounts", [])
             if a.get("circleci_project_slug") and a.get("circleci_token")]
    az = cfg.get("azure") or {}
    total_slots = len(cfg.get("accounts", []))

    if not accts:
        sys.exit("нет подключённых аккаунтов (circleci_token + slug) — нечего мониторить")

    green_seen, deadline = set(), time.time() + args.timeout
    while True:
        closed = len(accts) == total_slots and len(accts) >= 2
        print(f"# кольцо: подключено {len(accts)}/{total_slots}"
              f"  ({'ЗАМКНУТО' if closed else 'открыто — ещё не стартует'})")

        if not args.no_ado:
            q = ado_queue(az)
            if q is None:
                print("  очередь ADO: (креды azure не заданы)")
            elif "_err" in q:
                print(f"  очередь ADO: опрос не удался ({q['_err']})")
            else:
                print("  очередь ADO: " + "  ".join(f"{s}={q[s]}" for s in ADO_STATES)
                      + "   (new/subs/distilled — топливо; всё в indexed = кольцо встанет)")

        rows = snapshot(accts)
        for name, num, flow, st, when in rows:
            if st in GREEN:
                green_seen.add(name)
            fl = f"[{flow}]" if flow else ""
            print(f"  {name:5} #{str(num) if num else '-':<5} {mark(st):22} "
                  f"{fl:11} {when or ''}")
        reds = [n for n, _, _, st, _ in rows if st in RED and n not in green_seen]
        g, tot = len(green_seen), len(accts)
        print(f"  → зелёных (хоть раз): {g}/{tot}"
              + (f" · КРАСНЫЕ сейчас: {', '.join(reds)}" if reds else ""))

        if not args.watch:
            break
        if g >= tot and tot > 0:
            print("\n✅ ВСЕ ЗЕЛЁНЫЕ — первый круг пройден, каждый аккаунт дал успешный прогон.")
            break
        if reds:
            print(f"\n❌ красный прогон у: {', '.join(reds)} — глянь лог в CircleCI UI. "
                  f"Перезапуск кольца: python deploy.py --accounts {args.accounts} --trigger")
            break
        if time.time() > deadline:
            print(f"\n⏳ таймаут — пока зелёных {g}/{tot}. Кольцо последовательное, "
                  f"круг идёт; запусти снова позже.")
            break
        print(f"  … жду {args.interval}s\n")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
