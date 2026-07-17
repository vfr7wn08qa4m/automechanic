#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Живой локальный борд AutoMech: очередь ADO по статусам, бюджеты минут по
аккаунтам, тикеты по хостам, свежие пайплайны CircleCI. Токены живут только на
сервере (localhost), в браузер не уходят. Данные из accounts.json (пульт).

    python scripts/board_server.py                 # http://localhost:8788
    python scripts/board_server.py --port 9000 --refresh 60
Проще — запусти automech_board.bat (поднимет сервер и откроет браузер).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
STATES = [("new", "New"), ("subs", "ReadyForFilter"), ("distilled", "ReadyForEmbeding"),
          ("indexed", "Closed"), ("failed", "Removed")]


def _cfg() -> dict:
    return json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))


def _ado():
    cfg = _cfg()
    az = cfg["azure"]
    os.environ.update(ADO_ORG=az["org"], ADO_PROJECT=az["project"], ADO_PAT=az["pat"])
    sys.path.insert(0, str(ROOT))
    from pipeline.ado import AdoClient
    return AdoClient(), cfg


def _cci(token: str, path: str):
    try:
        req = urllib.request.Request("https://circleci.com/api/v2" + path,
                                     headers={"Circle-Token": token, "Accept": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return {"_err": f"HTTP {e.code}: {body}", "_status": e.code}
    except Exception as e:  # noqa: BLE001
        return {"_err": str(e)[:80]}


def collect() -> dict:
    """Собрать все метрики. Каждый источник в try — один сбой не роняет борд."""
    data: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "errors": []}
    try:
        ado, cfg = _ado()
    except Exception as e:  # noqa: BLE001
        data["errors"].append(f"ADO init: {e}")
        return data

    # очередь по статусам: в каждом статусе — раскладка видео / форум,
    # чтобы было видно, что это (ютуб-транскрипты vs форум-треды), а не куча.
    try:
        def _cnt(real: str, forum: bool = False) -> int:
            q = ("SELECT [System.Id] FROM WorkItems "
                 f"WHERE [System.TeamProject]='{ado.project}' "
                 "AND [System.WorkItemType]='Task' "
                 f"AND [System.State]='{real}' "
                 "AND [System.Tags] CONTAINS 'auto-mech'")
            if forum:                        # форум-тикеты помечены vid:frm- в тайтле
                q += " AND [System.Title] CONTAINS 'vid:frm-'"
            return len(ado._wiql(q))
        queue = {}
        for logical, real in STATES:
            total = _cnt(real)
            forum = _cnt(real, True) if total else 0
            queue[logical] = {"t": total, "f": forum, "v": total - forum}
        data["queue"] = queue
    except Exception as e:  # noqa: BLE001
        data["errors"].append(f"queue: {e}")

    # тикеты по хостам (форумы, ЖИВЫЕ — без Removed) + всего.
    # Фильтруем по тегу auto-mech (наши) И исключаем Removed (брак не считаем).
    try:
        from collections import Counter
        ids = ado._wiql("SELECT [System.Id] FROM WorkItems "
                        f"WHERE [System.TeamProject]='{ado.project}' "
                        "AND [System.Tags] CONTAINS 'auto-mech' "
                        "AND [System.State] <> 'Removed' "
                        "AND [System.Title] CONTAINS 'vid:frm-'")
        # локальный краул drive2 метил тикеты 'drive2:' -> сливаем с www.drive2.ru
        host_alias = {"drive2": "www.drive2.ru"}
        c = Counter()
        for wi in ado.get_batch(ids, fields=("System.Title",)):
            t = wi["fields"].get("System.Title", "")
            host = t.split("] ", 1)[-1].split(":", 1)[0].strip() if "] " in t else "?"
            c[host_alias.get(host, host)] += 1
        data["hosts"] = c.most_common()
        data["forum_total"] = len(ids)
    except Exception as e:  # noqa: BLE001
        data["errors"].append(f"hosts: {e}")

    # бюджеты минут по аккаунтам
    try:
        data["budgets"] = sorted(ado.budget_ledgers(), key=lambda x: (x["account"], x["month"]))
        data["cap"] = int(os.getenv("CI_MONTHLY_MINUTES", "6000"))
    except Exception as e:  # noqa: BLE001
        data["errors"].append(f"budgets: {e}")

    # Qdrant: заполнение free-тира (1 ГБ RAM) — точки + резидентная память
    try:
        import re as _re
        ss = cfg.get("shared_secrets", {})
        qurl = (ss.get("QDRANT_URL") or "").rstrip("/")
        qkey = ss.get("QDRANT_API_KEY") or ""
        coll = ss.get("QDRANT_COLLECTION") or "cases"
        if qurl and qkey:
            def _qget(path: str) -> str:
                req = urllib.request.Request(qurl + path, headers={"api-key": qkey})
                return urllib.request.urlopen(req, timeout=15).read().decode()
            pts = json.loads(_qget(f"/collections/{coll}"))["result"].get("points_count", 0)
            m = _re.search(r"memory_resident_bytes\s+(\d+)", _qget("/metrics"))
            ram = int(m.group(1)) if m else 0
            cap_bytes = 1073741824  # 1 ГБ free
            data["qdrant"] = {"points": pts, "ram_mb": ram / 1048576,
                              "ram_pct": min(100, ram / cap_bytes * 100),
                              "cap_points": 250000}
    except Exception as e:  # noqa: BLE001
        data["errors"].append(f"qdrant: {e}")

    # ТРЕВОГА: аккаунт CircleCI заблокирован/токен не работает (401/402/403) —
    # dispatch тихо проваливается неделями, если это не подсветить (уже наступали:
    # ADO-пайплайн репортил success, пока оба аккаунта были suspended). Проверяем
    # дешёвым /me ДО остальных запросов — один вызов на аккаунт, не роняет борд.
    ci_alerts = []
    for a in cfg.get("accounts", []):
        tok = a.get("circleci_token")
        if not tok:
            continue
        r = _cci(tok, "/me")
        status = r.get("_status") if isinstance(r, dict) else None
        if status in (401, 402, 403):
            ci_alerts.append({"account": a.get("name", "?"), "status": status,
                              "msg": r.get("_err", "")})
    data["ci_alerts"] = ci_alerts

    # CircleCI: что делается СЕЙЧАС (видео/форум) + прогонов по дням (7д)
    try:
        today = datetime.now(timezone.utc).date()
        day_keys = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
        runs = {dk: 0 for dk in day_keys}
        active = []
        flows_by_acc = {}
        for a in cfg.get("accounts", []):
            tok, slug = a.get("circleci_token"), a.get("circleci_project_slug")
            if not tok or not slug:
                continue
            if any(x["account"] == a.get("name") for x in ci_alerts):
                continue    # аккаунт уже помечен нерабочим — не дёргать дальше зря
            token = None
            for _ in range(3):                        # до 3 страниц ≈ неделя истории
                path = f"/project/{slug}/pipeline?branch=main"
                if token:
                    path += f"&page-token={token}"
                r = _cci(tok, path)
                if not isinstance(r, dict):
                    break
                for p in r.get("items", []):
                    dk = (p.get("created_at") or "")[:10]
                    if dk in runs:
                        runs[dk] += 1
                token = r.get("next_page_token")
                if not token:
                    break
            # активность + разбивка недавних прогонов по типам: видно перекос
            # (краул молотит, а видео-subs не идут) — это и был слепой пятно.
            r = _cci(tok, f"/project/{slug}/pipeline?branch=main")
            fl = {}
            for p in (r.get("items", [])[:15] if isinstance(r, dict) else []):
                wf = _cci(tok, f"/pipeline/{p['id']}/workflow")
                for w in (wf.get("items", []) if isinstance(wf, dict) else []):
                    k = _flow_kind(w.get("name", ""))
                    fl[k] = fl.get(k, 0) + 1
                    if w.get("status") == "running":
                        active.append({
                            "acc": a["name"], "kind": k, "num": p.get("number"),
                            "url": f"https://app.circleci.com/pipelines/{slug}/"
                                   f"{p.get('number')}/workflows/{w.get('id')}"})
            flows_by_acc[a["name"]] = fl
        data["ci_runs"] = [{"day": dk[5:], "n": runs[dk]} for dk in day_keys]
        data["ci_active"] = active
        data["ci_flows"] = flows_by_acc
    except Exception as e:  # noqa: BLE001
        data["errors"].append(f"circleci: {e}")

    # бюджет минут CircleCI по аккаунтам (леджер в ADO) — ГДЕ ГОРЯТ КРЕДИТЫ.
    # Раньше не заполнялось -> таблица бюджета на борде была пустой (слепое пятно).
    try:
        from pipeline import ci_budget
        data["cap"] = ci_budget.CAP
        data["budgets"] = [
            {"account": a["name"],
             "month": datetime.now(timezone.utc).strftime("%Y-%m"),
             "minutes": ci_budget.used(a["name"])}
            for a in cfg.get("accounts", []) if a.get("name")]
    except Exception as e:  # noqa: BLE001
        data["errors"].append(f"budget: {e}")

    # Claude: кейсов по дням (7д) — сколько дистилляция выдала. Считаем ВСЕ готовые
    # кейсы (ReadyForEmbeding + уже проиндексированные Closed) по дате изменения:
    # авто-индексация быстро уводит их в Closed, поэтому один ReadyForEmbeding = 0.
    try:
        today = datetime.now(timezone.utc).date()
        day_keys = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
        buckets = {dk: 0 for dk in day_keys}
        dids = ado._wiql("SELECT [System.Id] FROM WorkItems "
                         f"WHERE [System.TeamProject]='{ado.project}' "
                         "AND [System.State] IN ('ReadyForEmbeding','Closed') "
                         "AND [System.Tags] CONTAINS 'auto-mech'")
        for wi in ado.get_batch(dids, fields=("System.ChangedDate",)):
            dk = (wi["fields"].get("System.ChangedDate", "") or "")[:10]
            if dk in buckets:
                buckets[dk] += 1
        data["claude_7d"] = [{"day": dk[5:], "n": buckets[dk]} for dk in day_keys]
    except Exception as e:  # noqa: BLE001
        data["errors"].append(f"claude_7d: {e}")
    return data


def _flow_kind(wf_name: str) -> str:
    """Имя воркфлоу CircleCI -> человекочитаемо, что делается."""
    n = (wf_name or "").lower()
    if "sub" in n:
        return "📺 видео-транскрипты"
    if "cn" in n or "autohome" in n:
        return "💬 форум CN"
    if "crawl" in n:
        return "💬 форумы"
    return wf_name or "?"


# --- рендер -------------------------------------------------------------------

DOT = {"success": "#2fd196", "running": "#f5c451", "failed": "#ff6b6b",
       "error": "#ff6b6b", "canceled": "#8a94a4", "on_hold": "#f5c451"}


def render(d: dict, refresh: int) -> str:
    def card(real, logical, cell):
        if isinstance(cell, dict):
            val = cell["t"]
            sub = f'📺 {cell["v"]} видео · 💬 {cell["f"]} форум'
        else:
            val, sub = cell, logical
        return (f'<div class="card"><div class="v">{val}</div>'
                f'<div class="l">{html.escape(real)}</div>'
                f'<div class="s">{html.escape(sub)}</div></div>')

    q = d.get("queue", {})
    cards = "".join(card(real, lg, q.get(lg, "—")) for lg, real in STATES)

    host_items = d.get("hosts", [])
    max_h = max((n for _, n in host_items), default=1) or 1  # масштаб от лидера
    hosts = "".join(
        f'<tr><td>{html.escape(h)}</td><td class="n">{n}</td>'
        f'<td class="bar"><i style="width:{max(2, n / max_h * 100):.0f}%"></i></td></tr>'
        for h, n in host_items)

    cap = d.get("cap", 6000)
    budg = "".join(
        f'<tr><td>{html.escape(b["account"])}</td><td>{html.escape(b["month"])}</td>'
        f'<td class="n">{b["minutes"]:.0f}/{cap}</td>'
        f'<td class="bar"><i style="width:{min(100, b["minutes"]/cap*100):.0f}%"></i></td></tr>'
        for b in d.get("budgets", []))

    def daybars(series, color="#2fd196"):
        mx = max((x["n"] for x in series), default=1) or 1
        return "".join(
            f'<tr><td>{html.escape(x["day"])}</td><td class="n">{x["n"]}</td>'
            f'<td class="bar"><i style="width:{max(2, x["n"] / mx * 100):.0f}%;'
            f'background:{color}"></i></td></tr>'
            for x in series)

    ci_bars = daybars(d.get("ci_runs", []))
    claude_bars = daybars(d.get("claude_7d", []), "#7aa2ff")

    qd = d.get("qdrant")
    qdrant_rows = ((
        f'<tr><td>кейсов (векторов)</td><td class="n">{qd["points"]}</td>'
        f'<td class="s">≈ вмещает до {qd["cap_points"]//1000}k на 1&nbsp;ГБ</td></tr>'
        f'<tr><td>RAM занято</td><td class="n">{qd["ram_mb"]:.0f}&nbsp;МБ / 1024</td>'
        f'<td class="bar"><i style="width:{max(2, qd["ram_pct"]):.0f}%"></i></td></tr>')
        if qd else '<tr><td class=s>нет данных</td></tr>')
    act = d.get("ci_active", [])
    active_html = (" · ".join(f'<b>{html.escape(a["acc"])}</b> {html.escape(a["kind"])}'
                              for a in act)
                   if act else "<span class='s'>простаивает — активных прогонов нет</span>")
    active_detail = ("".join(
        f'<div class="nowrow"><b>{html.escape(a["acc"])}</b> {html.escape(a["kind"])} · '
        f'<a href="{html.escape(a.get("url", ""))}" target="_blank" rel="noopener">'
        f'пайплайн #{a.get("num", "?")} ↗ (живые логи)</a></div>'
        for a in act) if act else "<div class='s'>сейчас ничего не крутится</div>")
    cif = d.get("ci_flows", {})
    flows_html = ("  ·  ".join(
        f'<b>{html.escape(acc)}</b> ' + ", ".join(f"{html.escape(k)} {n}" for k, n in fl.items())
        for acc, fl in cif.items()) or "<span class='s'>нет данных</span>")

    errs = ("<div class='err'>⚠ " + "; ".join(html.escape(e) for e in d["errors"]) + "</div>"
            if d.get("errors") else "")

    alerts = d.get("ci_alerts", [])
    alert_banner = ("".join(
        f'<div class="critical">⛔ CircleCI «{html.escape(a["account"])}» НЕ РАБОТАЕТ '
        f'(HTTP {a["status"]}) — dispatch не может запускать subs, очередь копится. '
        f'{html.escape(a["msg"])}</div>'
        for a in alerts) if alerts else "")

    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh}"><title>AutoMech · борд</title>
