"""Локальный ДОБОР транскриптов: видео, которые облако не осилило (state:failed),
дотягиваем с ДОМАШНЕГО IP через yt-dlp — дома YouTube не режет, как датацентровый
CircleCI. «Не работает в облаке — добиваем локально, до последней капли».

Успех -> транскрипт (+топ-комменты) в тело тикета, state -> subs (Claude
продистиллирует наравне с остальными). Настоящий отказ (у видео нет титров) ->
тег `no-captions`, чтобы следующие прогоны его пропускали и не долбили впустую.

    python scripts/local_fetch_subs.py --batch 50            # добить failed
    python scripts/local_fetch_subs.py --state new --batch 50 # можно и свежие
"""
from __future__ import annotations

import argparse
import html as _html
import json
import os
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_az = json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))["azure"]
os.environ.update(ADO_ORG=_az["org"], ADO_PROJECT=_az["project"], ADO_PAT=_az["pat"])
os.environ["SUBTITLE_PROVIDERS"] = "ytdlp"       # дома yt-dlp работает напрямую

from pipeline import config                        # noqa: E402
from pipeline.ado import AdoClient                  # noqa: E402
from pipeline.subtitle_providers import transcript_for_item  # noqa: E402
from pipeline.subtitles import to_prompt_text       # noqa: E402


def _mark_nocaptions(ado: AdoClient, wi_id: int, err: str) -> None:
    """Оставляем в failed, но метим no-captions — реально нет титров, не гонять снова."""
    wi = ado.get(wi_id)
    tags = [t.strip() for t in (wi["fields"].get("System.Tags") or "").split(";") if t.strip()]
    if "no-captions" not in tags:
        tags.append("no-captions")
    ado._patch(wi_id, [
        {"op": "add", "path": "/fields/System.Tags", "value": "; ".join(tags)},
        {"op": "add", "path": "/fields/System.History", "value": f"local: нет титров ({err[:150]})"},
    ])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--state", default="failed", choices=["failed", "new"])
    args = ap.parse_args()

    ado = AdoClient()
    # берём широко: failed мешает видео с форумами/no-captions -> нужен весь пул,
    # чтобы отобрать именно видео; для new достаточно запаса на отсев.
    cap = 5000 if args.state == "failed" else args.batch * 4
    raw = ado.query_by_state(args.state, top=cap)
    todo: list[tuple[int, str]] = []
    for wi in ado.get_batch(raw, fields=("System.Title", "System.Tags")):
        vid = ado.video_id_from_title(wi["fields"].get("System.Title", "")) or ""
        tags = wi["fields"].get("System.Tags", "") or ""
        if vid and not vid.startswith("frm-") and "no-captions" not in tags:
            todo.append((wi["id"], vid))
        if len(todo) >= args.batch:
            break
    print(f"к добору (state={args.state}): {len(todo)} видео")

    ok = nocap = err = 0
    for wi_id, vid in todo:
        wi = ado.get(wi_id)
        url = AdoClient.source_url(wi) or f"https://www.youtube.com/watch?v={vid}"
        try:
            tr = transcript_for_item(url, vid)
            text = to_prompt_text(tr.lines)
            ado.append_description(
                wi_id,
                f"<hr><b>Transcript</b> ({_html.escape(tr.provider)}, "
                f"{_html.escape(tr.lang or '')}, {len(tr.lines)} строк, local)"
                f"<pre>{_html.escape(text[:150000])}</pre>")
            # топ-комменты YouTube — тоже золото («у меня было то же…»)
            try:
                from pipeline.youtube_discovery import video_comments_api
                comments = video_comments_api(vid, 60)
                if comments:
                    chtml = "".join(
                        f"<p><small>{_html.escape(c['author'])} (+{c['likes']})</small>: "
                        f"{_html.escape(c['text'])}</p>" for c in comments)
                    ado.append_description(
                        wi_id, f"<hr><b>Top comments</b> ({len(comments)})<div>{chtml}</div>")
            except Exception:  # noqa: BLE001 — комменты не критичны
                pass
            ado.set_state(wi_id, "subs",
                          comment=f"local transcript ok: {tr.provider}, {tr.lang}, {len(tr.lines)} строк")
            ok += 1
            print(f"  #{wi_id} {vid}: OK ({tr.lang}, {len(tr.lines)} строк) -> subs")
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            low = msg.lower()
            # безнадёжные (не вытянуть никогда) -> метим и пропускаем навсегда
            permanent = ("members-only", "join this channel", "private video",
                         "video unavailable", "removed by", "been terminated",
                         "no longer available", "no subtitles", "no captions",
                         "no-captions", "нет титров", "requested format")
            if any(k in low for k in permanent):
                _mark_nocaptions(ado, wi_id, msg)
                nocap += 1
                reason = ("members-only" if "member" in low or "join this" in low
                          else "недоступно" if any(k in low for k in
                          ("private", "unavailable", "removed", "terminated")) else "нет титров")
                print(f"  #{wi_id} {vid}: {reason} -> пропуск")
            else:
                err += 1                              # транзиент -> остаётся failed, повтор позже
                print(f"  #{wi_id} {vid}: ошибка (повтор) {msg[:90]}")
        time.sleep(config.YTDLP_SLEEP_SECONDS)     # пауза против 429 (даже дома)

    print(f"\nитог: восстановлено {ok} -> subs | без титров {nocap} | прочие ошибки {err}")


if __name__ == "__main__":
    main()
