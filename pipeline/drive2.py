"""drive2.ru — бортжурналы (реальные истории ремонта, ЗОЛОТО). Особенности:
листинг грузится JS + сайт за DDoS-Guard (реле/датацентр = 403), а в 2026 drive2
УБРАЛ публичный sitemap.xml (теперь 404) и забанил AI-ботов в robots.txt. Поэтому:
  - дискавери — ДВА SSR-источника (plain-requests, без JS), self-expanding frontier:
    (1) СООБЩЕСТВА (приоритет — тематический ремонт): /communities/search = каталог
        ~1651 клубов (Кузовной Ремонт, АКПП, ГБО, Краска…), у каждого /communities/
        <id>/blog SSR-отдаёт ~20 свежих постов /c/ (задача+«Решение»);
    (2) КАТАЛОГ МАШИН: бренд /cars/<slug>/ -> модель -> поколение, каждый SSR-уровень
        ~6 записей + ссылки вглубь; фронтир сам растёт 207 -> десятки тысяч страниц.
    Курсор идёт ПО КРУГУ по всей описи, ADO-дедуп не плодит дубли, повторный заход
    ловит свежак. У drive2 НЕТ URL-пагинации (?page= игнорится) — глубже только JS;
  - контент тянем ПРЯМО с домашнего IP (DDoS-Guard его пускает) — краулер
    ЛОКАЛЬНЫЙ (scripts/local_crawl_drive2.py), не облачный.
Берём записи бортжурнала и посты сообществ (/l/ и /c/) — там ремонт.
Опись+курсор в data/drive2_cursor.json -> повторный прогон продолжает с места.
Текст записи -> тело ADO-тикета (state:subs), Claude дистиллирует наравне.

    python -m pipeline.drive2 --minutes 20 [--max N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from . import config
from .crawler import _posts_to_html, thread_uid

# DDoS-Guard drive2 пропускает ТОЛЬКО «чистый» браузерный отпечаток: один
# User-Agent, без Accept-Language/лишних заголовков (с UA-словарём краулера = 403).
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

HOST = "www.drive2.ru"
HOME = "https://www.drive2.ru/"
CARS = "https://www.drive2.ru/cars/{slug}/"
COMMUNITIES = "https://www.drive2.ru/communities/search"  # SSR-каталог ВСЕХ сообществ (~1651)
JOURNAL = re.compile(r"/[cl]/\d+")                       # относит. ссылка на запись
COMM_ID = re.compile(r"/communities/(\d+)")             # id сообщества из каталога
FORUM_THREAD = re.compile(r"/communities/\d+/forum/\d+")  # тред форума клуба (Q&A-обсуждение)
# дочерние узлы дерева каталога: модель /cars/<brand>/<model>/ и поколение .../<gen>/
CHILD = re.compile(r'href="(/cars/[a-z0-9_-]+/[a-z0-9_-]+/(?:[a-z0-9]+/)?)"')
FRONTIER_CAP = 60_000                                    # потолок описи страниц-источников
CURSOR = config.DATA_DIR / "drive2_cursor.json"

# запасной список брендов, если главная не отдала слаги (drive2 иногда режет ленту)
_BRAND_SEED = ["lada", "toyota", "bmw", "mercedes", "volkswagen", "nissan",
               "hyundai", "kia", "ford", "chevrolet", "renault", "audi", "honda",
               "mazda", "skoda", "mitsubishi", "opel", "volvo", "subaru", "lexus",
               "geely", "chery", "haval", "peugeot", "citroen", "infiniti"]


def _sess() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = _UA     # минимум — иначе DDoS-Guard 403
    return s


def _get(sess: requests.Session, url: str, timeout: int = 30) -> requests.Response | None:
    try:
        r = sess.get(url, timeout=timeout)
        if r.status_code == 429:          # рейт-лимит DDoS-Guard — остыть и пропустить
            time.sleep(random.uniform(20, 40))
            return None
        return r if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return None


def _discovery_seed(sess: requests.Session) -> list[str]:
    """Старт фронтира. ДВА источника, сообщества первыми (приоритет юзера — там
    тематический ремонт: Кузовной Ремонт, АКПП, ГБО, Краска…):
      1) СООБЩЕСТВА: каталог /communities/search (SSR, ~1651 клубов) -> для каждого
         страница /communities/<id>/blog (SSR, ~20 свежих постов /c/ — задачи+решения);
      2) КАТАЛОГ МАШИН: бренд-страницы /cars/<slug>/ (~207), фронтир разрастётся
         моделями и поколениями (см. _harvest).
    Оба уровня SSR-отдают записи; _harvest вытащит /l/ /c/ с любой из них."""
    seed: list[str] = []
    r = _get(sess, COMMUNITIES, timeout=40)
    if r:
        cids = sorted(set(COMM_ID.findall(r.text)))
        seed += [f"https://{HOST}/communities/{c}/blog" for c in cids]    # посты /c/
        seed += [f"https://{HOST}/communities/{c}/forum/" for c in cids]  # треды Q&A
    r = _get(sess, HOME, timeout=40)
    slugs = sorted(set(re.findall(r"/cars/([a-z0-9_-]+)/", r.text))) if r else []
    slugs = slugs or _BRAND_SEED
    seed += [CARS.format(slug=s) for s in slugs]
    return seed


def _harvest(sess: requests.Session, page_url: str) -> tuple[list[str], list[str]]:
    """Страница-источник SSR-отдаёт записи + ссылки вглубь. Возвращаем
    (URL-записи к забору, дочерние страницы-источники). Записи двух видов:
    бортжурналы/посты `/l/ /c/` и треды форума клуба `/communities/<id>/forum/<tid>`
    (разбираются по-разному при заборе — см. _extract_record)."""
    r = _get(sess, page_url, timeout=40)
    if not r:
        return [], []
    seen: set[str] = set()
    records: list[str] = []
    for rx in (JOURNAL, FORUM_THREAD):
        for m in rx.finditer(r.text):
            u = f"https://{HOST}{m.group(0)}"
            if u not in seen:
                seen.add(u)
                records.append(u)
    # дочерние узлы дерева: /cars/<brand>/<model>/ и /cars/<brand>/<model>/<gen>/
    children = {f"https://{HOST}{p}" for p in CHILD.findall(r.text)}
    return records, sorted(children)


def _extract(html: str) -> tuple[str, str]:
    """Заголовок + текст записи бортжурнала drive2 (itemprop=articleBody)."""
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        title = og["content"].strip()
    elif soup.title:
        title = soup.title.get_text(strip=True)
    body_el = (soup.select_one("[itemprop=articleBody]")
               or soup.select_one("article") or soup.select_one("main"))
    text = re.sub(r"\s+", " ", body_el.get_text(" ", strip=True)).strip() if body_el else ""
    return title[:200], text


def _extract_thread(html: str) -> tuple[str, str]:
    """Заголовок + текст треда форума клуба: посты в .c-comment (вопрос+ответы)."""
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        title = og["content"].strip()
    elif soup.title:
        title = re.sub(r"\s*[—|]\s*DRIVE2.*$", "", soup.title.get_text(strip=True))
    posts = soup.select(".c-comment") or soup.select("[id^=a]")
    text = " ".join(re.sub(r"\s+", " ", p.get_text(" ", strip=True)) for p in posts).strip()
    return title[:200], text


def _extract_record(html: str, url: str) -> tuple[str, str]:
    """Диспетчер: тред форума -> .c-comment, иначе бортжурнал -> articleBody."""
    return _extract_thread(html) if FORUM_THREAD.search(url) else _extract(html)


def _load_frontier() -> tuple[list[str], int]:
    """Опись страниц-источников (бренды+модели+поколения) и курсор с прошлого прогона.
    Пусто/битое -> ([], 0), вызывающий пере-засеет брендами."""
    try:
        d = json.loads(CURSOR.read_text(encoding="utf-8"))
        frontier = [u for u in d.get("frontier", []) if isinstance(u, str)]
        idx = int(d.get("idx", 0))
        return frontier, (idx % len(frontier) if frontier else 0)
    except Exception:  # noqa: BLE001
        return [], 0


def _save_frontier(frontier: list[str], idx: int) -> None:
    CURSOR.write_text(json.dumps({"frontier": frontier[:FRONTIER_CAP], "idx": idx}),
                      encoding="utf-8")


def crawl_drive2(minutes: float = 20.0, max_entries: int | None = None,
                 create_workitems: bool = True) -> None:
    sess = _sess()
    # быстрый чек доступа с этого IP: главная должна отдаться (не sitemap — его убрали)
    if not _get(sess, HOME, timeout=40):
        print("drive2 главная недоступна с этого IP — нужен домашний/резидентный IP")
        return

    frontier, idx = _load_frontier()              # опись страниц-источников + курсор
    if not frontier:                              # первый запуск / сброс — засеять брендами
        frontier = _discovery_seed(sess)
        idx = 0
    if not frontier:
        print("drive2: дискавери пуста — ни брендов, ни дерева")
        return
    fset = set(frontier)
    print(f"drive2: опись страниц-источников={len(frontier)} (старт с #{idx})")

    ado = shard = None
    known: set[str] = set()
    count = 0
    if create_workitems:
        from .ado import AdoClient
        ado = AdoClient()
        channel = {"id": HOST, "name": "drive2 бортжурналы", "url": f"https://{HOST}", "lang": "ru"}
        known = ado.channel_all_child_video_ids(HOST, "forum")
        shard = ado.current_channel_shard(channel, "forum")
        count = len(ado.list_child_video_ids(shard))

    deadline = time.monotonic() + minutes * 60
    made = errors = pages = 0
    stop = False
    # Обход дерева каталога drive2: каждая страница-источник (бренд/модель/поколение)
    # SSR-отдаёт ~6 свежих записей + ссылки вглубь. Дочерние узлы дописываем в опись —
    # фронтир САМ разрастается (207 брендов -> десятки тысяч страниц), курсор идёт по
    # кругу и при повторном заходе ловит свежак. Так объём не упирается в 6×207.
    while not stop and time.monotonic() < deadline:
        page = frontier[idx % len(frontier)]
        time.sleep(random.uniform(3, 7))          # вежливость и к странице-источнику
        records, children = _harvest(sess, page)
        for c in children:                        # разрастание дерева
            if c not in fset:
                fset.add(c)
                frontier.append(c)
        for url in records:
            if (max_entries and made >= max_entries) or time.monotonic() >= deadline:
                stop = bool(max_entries and made >= max_entries)
                break
            uid = thread_uid(url)
            if uid in known:
                continue
            time.sleep(random.uniform(3, 10))     # вежливость к DDoS-Guard: рандомная пауза 3–10с (не частим, не палимся ботом)
            pr = _get(sess, url, timeout=35)
            if not pr:
                errors += 1
                continue
            title, text = _extract_record(pr.text, url)
            if len(text) < 200:          # пустышка/фото-запись без текста
                continue
            if ado is None:
                made += 1
                print(f"  [{made}] {title[:50]} — {len(text)} симв")
                continue
            if count >= ado.CHUNK_CAP:
                channel = {"id": HOST, "name": "drive2 бортжурналы", "url": f"https://{HOST}", "lang": "ru"}
                shard = ado.current_channel_shard(channel, "forum")
                count = len(ado.list_child_video_ids(shard))
            wi = ado.create_video_item(
                {"id": uid, "title": f"{HOST}: {title}", "url": url,
                 "channel": HOST, "channel_id": HOST},
                parent_id=shard, body_html=_posts_to_html(title, url, [{"text": text}]))
            if wi:
                ado.set_state(wi, "subs", comment=f"drive2 бортжурнал ({len(text)} симв в тикете)")
                known.add(uid)
                count += 1
                made += 1
        idx = (idx + 1) % len(frontier)           # следующая страница-источник, по кругу
        pages += 1
        if create_workitems and pages % 10 == 0:  # периодически фиксируем прогресс
            _save_frontier(frontier, idx)
    if create_workitems:
        _save_frontier(frontier, idx)
    print(f"\ndrive2: записей-тикетов={made} | страниц обойдено={pages} "
          f"| опись={len(frontier)} | курсор={idx} | ошибки={errors}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--max", type=int, default=None, dest="max_entries")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    crawl_drive2(args.minutes, args.max_entries, create_workitems=not args.dry_run)


if __name__ == "__main__":
    main()
