"""CarCareKiosk.com — ~1500 машин × ~80 коротких видео-инструкций (EN,
плюс зеркала es./fr.). Титров нет — транскрипт через ASR (pipeline/asr.py).

Структура сайта (выяснена по HAR из dump/ + sitemap, июль 2026):
    sitemap.xml                     -> /videos/{Make}/{Model}/{Year} (1517 шт)
    /videos/{Make}/{Model}/{Year}   -> абсолютные ссылки на ~80 задач
    /video/{car_slug}/{sys}/{task}  -> <video src="…cloudfront….mp4">
                                       + meta description с краткими шагами
robots.txt: /video/ и /videos/ не запрещены; ходим вежливо (пауза 3с).

Иерархия в ADO: Epic на марку [ch:cck-{Make}] (kind:site) -> child-задачи
[vid:cck-{sha1-12}]. В description задачи — page_url и mp4_url.

CLI:
    python -m pipeline.carcarekiosk --probe "https://www.carcarekiosk.com/video/..."
    python -m pipeline.carcarekiosk --discover --max-cars 5 [--create-workitems]
"""
from __future__ import annotations

import argparse
import hashlib
import re
import time

import requests

from . import config
from .case_schema import Source

BASE = "https://www.carcarekiosk.com"
UA = {"User-Agent": "Mozilla/5.0 AutoMechBot/0.1 (+research)"}
SLEEP = 3.0

_CAR_RE = re.compile(r"https://www\.carcarekiosk\.com/videos/([^/]+)/([^/]+)/(\d{4})$")
_TASK_RE = re.compile(r'href="(https://www\.carcarekiosk\.com/video/([^/"]+)/([^/"]+)/([^/"]+))"')
_MP4_RE = re.compile(r'<(?:video|source)[^>]*src="(https://[^"]+\.mp4)"')
_DESC_RE = re.compile(r'<meta name="description" content="([^"]*)"')
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL)


def video_uid(page_url: str) -> str:
    return "cck-" + hashlib.sha1(page_url.encode()).hexdigest()[:12]


def load_vehicle_urls() -> list[str]:
    """Все страницы машин (EN) из sitemap."""
    xml = requests.get(f"{BASE}/sitemap.xml", headers=UA, timeout=60).text
    return [u for u in re.findall(r"<loc>([^<]+)</loc>", xml) if _CAR_RE.match(u)]


def vehicle_tasks(car_url: str) -> list[dict]:
    """Задачи одной машины: [{page_url, car_slug, system, task}]."""
    html = requests.get(car_url, headers=UA, timeout=30).text
    out, seen = [], set()
    for m in _TASK_RE.finditer(html):
        page_url, car_slug, system, task = m.groups()
        if page_url in seen:
            continue
        seen.add(page_url)
        out.append({"page_url": page_url, "car_slug": car_slug,
                    "system": system, "task": task})
    return out


def video_info(page_url: str) -> dict:
    """Страница задачи: mp4, заголовок, краткие шаги из meta description."""
    html = requests.get(page_url, headers=UA, timeout=30).text
    mp4 = _MP4_RE.search(html)
    title = _TITLE_RE.search(html)
    desc = _DESC_RE.search(html)
    return {
        "page_url": page_url,
        "mp4_url": mp4.group(1) if mp4 else "",
        "title": (title.group(1).strip() if title else page_url),
        "steps_text": (desc.group(1).strip() if desc else ""),
    }


def transcript_for(page_url: str) -> tuple[str, list[tuple[int, str]]]:
    """(lang, lines) для задачи: ASR по mp4 + шаги из description первым блоком."""
    info = video_info(page_url)
    if not info["mp4_url"]:
        raise RuntimeError(f"mp4 не найден на {page_url}")
    from .asr import transcribe_url
    lines = transcribe_url(info["mp4_url"])
    if info["steps_text"]:
        lines = [(0, f"(official summary) {info['steps_text']}")] + lines
    return "en", lines


def source_for(page_url: str, title: str = "") -> Source:
    return Source(type="carcarekiosk", url=page_url, video_id=video_uid(page_url),
                  title=title or page_url.rsplit("/", 2)[-2:][0], lang="en")


# --- discovery в ADO -------------------------------------------------------------

def discover(create_workitems: bool, max_cars: int, max_tasks: int) -> None:
    from .ado import AdoClient
    ado = AdoClient() if create_workitems else None
    cars = load_vehicle_urls()
    print(f"машин в sitemap: {len(cars)}; обрабатываю первые {max_cars}")

    for car_url in cars[:max_cars]:
        make, model, year = _CAR_RE.match(car_url).groups()
        tasks = vehicle_tasks(car_url)
        print(f"  {make} {model} {year}: задач {len(tasks)}")
        created = 0
        if ado is not None and tasks:
            channel = {"id": f"cck-{make}", "name": f"CarCareKiosk: {make}",
                       "url": f"{BASE}/videos/{make}"}
            # марка CCK легко >1000 задач по всем моделям -> attach_video шардит
            known = ado.channel_all_child_video_ids(f"cck-{make}", "site")
            for t in tasks[:max_tasks]:
                if ado.attach_video(channel, "site", {
                        "id": video_uid(t["page_url"]),
                        "title": f"{make} {model} {year}: {t['system']}/{t['task']}",
                        "url": t["page_url"],
                        "channel": f"CarCareKiosk {make}",
                        "channel_id": f"cck-{make}"}, known=known):
                    created += 1
            print(f"    новых WI: {created}")
        time.sleep(SLEEP)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe", metavar="PAGE_URL",
                    help="показать инфо по одной задаче (mp4, шаги)")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--create-workitems", action="store_true")
    ap.add_argument("--max-cars", type=int, default=5)
    ap.add_argument("--max-tasks", type=int, default=100)
    args = ap.parse_args()

    if args.probe:
        info = video_info(args.probe)
        for k, v in info.items():
            print(f"{k}: {str(v)[:160]}")
    if args.discover:
        discover(args.create_workitems, args.max_cars, args.max_tasks)


if __name__ == "__main__":
    main()
