"""Форумы: тред -> посты -> та же дистилляция -> RepairCase(type=forum).

Схема работы:
1. Ты открываешь форум в браузере (логин, капча — руками один раз), сохраняешь
   HAR (DevTools -> Network -> Export HAR). Из HAR берём cookies + User-Agent.
2. Парсер тянет тред этой сессией, вежливо (пауза FORUM_SLEEP_SECONDS,
   robots.txt уважаем, честный UA поверх сессионного не подменяем).
3. Движки форумов узнаваемы: XenForo / phpBB / vBulletin / IPB покрывают
   большинство автофорумов — селекторы ниже. Для экзотики пишется свой
   экстрактор (наследник ForumParser, ~20 строк).

CLI:
    python -m pipeline.forums --thread URL [--har session.har] [--no-distill]
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import config
from .case_schema import Source
from .store import append_jsonl, archive_blob

FORUM_SLEEP_SECONDS = 4.0
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 AutoMechBot/0.1")


# --- сессия из HAR --------------------------------------------------------------

def session_from_har(har_path: str | Path | None, target_url: str) -> requests.Session:
    """Собрать requests.Session с куками и UA из HAR-файла (домен цели)."""
    s = requests.Session()
    s.headers["User-Agent"] = DEFAULT_UA
    if not har_path:
        return s
    har = json.loads(Path(har_path).read_text(encoding="utf-8", errors="replace"))
    host = urlparse(target_url).hostname or ""
    for entry in har.get("log", {}).get("entries", []):
        req = entry.get("request", {})
        if host not in (urlparse(req.get("url", "")).hostname or ""):
            continue
        for c in req.get("cookies", []):
            if c.get("name"):
                s.cookies.set(c["name"], c.get("value", ""), domain=host)
        for h in req.get("headers", []):
            if h.get("name", "").lower() == "user-agent" and h.get("value"):
                s.headers["User-Agent"] = h["value"]
    return s


# --- извлечение постов -----------------------------------------------------------

# движок -> (селектор поста, селектор автора) для BeautifulSoup.select
ENGINE_SELECTORS: dict[str, tuple[str, str]] = {
    "xenforo": ("article.message .bbWrapper", "article.message .message-name"),
    "phpbb": ("div.post div.content", "div.post .username, div.post .username-coloured"),
    "vbulletin": ("div.postcontent, blockquote.postcontent, ol.posts li.postbit .content",
                  "a.bigusername, .username_container .username, .username"),
    "ipb": ("div[data-role='commentContent'], article .cPost_contentWrap [data-role='commentContent']",
            "aside .cAuthorPane_author, .ipsComment_author .cAuthorPane_author, h3.ipsType_sectionHead a"),
}


def detect_engine(html: str) -> str | None:
    # сканируем ВЕСЬ документ: на крупных форумах движковые маркеры уходят
    # далеко за <head> (у vwvortex data-xf появляется после ~60KB меню)
    low = html.lower()
    # сильные, специфичные маркеры важнее общих слов (bbWrapper/postbit могут
    # встретиться как чужие классы) — поэтому проверяем по «якорям» движка
    if ("data-role=\"commentcontent\"" in low or "ipscommunity" in low
            or "ips.setting" in low or "invision community" in low
            or "ips community suite" in low):
        return "ipb"
    if ("data-xf-" in low or "js-xf-" in low or "message-usercontent" in low
            or "xenforo" in low):
        return "xenforo"
    if "phpbb" in low or 'id="phpbb"' in low:
        return "phpbb"
    if "vbulletin" in low or "vbseo" in low:
        return "vbulletin"
    return None


# Сайты со своим движком (не форумным) — отдельные CSS-экстракторы.
# selector -> текст «поста»; первый = основная запись, остальные = комментарии.
CUSTOM_SELECTORS: dict[str, tuple[str, str]] = {
    # drive2.ru — бортжурналы: главная запись в articleBody, каменты отдельно
    "drive2.ru": ("[itemprop='articleBody'], div.c-post__body",
                  "div.c-comment__text, div.js-comment-text"),
    # opinautos.com — Q&A: вопрос + ответы
    "opinautos.com": ("div.PostText, div.QuestionText, div.AnswerText", ""),
}


def extract_custom(html: str, host: str) -> list[dict]:
    """Экстрактор для сайтов со своим движком (drive2, opinautos...)."""
    from bs4 import BeautifulSoup
    key = next((k for k in CUSTOM_SELECTORS if k in host), None)
    if not key:
        return []
    soup = BeautifulSoup(html, "html.parser")
    body_sel, comment_sel = CUSTOM_SELECTORS[key]
    posts = []
    for el in soup.select(body_sel):
        t = el.get_text(" ", strip=True)
        if len(t) > 40:
            posts.append({"author": "", "text": t})
    if comment_sel:
        for el in soup.select(comment_sel):
            t = el.get_text(" ", strip=True)
            if len(t) > 30:
                posts.append({"author": "", "text": t})
    return posts


def extract_posts(html: str, host: str = "") -> list[dict]:
    """[{author, text}] по движку/кастомному экстрактору; фолбэк — крупные блоки."""
    from bs4 import BeautifulSoup
    custom = extract_custom(html, host)
    if custom:
        return custom
    soup = BeautifulSoup(html, "html.parser")
    engine = detect_engine(html)
    if engine:
        post_sel, author_sel = ENGINE_SELECTORS[engine]
        posts = [p.get_text(" ", strip=True) for p in soup.select(post_sel)]
        authors = [a.get_text(" ", strip=True) for a in soup.select(author_sel)]
        if posts:
            return [{"author": (authors[i] if i < len(authors) else ""),
                     "text": t} for i, t in enumerate(posts) if len(t) > 20]
    # фолбэк: эвристика — большие текстовые блоки
    blocks = [b.get_text(" ", strip=True) for b in soup.find_all(["p", "div"])
              if 80 < len(b.get_text(strip=True)) < 8000]
    seen, out = set(), []
    for b in blocks:
        key = b[:120]
        if key not in seen:
            seen.add(key)
            out.append({"author": "", "text": b})
    return out[:60]


def fetch_thread(url: str, har: str | None = None,
                 max_pages: int = 5) -> tuple[str, list[dict]]:
    """Тред (с пагинацией page-2..N по типовым URL-схемам) -> (title, posts)."""
    s = session_from_har(har, url)
    host = urlparse(url).hostname or ""
    posts: list[dict] = []
    title = ""
    for page in range(1, max_pages + 1):
        page_url = url if page == 1 else _page_url(url, page)
        r = s.get(page_url, timeout=30)
        if r.status_code != 200:
            break
        html = r.text
        if page == 1:
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
            title = (m.group(1).strip() if m else url)[:200]
        got = extract_posts(html, host)
        if not got or (posts and got[0]["text"] == posts[0]["text"]):
            break  # пагинация кончилась / зациклилась
        posts.extend(got)
        time.sleep(FORUM_SLEEP_SECONDS)
    return title, posts


def _page_url(url: str, page: int) -> str:
    """Пагинация в стиле XenForo (…/page-N) — самая частая на автофорумах.
    Для phpBB (&start=15) и vBulletin (/pageN) — переопределить под форум."""
    return f"{url.rstrip('/')}/page-{page}"


def posts_to_transcript(posts: list[dict]) -> str:
    lines = []
    for i, p in enumerate(posts, 1):
        author = p["author"] or f"user{i}"
        lines.append(f"[post {i} | {author}] {p['text']}")
    text = "\n".join(lines)
    return text[:config.DISTILL_MAX_INPUT_CHARS]


# --- CLI ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--thread", required=True, help="URL треда")
    ap.add_argument("--har", help="HAR-файл с авторизованной сессией")
    ap.add_argument("--max-pages", type=int, default=5)
    ap.add_argument("--no-distill", action="store_true",
                    help="только извлечь посты (проверка парсера)")
    args = ap.parse_args()

    title, posts = fetch_thread(args.thread, args.har, args.max_pages)
    print(f"тред: {title}\nпостов: {len(posts)}")
    for p in posts[:3]:
        print(f"  [{p['author']}] {p['text'][:120]}")
    if not posts:
        raise SystemExit("посты не извлечены — нужен свой экстрактор под этот движок")

    if args.no_distill:
        return

    from .distill import distill
    source = Source(type="forum", url=args.thread, title=title,
                    video_id=f"forum-{abs(hash(args.thread)) % 10**10}")
    transcript = posts_to_transcript(posts)
    case = distill(transcript, source)
    append_jsonl(case)
    archive_blob(f"cases/{source.video_id}.json", case.model_dump_json())
    print(f"\nкейс: {case.system} | {case.problem_summary}")
    print(f"причина: {case.root_cause}")
    print(f"нюансов: {len(case.pitfalls)}, off_topic={case.off_topic}")

    # в «базу данных» ADO: Epic форума ([forum:host], с шардингом) -> child-тред
    if config.ADO_ORG and config.ADO_PROJECT and config.ADO_PAT:
        from .ado import AdoClient
        ado = AdoClient()
        host = urlparse(args.thread).hostname or "forum"
        channel = {"id": host, "name": host, "url": f"https://{host}"}
        wi = ado.attach_video(channel, "forum",
                              {"id": source.video_id, "title": title,
                               "url": args.thread, "channel": host,
                               "channel_id": host})
        if wi:
            state = "distilled" if not case.off_topic else "offtopic"
            ado.set_state(wi, state, comment=f"case: {case.problem_summary[:120]}")
            print(f"work item треда: #{wi} (форум {host})")


if __name__ == "__main__":
    main()
