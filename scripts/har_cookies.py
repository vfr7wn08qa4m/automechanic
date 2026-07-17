#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Извлечь YouTube-сессию из HAR -> cookies (base64) + visitorData, вписать в
пульт (shared_secrets.YTDLP_COOKIES_B64 / YTDLP_VISITOR_DATA). Затем
`deploy.py --context` разольёт их в CI, и облачный yt-dlp пройдёт блок DC-IP.

    python scripts/har_cookies.py --har dump/youtube.har --accounts accounts.json
"""
from __future__ import annotations

import argparse
import base64
import json
import time
from pathlib import Path

YT_HOSTS = ("youtube.com", ".google.com", "google.com")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--har", required=True)
    ap.add_argument("--accounts", default="accounts.json")
    args = ap.parse_args()
    root = Path(__file__).resolve().parent.parent

    d = json.loads(Path(args.har).read_text(encoding="utf-8", errors="ignore"))
    cookies: dict[str, str] = {}
    visitor = ""
    for e in d["log"]["entries"]:
        url = e["request"]["url"]
        if not any(h in url for h in YT_HOSTS):
            continue
        for c in e["request"].get("cookies", []):
            if c.get("name"):
                cookies[c["name"]] = c.get("value", "")
        for h in e["request"].get("headers", []):
            n = h["name"].lower()
            if n == "x-goog-visitor-id" and not visitor:
                visitor = h["value"]
            elif n == "cookie":
                for pair in h["value"].split("; "):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        cookies.setdefault(k.strip(), v)

    if not cookies:
        raise SystemExit("в HAR не нашлось YouTube-cookies — проверь, что это HAR "
                         "с youtube.com (открой видео, дай титрам загрузиться, сохрани HAR)")

    exp = int(time.time()) + 3600 * 24 * 180
    lines = ["# Netscape HTTP Cookie File"]
    for name, val in cookies.items():
        lines.append(f".youtube.com\tTRUE\t/\tTRUE\t{exp}\t{name}\t{val}")
    txt = "\n".join(lines) + "\n"
    b64 = base64.b64encode(txt.encode("utf-8")).decode()

    cfg = json.loads((root / args.accounts).read_text(encoding="utf-8"))
    ss = cfg.setdefault("shared_secrets", {})
    ss["YTDLP_COOKIES_B64"] = b64
    if visitor:
        ss["YTDLP_VISITOR_DATA"] = visitor
    (root / args.accounts).write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    key_names = ", ".join(sorted(cookies)[:8])
    print(f"cookies извлечено: {len(cookies)} ({key_names}...)")
    print(f"visitorData: {'да' if visitor else 'нет (не критично)'}")
    print(f"вписано в {args.accounts}: shared_secrets.YTDLP_COOKIES_B64"
          + (" + YTDLP_VISITOR_DATA" if visitor else ""))
    print("дальше: python deploy.py --context  (разлить в CI), затем тест облачного subs")


if __name__ == "__main__":
    main()
