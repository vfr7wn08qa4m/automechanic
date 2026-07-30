"""Реестр форумов для краулера: движок, seed-разделы, паттерны ссылок, ЗОНА.

Зона (zone) — группа сайтов, которую обходит один CI-аккаунт по своему
расписанию. Разные зоны = разные хосты в разное время = один IP не молотит
один форум. Парные аккаунты: зона 'a' на аккаунте A, зона 'b' на аккаунте B.

thread_re — как отличить ссылку на ТРЕД (страницу с постами).
listing_re — как отличить ссылку-ЛИСТИНГ (раздел/страница списка тредов).
next_page — шаблон следующей страницы листинга ({url}, {n}); None = без пагинации.
seeds — стартовые URL разделов (расширяй; можно вытащить из твоих HAR).
mode: "crawl" — полный обход разделов; "seed" — только заданные thread-URL
      (для сайтов, где список грузится JS/через API, напр. drive2).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ForumSite:
    host: str
    engine: str                      # ipb | xenforo | vbulletin | phpbb | custom
    lang: str
    zone: str
    seeds: list[str] = field(default_factory=list)
    thread_re: str = ""
    listing_re: str = ""
    next_page: str | None = None
    mode: str = "crawl"
    per_host_delay: float = 6.0      # сек между запросами к этому хосту
    render_js: bool = False          # True -> тянуть через headless-браузер
                                     # (Cloudflare JS-челлендж); зона запускается
                                     # Playwright-джобой crawl-forums-js


SITES: dict[str, ForumSite] = {
    # --- ЗОНА A: RU (приоритет) ---------------------------------------------
    "carmasters.org": ForumSite(
        host="carmasters.org", engine="ipb", lang="ru", zone="a",
        seeds=[
            "https://carmasters.org/forum/3971-bmw/",
            "https://carmasters.org/forum/5-форум-по-диагностике/",
        ],
        thread_re=r"https://carmasters\.org/topic/\d+[^\"#?]*",
        listing_re=r"https://carmasters\.org/forum/\d+[^\"#?]*",
        next_page="{base}/page/{n}/",   # IPS: /forum/3971-bmw/page/2/
    ),
    "drive2.ru": ForumSite(
        host="www.drive2.ru", engine="custom", lang="ru", zone="a",
        # листинг сообществ грузится JS -> обходим из seed-ссылок на записи
        # (пополняй из HAR: страницы /c/{id} и /l/{id})
        seeds=[
            "https://www.drive2.ru/c/734240946840933339/",
            "https://www.drive2.ru/communities/729091659010147439/",
        ],
        thread_re=r"https://www\.drive2\.ru/[cl]/\d+[^\"#?]*",
        mode="seed",
    ),

    # --- ЗОНА B: EN (доступ через Worker-реле CRAWL_PROXY) -------------------
    "bimmerforums.com": ForumSite(
        host="www.bimmerforums.com", engine="vbulletin", lang="en", zone="b",
        # доступен ТОЛЬКО через Worker-реле (CRAWL_PROXY) — прямой доступ = 403 CF
        seeds=["https://www.bimmerforums.com/forum/"],
        thread_re=r"showthread\.php\?\d+",       # vBulletin: showthread.php?2386564-title
        listing_re=r"forumdisplay\.php\?\d+",    # forumdisplay.php?12-e46
        next_page="{base}/page{n}",              # vBulletin: forumdisplay.php?12-e46/page2
    ),

    # --- ЗОНА D: EN за активным JS-челленджем Cloudflare (headless-браузер) ---
    # Проверено: Playwright решает челлендж vwvortex (bobistheoilguy — Turnstile,
    # жёстче; пробуем со stealth). Зону d крутит CI-джоба crawl-forums-js.
    "vwvortex.com": ForumSite(
        host="www.vwvortex.com", engine="xenforo", lang="en", zone="d", render_js=True,
        seeds=["https://www.vwvortex.com/forums/"],
        thread_re=r"/threads/[^\"#?]+\.\d+/?$",
        listing_re=r"/forums/[^\"#?]+\.\d+/?$",
        next_page="{base}page-{n}",     # XenForo: /forums/xxx.12/page-2
    ),
    # bobistheoilguy — ОТКЛЮЧЁН (zone=off): Turnstile-челлендж, headless не берёт.
    # Хочешь позже — stealth-докрутка или парс dump/bobistheoilguy.com.har.
    "bobistheoilguy.com": ForumSite(
        host="bobistheoilguy.com", engine="xenforo", lang="en", zone="off", render_js=True,
        seeds=["https://bobistheoilguy.com/forums/"],
        thread_re=r"/threads/[^\"#?]+\.\d+/?$",
        listing_re=r"/forums/[^\"#?]+\.\d+/?$",
        next_page="{base}page-{n}",
    ),

    # --- ЗОНА C: прочие языки / API-сайты ------------------------------------
    # ИСПРАВЛЕНО 2026-07-30: реальная структура сайта — /{марка}/{модель}/defectos/
    # {id}/{slug}, а НЕ /pregunta (тред такого пути на сайте не существует вообще).
    # thread_re не совпадал НИ С ОДНОЙ страницей с первого дня — за всю историю
    # прошёл 1 тикет. Проверено вживую (requests): /elegirmarca -> 81 марка ->
    # /{марка}/{модель} -> /{марка}/{модель}/defectos -> до 30 тредов на модель.
    # Первый сегмент пути ограничен ≥3 симв., чтобы не цеплять гео-варианты сайта
    # (/mx/toyota/..., /ar/..., 2-буквенные коды стран — у брендов их не бывает).
    "opinautos.com": ForumSite(
        host="www.opinautos.com", engine="custom", lang="es", zone="c",
        seeds=["https://www.opinautos.com/elegirmarca"],
        thread_re=r"https://www\.opinautos\.com/[a-z][a-z0-9-]{2,}/[a-z0-9-]+"
                  r"/defectos/\d+/[^\"#?]+",
        listing_re=r"https://www\.opinautos\.com/[a-z][a-z0-9-]{2,}"
                   r"(?:/elegirmodelo|/[a-z0-9-]+(?:/defectos)?)?$",
        mode="crawl",
    ),
    # motor-talk.de -> отдельный GraphQL-клиент (не HTML-краул), см. sources-scan.md
    # reddit -> OAuth API-клиент; autohome.com.cn -> позже
}


def sites_in_zone(zone: str) -> list[ForumSite]:
    return [s for s in SITES.values() if s.zone == zone]
