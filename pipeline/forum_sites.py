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

    # --- ЗОНА B: EN ----------------------------------------------------------
    "vwvortex.com": ForumSite(
        host="www.vwvortex.com", engine="xenforo", lang="en", zone="b",
        seeds=["https://www.vwvortex.com/forums/"],
        thread_re=r"/threads/[^\"#?]+\.\d+/?$",
        listing_re=r"/forums/[^\"#?]+\.\d+/?$",
        next_page="{base}page-{n}",     # XenForo: /forums/xxx.12/page-2
    ),
    "bimmerforums.com": ForumSite(
        host="www.bimmerforums.com", engine="vbulletin", lang="en", zone="b",
        seeds=["https://www.bimmerforums.com/forum/"],
        thread_re=r"/forum/[^\"#?]*\d+-[^\"#?]+",
        listing_re=r"/forum/forumdisplay[^\"#?]*|/forum/[^\"#?]*forum[^\"#?]*",
        next_page="{base}/page{n}",
    ),
    "bobistheoilguy.com": ForumSite(
        host="bobistheoilguy.com", engine="xenforo", lang="en", zone="b",
        seeds=["https://bobistheoilguy.com/forums/"],
        thread_re=r"/threads/[^\"#?]+\.\d+/?$",
        listing_re=r"/forums/[^\"#?]+\.\d+/?$",
        next_page="{base}page-{n}",
    ),

    # --- ЗОНА C: прочие языки / API-сайты ------------------------------------
    "opinautos.com": ForumSite(
        host="www.opinautos.com", engine="custom", lang="es", zone="c",
        seeds=["https://www.opinautos.com/"],
        thread_re=r"https://www\.opinautos\.com/[^/\"]+/[^/\"]+/pregunta[^\"#?]*",
        mode="seed",
    ),
    # motor-talk.de -> отдельный GraphQL-клиент (не HTML-краул), см. sources-scan.md
    # reddit -> OAuth API-клиент; autohome.com.cn -> позже
}


def sites_in_zone(zone: str) -> list[ForumSite]:
    return [s for s in SITES.values() if s.zone == zone]