<style>
html,body{{margin:0;background:#090b0f;color:#e7ebf2;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:1000px;margin:0 auto;padding:22px}}
h1{{font-size:19px;margin:0 0 2px}} .ts{{color:#8a94a4;font-size:12px;margin-bottom:18px}}
.row{{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:22px}}
.card{{flex:1;min-width:120px;background:#12161d;border:1px solid #1e2530;border-radius:12px;padding:14px}}
.card .v{{font-size:26px;font-weight:700;color:#2fd196}} .card .l{{font-size:13px;margin-top:2px}}
.card .s{{font-size:11px;color:#8a94a4}}
h2{{font-size:13px;color:#8a94a4;text-transform:uppercase;letter-spacing:.5px;margin:0 0 8px}}
.box{{background:#12161d;border:1px solid #1e2530;border-radius:12px;padding:6px 14px;margin-bottom:20px}}
table{{width:100%;border-collapse:collapse;font-size:13px}} td{{padding:7px 6px;border-bottom:1px solid #171d26}}
tr:last-child td{{border:0}} .n{{text-align:right;font-variant-numeric:tabular-nums;color:#cfd6e0}}
.bar{{width:38%}} .bar i{{display:block;height:8px;background:#2fd196;border-radius:5px;opacity:.85}}
.s{{color:#8a94a4;font-size:12px}} .dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:middle}}
.err{{background:#2a1414;border:1px solid #4a1f1f;color:#ff9b9b;padding:8px 12px;border-radius:10px;font-size:12px;margin-bottom:16px}}
.critical{{background:#4a0f0f;border:2px solid #ff3b3b;color:#ffd6d6;padding:12px 16px;
border-radius:10px;font-size:14px;font-weight:600;margin-bottom:16px;box-shadow:0 0 0 3px rgba(255,59,59,.15)}}
.note{{color:#8a94a4;font-size:11px;line-height:1.5;padding:8px 2px 4px;border-top:1px solid #171d26;margin-top:4px}}
.tag{{width:26px;text-align:center}}
.two{{display:flex;gap:16px;flex-wrap:wrap}} .two .col{{flex:1;min-width:280px}}
.nowbar{{background:#12161d;border:1px solid #1e2530;border-radius:10px;padding:11px 14px;font-size:13px;margin-bottom:22px}}
details.nowbar summary{{cursor:pointer;outline:none;list-style-position:inside}}
details.nowbar summary::-webkit-details-marker{{color:#7aa2ff}}
.nowdetail{{margin-top:9px;padding-top:9px;border-top:1px solid #1e2530}}
.nowrow{{font-size:12px;padding:4px 0;color:#c7cede}}
.nowrow a{{color:#7aa2ff;text-decoration:none}} .nowrow a:hover{{text-decoration:underline}}
</style></head><body><div class="wrap">
<h1>🔧 AutoMech — борд конвейера</h1><div class="ts">обновлено {html.escape(d.get("ts",""))} · автообновление {refresh}с</div>
{alert_banner}
{errs}
<div class="row">{cards}</div>
<details class="nowbar"><summary>▶ сейчас в работе: {active_html}</summary><div class="nowdetail">{active_detail}</div></details>
<div class="nowbar">⚙ CircleCI · недавние прогоны по типам (перекос виден сразу): {flows_html}</div>
<h2>Форум-тикеты по хостам · всего {d.get("forum_total","—")}</h2>
<div class="box"><table>{hosts or '<tr><td class=s>нет данных</td></tr>'}</table></div>
<h2>Бюджет CircleCI-минут · free-пул на каждый аккаунт</h2>
<div class="box"><table>{budg or '<tr><td class=s>нет леджеров</td></tr>'}</table>
<div class="note">это компьют CircleCI (парсинг/транскрипт), не Azure. Azure DevOps —
только база тикетов, минут не тратит. У acc1 и acc2 свой пул ~{cap}&nbsp;мин/мес,
считаем сами в ADO. «solo» — прогоны без парного раздела (ручной запуск / дискавери).</div></div>
<h2>🧠 Векторная база Qdrant · free 1&nbsp;ГБ</h2>
<div class="box"><table>{qdrant_rows}</table></div>
<div class="two">
<div class="col"><h2>Прогонов CircleCI · 7 дней</h2>
<div class="box"><table>{ci_bars or '<tr><td class=s>нет данных</td></tr>'}</table></div></div>
<div class="col"><h2>🧩 Claude — кейсов · 7 дней</h2>
<div class="box"><table>{claude_bars or '<tr><td class=s>нет данных</td></tr>'}</table></div></div>
</div>
</div></body></html>"""


PLACEHOLDER = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="3"><title>AutoMech · сборка…</title>
<style>
html,body{margin:0;height:100%;background:#090b0f;color:#e7ebf2;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.c{height:100%;display:flex;flex-direction:column;align-items:center;
justify-content:center;gap:20px;text-align:center;padding:24px}
.s{width:46px;height:46px;border:4px solid rgba(47,209,150,.15);
border-top-color:#2fd196;border-radius:50%;animation:sp .8s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.dots::after{content:'';animation:dots 1.4s steps(1,end) infinite}
@keyframes dots{0%{content:''}25%{content:'.'}50%{content:'..'}75%{content:'...'}}
.t b{color:#e7ebf2;font-size:16px}.t{color:#8a94a4;font-size:13px;line-height:1.7}
</style></head><body><div class="c">
<div class="s"></div>
<div class="t"><b>Собираю борд<span class="dots"></span></b><br>
опрашиваю Azure DevOps и CircleCI (~10–20&nbsp;с) · страница обновится сама</div>
</div></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--refresh", type=int, default=90)
    ap.add_argument("--cache", type=int, default=45)
    args = ap.parse_args()

    state = {"html": None, "ts": 0.0, "building": False}
    lock = threading.Lock()

    def rebuild():
        try:
            state["html"] = render(collect(), args.refresh)
            state["ts"] = time.time()
        except Exception as e:  # noqa: BLE001
            print(f"! сборка упала: {e}")
        finally:
            state["building"] = False

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # тихо
            pass

        def do_GET(self):
            fresh = state["html"] and (time.time() - state["ts"] < args.cache)
            if not fresh and not state["building"]:
                state["building"] = True
                threading.Thread(target=rebuild, daemon=True).start()
            body = (state["html"] or PLACEHOLDER).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    print(f"AutoMech board -> http://localhost:{args.port}  (Ctrl+C — стоп)")
    ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()


if __name__ == "__main__":
    main()
