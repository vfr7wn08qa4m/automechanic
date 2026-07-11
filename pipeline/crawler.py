"""Форумный краулер: обходит треды зоны, посты -> R2, тред -> ADO work item.

Свойства под требования:
- ЗОНЫ: один прогон обходит сайты одной зоны (forum_sites.py) -> разные
  CI-аккаунты бьют по разным хостам, IP не банится.
- ВЕЖЛИВОСТЬ: на каждый хост не чаще per_host_delay; хосты чередуются
  (берём тред того хоста, который дольше всех «отдыхал») -> нагрузка размазана.
- ТАЙМ-БОКС: прогон живёт не дольше --minutes (по умолчанию 18) и выходит,
  сохранив фронтир -> укладываемся в бюджет CI-джоба (<20 мин).
- ВОЗОБНОВЛЕНИЕ: фронтир и множество «увидено» лежат в R2
  (crawl/{zone}.json), переживают эфемерные агенты; следующий прогон продолжает.
- ВЫХОД: каждый тред -> child work item под Epic форума в state:subs, посты
  архивируются в R2 (subs/{uid}.{lang}.json). Дальше их берёт дистилляция.

CLI:
    python -m pipeline.crawler --zone a --minutes 18 [--create-workitems]
    python -m pipeline.crawler --zone a --dry-run --max-threads 5
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from urllib.parse import urljoin, urlparse

import requests

from . import config
from .forum_sites import ForumSite, sites_in_zone
from .forums import extract_posts, session_from_har
from .store import archive_blob

UA = {"User-Agent": "Mozilla/5.0 (compatible; AutoMechBot/0.1; +research; see /about)"}
FRONTIER_PREFIX = "crawl"


def thread_uid(url: str) -> str:
    return "frm-" + hashlib.sha1(url.encode()).hexdigest()[:12]


# --- состояние (фронтир + seen), переживает прогоны ----------------------------

def _state_key(zone: str) -> str:
    return f"{FRONTIER_PREFIX}/{zone}.json"


def load_state(zone: str) -> dict:
    """{'frontier': [[url, kind], ...], 'seen': [uid, ...], 'listing_seen': [...]}"""
    local = config.DATA_DIR / f"crawl_{zone}.json"
    if config.S3_ENDPOINT:
        try:
            from .store import s3_client
            body = s3_client().get_object(
                Bucket=config.S3_BUCKET, Key=_state_key(zone))["Body"].read()
            return json.loads(body)
        except Exception:  # noqa: BLE001 — первый прогон
            pass
    if local.exists():
        return json.loads(local.read_text(encoding="utf-8"))
    return {"frontier": [], "seen": [], "listing_seen": []}


def save_state(zone: str, state: dict) -> None:
    payload = json.dumps(state, ensure_ascii=False)
    (config.DATA_DIR / f"crawl_{zone}.json").write_text(payload, encoding="utf-8")
    if config.S3_ENDPOINT:
        archive_blob(_state_key(zone), payload)


def seed_frontier(state: dict, sites: list[ForumSite]) -> None:
    """Досеять стартовые URL, если фронтир пуст (первый прогон зоны)."""
    if state["frontier"]:
        return
    for site in sites:
        kind = "thread" if site.mode == "seed" else "listing"
        for url in site.seeds:
            state["frontier"].append([url, kind])


# --- разбор страниц ------------------------------------------------------------

def _site_for(url: str, sites: list[ForumSite]) -> ForumSite | None:
    host = urlparse(url).hostname or ""
    return next((s for s in sites if s.host in host or host in s.host), None)


def parse_listing(url: str, html: str, site: ForumSite) -> tuple[list[str], list[str]]:
    """(ссылки на треды, ссылки на след. листинги) с страницы-раздела."""
    hrefs = re.findall(r'href="([^"#]+)"', html)
    threads, listings = [], []
    for h in hrefs:
        full = urljoin(url, h)
        if site.thread_re and re.search(site.thread_re, full):
            threads.append(full.split("#")[0])
        elif site.listing_re and re.search(site.listing_re, full):
            listings.append(full.split("#")[0])
    # следующая страница текущего листинга
    if site.next_page:
        base = url.split("/page")[0].split("?")[0].rstrip("/")
        m = re.search(r"/page/?(\d+)", url) or re.search(r"page-(\d+)", url) \
            or re.search(r"page=(\d+)", url)
        cur = int(m.group(1)) if m else 1
        nxt = site.next_page.format(base=base + ("/" if site.host.endswith(".com")
                                                 and "xenforo" in site.engine else ""),
                                    n=cur + 1)
        # добавляем след. страницу, только если на текущей были треды
        if threads:
            listings.append(urljoin(url, nxt))
    return list(dict.fromkeys(threads)), list(dict.fromkeys(listings))


# --- краулер -------------------------------------------------------------------

def crawl(zone: str, minutes: float, create_workitems: bool,
          max_threads: int | None, har: str | None) -> None:
    sites = sites_in_zone(zone)
    if not sites:
        print(f"зона '{zone}' пуста")
        return
    print(f"зона '{zone}': сайты {[s.host for s in sites]}")

    state = load_state(zone)
    seed_frontier(state, sites)
    seen = set(state["seen"])
    listing_seen = set(state["listing_seen"])

    sess = session_from_har(har, sites[0].seeds[0]) if har else requests.Session()
    sess.headers.update(UA)

    ado = None
    shard_cache: dict[str, dict] = {}   # host -> {id, count, known, channel}
    if create_workitems:
        from .ado import AdoClient
        ado = AdoClient()

    last_hit: dict[str, float] = {}
    deadline = time.monotonic() + minutes * 60
    done = 0
    stats = {"threads": 0, "listings": 0, "wi": 0, "errors": 0}

    while state["frontier"] and time.monotonic() < deadline:
        if max_threads and stats["threads"] >= max_threads:
            break
        # выбрать элемент того хоста, который дольше всех «отдыхал» (чередование)
        idx = _pick_ready(state["frontier"], last_hit)
        url, kind = state["frontier"].pop(idx)
        site = _site_for(url, sites)
        if site is None:
            continue

        wait = site.per_host_delay - (time.monotonic() - last_hit.get(site.host, 0))
        if wait > 0:
            time.sleep(wait)
        last_hit[site.host] = time.monotonic()

        try:
            r = sess.get(url, timeout=30)
            if r.status_code != 200:
                stats["errors"] += 1
                continue
            html = r.text

            if kind == "listing":
                if url in listing_seen:
                    continue
                listing_seen.add(url)
                threads, listings = parse_listing(url, html, site)
                for t in threads:
                    if thread_uid(t) not in seen:
                        state["frontier"].append([t, "thread"])
                for l in listings:
                    if l not in listing_seen:
                        state["frontier"].append([l, "listing"])
                stats["listings"] += 1
                print(f"  listing {url[:70]} -> +{len(threads)} тредов, +{len(listings)} стр.")
            else:  # thread
                uid = thread_uid(url)
                if uid in seen:
                    continue
                host = urlparse(url).hostname or ""
                posts = extract_posts(html, host)
                posts = [p for p in posts if len(p["text"]) > 60][:200]
                if not posts:
                    seen.add(uid)
                    continue
                m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.I)
                title = (m.group(1).strip() if m else url)[:200]
                _archive_and_ticket(url, title, posts, site, ado, shard_cache, stats)
                seen.add(uid)
                stats["threads"] += 1
        except Exception as e:  # noqa: BLE001 — один битый URL не роняет прогон
            stats["errors"] += 1
            print(f"  ERR {url[:70]}: {str(e)[:80]}")

        done += 1
        if done % 20 == 0:  # периодически фиксируем прогресс
            _persist(zone, state, seen, listing_seen)

    _persist(zone, state, seen, listing_seen)
    left = len(state["frontier"])
    print(f"\nзона '{zone}': треды={stats['threads']} листинги={stats['listings']} "
          f"WI={stats['wi']} ошибки={stats['errors']}; в очереди осталось {left}")


def _pick_ready(frontier: list, last_hit: dict) -> int:
    """Индекс элемента, чей хост дольше всех не трогали (чередование хостов)."""
    now = time.monotonic()
    best_idx, best_idle = 0, -1.0
    for i, (url, _kind) in enumerate(frontier):
        host = urlparse(url).hostname or ""
        idle = now - last_hit.get(host, 0)
        if idle > best_idle:
            best_idle, best_idx = idle, i
    return best_idx


def _archive_and_ticket(url, title, posts, site, ado, shard_cache, stats) -> None:
    uid = thread_uid(url)
    payload = json.dumps({"title": title, "url": url, "lang": site.lang,
                          "posts": posts}, ensure_ascii=False)
    key = archive_blob(f"subs/{uid}.{site.lang}.json", payload)
    if ado is None:
        print(f"    thread [{len(posts)} постов] {title[:60]}  (dry: WI не создан)")
        return
    # кэш чанка на хост: форумы легко >1000 тредов -> цепочка чанков.
    # шард и счётчик кэшируются, чтобы не дёргать ADO на каждый тред (rate-limit).
    sc = shard_cache.get(site.host)
    if sc is None:
        channel = {"id": site.host, "name": site.host,
                   "url": f"https://{site.host}", "lang": site.lang}
        sid = ado.current_channel_shard(channel, "forum")
        sc = {"id": sid, "count": len(ado.list_child_video_ids(sid)),
              "known": ado.channel_all_child_video_ids(site.host, "forum"),
              "channel": channel}
        shard_cache[site.host] = sc
    if uid in sc["known"]:
        return
    if sc["count"] >= ado.CHUNK_CAP:              # текущий чанк забит -> следующий
        sc["id"] = ado.current_channel_shard(sc["channel"], "forum")
        sc["count"] = len(ado.list_child_video_ids(sc["id"]))
    wi = ado.create_video_item(
        {"id": uid, "title": f"{site.host}: {title}", "url": url,
         "channel": site.host, "channel_id": site.host}, parent_id=sc["id"])
    if wi:
        sc["known"].add(uid)
        sc["count"] += 1
        ado.set_state(wi, "subs",
                      comment=f"crawled: {len(posts)} постов",
                      link=f"s3://{config.S3_BUCKET}/{key}" if key else "")
        stats["wi"] += 1


def _persist(zone, state, seen, listing_seen) -> None:
    state["seen"] = list(seen)
    state["listing_seen"] = list(listing_seen)
    save_state(zone, state)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--zone", required=True, help="a | b | c (см. forum_sites.py)")
    ap.add_argument("--minutes", type=float, default=18.0, help="тайм-бокс прогона")
    ap.add_argument("--create-workitems", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="без ADO, просто показать")
    ap.add_argument("--max-threads", type=int, default=None)
    ap.add_argument("--har", help="HAR с сессией (если раздел за логином)")
    ap.add_argument("--reset", action="store_true", help="очистить фронтир зоны")
    args = ap.parse_args()

    if args.reset:
        save_state(args.zone, {"frontier": [], "seen": [], "listing_seen": []})
        print(f"фронтир зоны '{args.zone}' очищен")
        return

    crawl(args.zone, args.minutes,
          create_workitems=args.create_workitems and not args.dry_run,
          max_threads=args.max_threads, har=args.har)


if __name__ == "__main__":
    main()
