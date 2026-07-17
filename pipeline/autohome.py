"""club.autohome.com.cn — китайский авто-Q&A (вопросы-ответы по ремонту, ZH). ЗОЛОТО ZH.
ОТКРЫТИЕ (2026-07-13): frontapi/qa/hot отдаёт максимум 5 «страниц», но размер
страницы = pageSize, поэтому `pageSize=1000` -> ~2972 УНИКАЛЬНЫХ темы за 4 запроса
(весь пул Q&A, счётчик rowcount≈3464). Со старым pageSize=30 был потолок 150 —
оттого и стояли на 149. Прямой доступ работает С ДОМАШНЕГО IP (как drive2); датацентр/
реле сайт режет, поэтому краул ЛОКАЛЬНЫЙ (scripts/local_crawl_autohome.py), НЕ облачный.
Тред-страницы server-rendered: посты .tz-paragraph (вопрос) + .reply-detail (ответы) ->
тело ADO-тикета (state:subs), Claude дистиллирует наравне. ADO-дедуп (known) = резюме
между прогонами; hot со временем обновляется -> база копится сверх 2972.

    python -m pipeline.autohome --minutes 20 [--max-threads N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import time

import requests
from bs4 import BeautifulSoup

from .crawler import _posts_to_html, thread_uid

HOST = "club.autohome.com.cn"
QA_HOT = "https://club.autohome.com.cn/frontapi/qa/hot?pageSize={ps}&pageIndex={n}"
PAGE_SIZE = 1000                      # 5 стр. × 1000 -> весь пул hot Q&A (~2972 уник.)
# autohome пускает прямой домашний IP с обычным браузерным UA (чистый, без лишних заголовков).
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _sess() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = _UA
    return s


def _clean(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _thread_posts(html: str) -> list[dict]:
    """Посты треда autohome: .tz-paragraph (вопрос) + .reply-detail (ответы)."""
    soup = BeautifulSoup(html, "html.parser")
    posts: list[dict] = []
    for sel in (".tz-paragraph", ".reply-detail"):
        for el in soup.select(sel):
            t = _clean(el)
            if len(t) > 30:
                posts.append({"text": t})
    return posts


def _listing(sess: requests.Session) -> list[tuple[str, str, str]]:
    """Весь пул hot Q&A: pageSize=1000, страницы до пустоты. -> [(uid, turl, title)]."""
    seen: dict = {}
    for n in range(1, 7):                          # серверный потолок ~5 стр.
        try:
            r = sess.get(QA_HOT.format(ps=PAGE_SIZE, n=n), timeout=45)
        except Exception:  # noqa: BLE001
            break
        if r.status_code != 200:
            print(f"  qa/hot p{n}: HTTP {r.status_code}")
            break
        try:
            lst = json.loads(r.text).get("result", {}).get("list", []) or []
        except Exception:  # noqa: BLE001
            break
        if not lst:
            break
        for it in lst:
            tid = it.get("topicid")
            turl = (it.get("turl") or "").replace("http://", "https://")
            if tid and turl and tid not in seen:
                seen[tid] = (thread_uid(turl), turl, (it.get("title") or "")[:200])
    return list(seen.values())


def crawl_autohome(minutes: float = 20.0, max_threads: int | None = None,
                   create_workitems: bool = True) -> None:
    sess = _sess()
    listing = _listing(sess)
    print(f"autohome: в пуле hot Q&A уникальных тем={len(listing)}")
    if not listing:
        print("  пусто — нужен домашний/резидентный IP (датацентр autohome режет)")
        return

    ado = shard = None
    known: set[str] = set()
    count = 0
    if create_workitems:
        from .ado import AdoClient
        ado = AdoClient()
        channel = {"id": HOST, "name": "autohome club (Q&A)",
                   "url": f"https://{HOST}", "lang": "zh"}
        known = ado.channel_all_child_video_ids(HOST, "forum")
        shard = ado.current_channel_shard(channel, "forum")
        count = len(ado.list_child_video_ids(shard))

    todo = [x for x in listing if x[0] not in known]
    print(f"  уже в базе={len(known)} | к забору сейчас={len(todo)}")
    deadline = time.monotonic() + minutes * 60
    made = errors = 0
    for uid, turl, title in todo:
        if (max_threads and made >= max_threads) or time.monotonic() >= deadline:
            break
        time.sleep(random.uniform(2, 5))           # вежливость к хосту (рандомно)
        try:
            tr = sess.get(turl, timeout=45)
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        if tr.status_code != 200:
            errors += 1
            continue
        posts = _thread_posts(tr.text)[:200]
        if not posts:
            continue
        if ado is None:
            made += 1
            print(f"  [{made}] {title[:48]} — {len(posts)} постов")
            continue
        if count >= ado.CHUNK_CAP:
            channel = {"id": HOST, "name": "autohome club (Q&A)",
                       "url": f"https://{HOST}", "lang": "zh"}
            shard = ado.current_channel_shard(channel, "forum")
            count = len(ado.list_child_video_ids(shard))
        wi = ado.create_video_item(
            {"id": uid, "title": f"{HOST}: {title}", "url": turl,
             "channel": HOST, "channel_id": HOST},
            parent_id=shard, body_html=_posts_to_html(title, turl, posts))
        if wi:
            ado.set_state(wi, "subs",
                          comment=f"autohome: {len(posts)} постов (текст в тикете)")
            known.add(uid)
            count += 1
            made += 1
    print(f"\nautohome: тредов-тикетов={made} | ошибки={errors} | осталось≈{max(0, len(todo) - made)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--max-threads", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    crawl_autohome(args.minutes, args.max_threads, create_workitems=not args.dry_run)


if __name__ == "__main__":
    main()
