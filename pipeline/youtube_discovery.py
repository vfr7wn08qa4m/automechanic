"""Discovery: поиск каналов автоэлектриков и новых видео, заведение work items.

«База данных» живёт в ADO: канал = Epic ([ch:UCxxx], kind:channel,
state:active|paused), видео = child-Issue под ним. Discovery создаёт эпики,
sync читает АКТИВНЫЕ эпики из ADO (это и есть реестр) и вешает под них новые
видео. Курирование: мусорный канал -> тег state:paused, синк его пропускает.

Два режима доступа к YouTube:
- с YOUTUBE_API_KEY — официальный Data API v3 (квота 10k units/день бесплатно;
  search=100 units, playlistItems=1 unit/страница — дельту каналов гонять дёшево);
- без ключа — yt-dlp (ytsearch / вкладка /videos канала). Медленнее, но 0 квоты.
  С датацентровых IP не работает — только чистый IP.

Без настроенного ADO реестр — файл data/channels.json (+зеркало в R2): dev-режим.
CLI:
    python -m pipeline.youtube_discovery --discover "buscar fallas electricas auto" --create-workitems
    python -m pipeline.youtube_discovery --sync-all --create-workitems --max-videos 30
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone

import requests

from . import config
from .subtitles import _run_ytdlp

CHANNELS_FILE = config.DATA_DIR / "channels.json"

# Затравочные запросы: ~24 языка. Search в Data API стоит 100 units ->
# полный прогон ~2.4k units из дневных 10k. Расширяй свободно.
SEED_QUERIES = [
    "автоэлектрик диагностика",            # ru
    "автоелектрик діагностика авто",       # uk
    "car electrical fault finding",        # en
    "auto electrician diagnostics",        # en
    "kfz elektrik fehlersuche",            # de
    "diagnostico electrico automotriz",    # es
    "diagnóstico elétrico automotivo",     # pt-BR
    "diagnostic électrique automobile",    # fr
    "diagnostica elettrica auto",          # it
    "elektryka samochodowa diagnoza",      # pl
    "oto elektrik arıza tespiti",          # tr
    "تشخيص كهرباء السيارات",               # ar
    "कार इलेक्ट्रिकल फॉल्ट डायग्नोसिस",      # hi
    "diagnosa kelistrikan mobil",          # id
    "chẩn đoán điện ô tô",                 # vi
    "รถยนต์ ไฟฟ้า วินิจฉัย ซ่อม",             # th
    "자동차 전기 고장 진단",                 # ko
    "汽车电路故障诊断",                      # zh
    "自動車 電装 故障診断",                  # ja
    "diagnosticare electrica auto",        # ro
    "autoelektrikář diagnostika",          # cs
    "autó elektromos hibakeresés",         # hu
    "διάγνωση ηλεκτρικών αυτοκινήτου",     # el
    "avtoelektrik diaqnostika",            # az
]


REGISTRY_S3_KEY = "registry/channels.json"


def load_channels() -> dict:
    """Реестр каналов: S3-архив (переживает эфемерные CI-агенты) или локальный файл."""
    if config.S3_ENDPOINT:
        try:
            from .store import s3_client
            body = s3_client().get_object(
                Bucket=config.S3_BUCKET, Key=REGISTRY_S3_KEY)["Body"].read()
            return json.loads(body)
        except Exception:  # noqa: BLE001 — первый запуск: реестра ещё нет
            pass
    if CHANNELS_FILE.exists():
        return json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
    return {}


def save_channels(channels: dict) -> None:
    payload = json.dumps(channels, ensure_ascii=False, indent=2)
    CHANNELS_FILE.write_text(payload, encoding="utf-8")
    if config.S3_ENDPOINT:
        from .store import archive_blob
        archive_blob(REGISTRY_S3_KEY, payload)


# --- режим yt-dlp (без API-ключа) --------------------------------------------

def discover_channels_ytdlp(query: str, limit: int = 10) -> list[dict]:
    p = _run_ytdlp([f"ytsearch{limit}:{query}", "--flat-playlist",
                    "--print", "%(channel_id)s\t%(channel)s\t%(channel_url)s"])
    found: dict[str, dict] = {}
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] and parts[0] != "NA":
            found[parts[0]] = {"name": parts[1], "url": parts[2], "last_sync": None}
    return [{"id": cid, **info} for cid, info in found.items()]


def channel_videos_ytdlp(channel_url: str, max_videos: int = 30) -> list[dict]:
    p = _run_ytdlp([f"{channel_url.rstrip('/')}/videos", "--flat-playlist",
                    "--playlist-end", str(max_videos),
                    "--print", "%(id)s\t%(title)s\t%(duration)s"], timeout=300)
    videos = []
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0]:
            videos.append({"id": parts[0], "title": parts[1],
                           "duration": parts[2],
                           "url": f"https://www.youtube.com/watch?v={parts[0]}"})
    return videos


# --- режим YouTube Data API v3 ------------------------------------------------

YT_API = "https://www.googleapis.com/youtube/v3"


def discover_channels_api(query: str, limit: int = 10) -> list[dict]:
    r = requests.get(f"{YT_API}/search", params={
        "key": config.YOUTUBE_API_KEY, "part": "snippet", "type": "channel",
        "q": query, "maxResults": limit}, timeout=30)
    r.raise_for_status()
    return [{"id": it["snippet"]["channelId"], "name": it["snippet"]["title"],
             "url": f"https://www.youtube.com/channel/{it['snippet']['channelId']}",
             "last_sync": None} for it in r.json().get("items", [])]


def video_comments_api(video_id: str, max_comments: int = 60) -> list[dict]:
    """Топ-комментарии видео через Data API (commentThreads, 1 unit/страница).
    Часто золото: «у меня было то же, оказалось X». Пусто без YOUTUBE_API_KEY."""
    if not config.YOUTUBE_API_KEY:
        return []
    out: list[dict] = []
    page = None
    while len(out) < max_comments:
        r = requests.get(f"{YT_API}/commentThreads", params={
            "key": config.YOUTUBE_API_KEY, "part": "snippet", "videoId": video_id,
            "maxResults": min(100, max_comments - len(out)), "order": "relevance",
            "textFormat": "plainText", **({"pageToken": page} if page else {})},
            timeout=30)
        if r.status_code != 200:       # комменты выключены / квота — не критично
            break
        body = r.json()
        for it in body.get("items", []):
            s = it["snippet"]["topLevelComment"]["snippet"]
            txt = (s.get("textDisplay") or "").strip()
            if len(txt) > 15:          # мусорные «спасибо» отсекаем
                out.append({"author": s.get("authorDisplayName", ""),
                            "text": txt, "likes": int(s.get("likeCount", 0) or 0)})
        page = body.get("nextPageToken")
        if not page:
            break
    out.sort(key=lambda c: c["likes"], reverse=True)   # по лайкам — сверху ценное
    return out


def channel_videos_api(channel_id: str, max_videos: int = 30) -> list[dict]:
    # uploads-плейлист = 'UU' + канал без 'UC' — 1 unit вместо search за 100
    uploads = "UU" + channel_id[2:]
    videos, page = [], None
    while len(videos) < max_videos:
        r = requests.get(f"{YT_API}/playlistItems", params={
            "key": config.YOUTUBE_API_KEY, "part": "snippet,contentDetails",
            "playlistId": uploads, "maxResults": min(50, max_videos - len(videos)),
            **({"pageToken": page} if page else {})}, timeout=30)
        if r.status_code == 404:
            break
        r.raise_for_status()
        body = r.json()
        for it in body.get("items", []):
            vid = it["contentDetails"]["videoId"]
            videos.append({"id": vid, "title": it["snippet"]["title"],
                           "published_at": it["contentDetails"].get("videoPublishedAt", ""),
                           "url": f"https://www.youtube.com/watch?v={vid}"})
        page = body.get("nextPageToken")
        if not page:
            break
    return videos


# --- оркестрация ---------------------------------------------------------------

def _ado_or_none():
    if config.ADO_ORG and config.ADO_PROJECT and config.ADO_PAT:
        from .ado import AdoClient
        return AdoClient()
    return None


MY_CHANNELS_FILE = ROOT_MY = __import__("pathlib").Path(__file__).resolve().parent.parent / "my_channels.txt"


def load_my_channels() -> list[str]:
    """Свои/приоритетные каналы из my_channels.txt (URL / @handle / UC...)."""
    if not MY_CHANNELS_FILE.exists():
        return []
    out = []
    for line in MY_CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        code = line.split("#", 1)[0].strip()   # срезаем инлайн-коммент: "UCxxx # @handle"
        if code:
            out.append(code)
    return out


def resolve_channel(ref: str) -> dict | None:
    """Привести URL/@handle/UC-id к {id, name, url}.

    UC-id и /channel/UC... ссылки резолвим БЕЗ сети (id уже известен). @handle и
    кастомные URL — через yt-dlp (0 квоты), с ретраем на случай 429."""
    ref = ref.strip()
    # прямой channel_id известен -> сеть не нужна
    if ref.startswith("UC") and "/" not in ref and len(ref) >= 20:
        return {"id": ref, "name": ref,
                "url": f"https://www.youtube.com/channel/{ref}"}
    if "/channel/UC" in ref:
        cid = ref.split("/channel/", 1)[1].split("/")[0].split("?")[0]
        return {"id": cid, "name": cid,
                "url": f"https://www.youtube.com/channel/{cid}"}
    # @handle / кастомный URL -> yt-dlp
    if ref.startswith("http"):
        url = ref
    else:
        url = f"https://www.youtube.com/{ref if ref.startswith('@') else '@' + ref}"
    for attempt in range(3):
        p = _run_ytdlp([f"{url.rstrip('/')}/videos", "--flat-playlist",
                        "--playlist-end", "1",
                        "--print", "%(channel_id)s\t%(channel)s\t%(channel_url)s"])
        for line in p.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].startswith("UC"):
                return {"id": parts[0], "name": parts[1], "url": parts[2]}
        time.sleep(config.YTDLP_SLEEP_SECONDS * (attempt + 1))  # 429-бэкофф
    print(f"  resolve_channel: не удалось разрешить {ref}")
    return None


def sync_channel(channel_id: str, info: dict, create_workitems: bool,
                 max_videos: int, ado=None, kind: str = "channel") -> int:
    if config.YOUTUBE_API_KEY:
        videos = channel_videos_api(channel_id, max_videos)
    else:
        url = info.get("url") or f"https://www.youtube.com/channel/{channel_id}"
        videos = channel_videos_ytdlp(url, max_videos)
        time.sleep(config.YTDLP_SLEEP_SECONDS)

    created = 0
    if create_workitems and videos and ado is not None:
        channel = {"id": channel_id, "name": info.get("name", ""),
                   "url": info.get("url", "")}
        known = ado.channel_all_child_video_ids(channel_id, kind)  # дедуп по каналу
        for v in videos:
            v.setdefault("channel", info.get("name", ""))
            v.setdefault("channel_id", channel_id)
            # attach_video сам выберет текущий чанк и дольёт следующий при >900
            if ado.attach_video(channel, kind, v, known=known):
                created += 1
    print(f"  {info.get('name', channel_id)}: {len(videos)} видео, новых WI: {created}")
    return created


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--discover", metavar="QUERY", help="найти каналы по запросу")
    ap.add_argument("--discover-seeds", action="store_true",
                    help="прогнать все затравочные запросы SEED_QUERIES")
    ap.add_argument("--sync-all", action="store_true",
                    help="дельта по всем каналам из реестра")
    ap.add_argument("--my-channels", action="store_true",
                    help="завести Epic и очередь по каналам из my_channels.txt")
    ap.add_argument("--create-workitems", action="store_true",
                    help="создавать work items в ADO (иначе dry-run)")
    ap.add_argument("--max-videos", type=int, default=30)
    args = ap.parse_args()

    if not config.YOUTUBE_API_KEY and os.getenv("CI"):
        print("ВНИМАНИЕ: на CI-агенте без YOUTUBE_API_KEY discovery через yt-dlp "
              "почти наверняка упрётся в 429 (датацентровый IP). Задай ключ "
              "Data API v3 — он официальный и с DC IP работает.")

    ado = _ado_or_none()
    channels = load_channels()  # файловый реестр — кэш/dev-режим

    queries = [args.discover] if args.discover else (SEED_QUERIES if args.discover_seeds else [])
    for q in queries:
        found = (discover_channels_api(q) if config.YOUTUBE_API_KEY
                 else discover_channels_ytdlp(q))
        fresh = [c for c in found if c["id"] not in channels]
        epics = 0
        for c in found:
            cid = c["id"]
            if args.create_workitems and ado is not None:
                if ado.create_channel_item({"id": cid, "name": c["name"],
                                            "url": c["url"]}):
                    epics += 1
            if cid in channels:
                continue
            channels[cid] = {k: v for k, v in c.items() if k != "id"}
        print(f"'{q}': найдено {len(found)}, новых {len(fresh)}, новых эпиков {epics}")
        time.sleep(config.YTDLP_SLEEP_SECONDS)

    if args.my_channels:
        refs = load_my_channels()
        print(f"my_channels.txt: {len(refs)} каналов")
        for ref in refs:
            ch = resolve_channel(ref)
            if not ch:
                print(f"  не разрешён: {ref}")
                continue
            channels[ch["id"]] = {"name": ch["name"], "url": ch["url"], "mine": True}
            sync_channel(ch["id"], ch, args.create_workitems, args.max_videos,
                         ado=ado)  # attach_video заведёт Epic-чанк сам
            time.sleep(config.YTDLP_SLEEP_SECONDS)

    if args.sync_all:
        total = sync_active_channels(ado, channels, args.create_workitems,
                                     args.max_videos)
        print(f"Итого новых work items: {total}")

    save_channels(channels)


def sync_active_channels(ado, channels: dict | None, create_workitems: bool,
                         max_videos: int) -> int:
    """Дельта по активным каналам: новые видео -> New Task'и. Ядро delta-пайплайна.
    Источник истины — активные Epic-каналы ADO (state:paused пропускаются)."""
    channels = channels if channels is not None else {}
    total = 0
    if ado is not None:
        for ch in ado.list_channel_items(kind="channel", active_only=True):
            cid = ch["channel_id"]
            info = channels.get(cid, {"name": ch["name"]})
            total += sync_channel(cid, info, create_workitems, max_videos, ado=ado)
            channels.setdefault(cid, {}).update(
                name=ch["name"], last_sync=datetime.now(timezone.utc).isoformat())
    else:
        for cid, info in channels.items():
            total += sync_channel(cid, info, create_workitems, max_videos)
            info["last_sync"] = datetime.now(timezone.utc).isoformat()
    return total


def ensure_my_channels(ado, channels: dict, create_workitems: bool,
                       max_videos: int) -> int:
    """Зарегистрировать каналы из my_channels.txt как Epic и синкнуть их видео.
    Идемпотентно: повторный вызов не плодит дубли (attach_video дедупит)."""
    refs = load_my_channels()
    total = 0
    for ref in refs:
        ch = resolve_channel(ref)
        if not ch:
            print(f"  my_channels: не разрешён {ref}")
            continue
        channels[ch["id"]] = {"name": ch["name"], "url": ch["url"], "mine": True}
        total += sync_channel(ch["id"], ch, create_workitems, max_videos, ado=ado)
        time.sleep(config.YTDLP_SLEEP_SECONDS)
    return total


def discover_new_channels(ado, channels: dict, create_workitems: bool,
                          queries: list[str] | None = None) -> int:
    """Поиск НОВЫХ каналов по затравочным запросам (~24 языка) -> Epic на канал."""
    queries = queries if queries is not None else SEED_QUERIES
    new_epics = 0
    for q in queries:
        try:
            found = (discover_channels_api(q) if config.YOUTUBE_API_KEY
                     else discover_channels_ytdlp(q))
        except Exception as e:  # noqa: BLE001 — один запрос не валит остальные
            print(f"  discover '{q[:30]}': {str(e)[:80]}")
            continue
        for c in found:
            cid = c["id"]
            if create_workitems and ado is not None:
                if ado.create_channel_item({"id": cid, "name": c["name"],
                                            "url": c["url"]}):
                    new_epics += 1
            channels.setdefault(cid, {k: v for k, v in c.items() if k != "id"})
        time.sleep(config.YTDLP_SLEEP_SECONDS)
    return new_epics


if __name__ == "__main__":
    main()
