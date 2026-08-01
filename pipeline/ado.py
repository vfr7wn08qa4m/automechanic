"""Azure DevOps Boards как тикетная система И «база данных» конвейера.

Иерархия (кастомный процесс AutoMechanikBoard):
    Epic = канал YouTube / форум   [ch:UCxxx] / [forum:host] (+ чанки #2,#3)
           теги: auto-mech; kind:channel|forum; state:active|paused
      └─ Task = видео / тред        [vid:xxxx], поле Custom.url = ссылка
           System.State: New -> ReadyForFilter -> ReadyForEmbeding -> Closed
                         (Removed = брак/offtopic)

Логические состояния конвейера в коде (new/subs/distilled/indexed/failed/
offtopic) маппятся на реальный System.State через STATE_MAP.

Крупные артефакты (титры, JSON кейса) в work item НЕ кладём — только ссылки
на объектное хранилище; структура и статусы — здесь, тела данных — в R2/B2.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.parse

import requests

from . import config

API = "7.1"

# логическое состояние конвейера -> реальный System.State кастомного процесса
STATE_MAP = {
    "new": "New",
    "subs": "ReadyForFilter",        # транскрипт добыт/архивирован -> фильтр Клодом
    "distilled": "ReadyForEmbeding",  # кейс извлечён -> на эмбеддинг (орфография их)
    "indexed": "Closed",
    "active": "Active",
    "failed": "Removed",
    "offtopic": "Removed",
}


def _wiql_quote(v: str) -> str:
    return v.replace("'", "''")


# Пространство [vid:] делят РАЗНЫЕ источники, и YouTube-ID узнаётся только по
# форме (11 символов base64url), а не по префиксу:
#     YouTube        aJe2bsIy0Uk      (11 симв., без префикса)
#     форум-тред     frm-<hash>
#     CarCareKiosk   cck-<sha1-12>
#     reddit-пост    1v9vfuf          (6-8 симв., БЕЗ префикса — как YouTube!)
# Отрицательная проверка «CONTAINS '[vid:' AND NOT CONTAINS 'vid:frm-'» считала
# reddit-посты видео: fix_truncated_subs видел в тексте поста 0 таймкодов,
# объявлял транскрипт обрезанным и СТИРАЛ тело тикета, после чего subs-пайплайн
# уходил тянуть титры для несуществующего видео 1v9vfuf. Проверять надо форму.
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def is_youtube_vid(vid: str | None) -> bool:
    """True только для настоящего YouTube-ID (ровно 11 символов base64url).

    WIQL не умеет регулярки, поэтому в запросе фильтр остаётся грубым
    (по префиксам), а точная отсечка — здесь, после чтения тайтла.
    """
    return bool(vid and _YT_ID_RE.match(vid))


class AdoClient:
    def __init__(self, org: str | None = None, project: str | None = None,
                 pat: str | None = None):
        self.org = org or config.ADO_ORG or ""
        # допускаем и имя организации, и полный URL (System.CollectionUri)
        if self.org.startswith("http"):
            self.org = self.org.rstrip("/").rsplit("/", 1)[-1]
        self.project = project or config.ADO_PROJECT
        pat = pat or config.ADO_PAT
        if not (self.org and self.project and pat):
            raise RuntimeError("ADO_ORG / ADO_PROJECT / ADO_PAT не заданы (.env)")
        token = base64.b64encode(f":{pat}".encode()).decode()
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Basic {token}"
        self.base = f"https://dev.azure.com/{self.org}/{self.project}/_apis"

    # --- низкоуровневое ---------------------------------------------------
    def _wiql(self, query: str, top: int | None = None) -> list[int]:
        # $top ограничивает ЧИСЛО возвращаемых строк на сервере. Без него ADO бьёт
        # VS402337 «exceeds the size limit of 20000», как только МАТЧ > 20000 —
        # на большом бэклоге (subs >20k) это роняло любой query_by_state ещё до
        # чтения метаданных. С $top отдаётся первые N по ORDER BY и лимит не бьёт.
        url = f"{self.base}/wit/wiql?api-version={API}"
        if top:
            url += f"&$top={int(top)}"
        r = self.s.post(url, json={"query": query}, timeout=30)
        r.raise_for_status()
        return [wi["id"] for wi in r.json().get("workItems", [])]

    def get(self, wi_id: int) -> dict:
        r = self.s.get(f"{self.base}/wit/workitems/{wi_id}?api-version={API}", timeout=30)
        r.raise_for_status()
        return r.json()

    def get_batch(self, ids: list[int],
                  fields: tuple = ("System.Title", "System.Tags")) -> list[dict]:
        """До 200 work items за один запрос (лимит API)."""
        out: list[dict] = []
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            r = self.s.get(
                f"{self.base}/wit/workitems?ids={','.join(map(str, chunk))}"
                f"&fields={','.join(fields)}&api-version={API}", timeout=30)
            r.raise_for_status()
            out.extend(r.json().get("value", []))
        return out

    def _patch(self, wi_id: int, ops: list[dict]) -> dict:
        r = self.s.patch(
            f"{self.base}/wit/workitems/{wi_id}?api-version={API}",
            json=ops, headers={"Content-Type": "application/json-patch+json"}, timeout=30)
        r.raise_for_status()
        return r.json()

    def _create(self, wi_type: str, ops: list[dict]) -> int:
        r = self.s.post(
            f"{self.base}/wit/workitems/${wi_type}?api-version={API}",
            json=ops, headers={"Content-Type": "application/json-patch+json"}, timeout=30)
        r.raise_for_status()
        return r.json()["id"]

    def _find_marker(self, marker: str) -> int | None:
        proj = self.project.replace("'", "''")
        mrk = marker.replace("'", "''")
        ids = self._wiql(
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{proj}' "
            f"AND [System.Title] CONTAINS '{mrk}'")
        return ids[0] if ids else None

    # --- каналы/форумы (Epic-уровень, «таблица источников») -------------------
    # Лимит ADO: 1000 связей на work item => максимум 1000 детей у Epic. Крупные
    # каналы/марки шардим цепочкой чанков: [ch:UCxxx#1] -> #2 -> #3, связанных
    # Successor'ом (Dependency-Forward: предыдущий -> следующий). Новые видео
    # вешаем в текущий незаполненный чанк. Так же обходим display-лимит 10k.
    CHUNK_CAP = 900                     # запас под 1000 связей

    @staticmethod
    def _marker(kind: str) -> str:
        return {"channel": "ch"}.get(kind, kind)

    @staticmethod
    def _parse_channel_marker(title: str, marker: str) -> tuple[str, int] | None:
        """'[ch:UCxxx#2] Name' -> ('UCxxx', 2); '[ch:UCxxx] Name' -> ('UCxxx', 1)."""
        key = f"[{marker}:"
        if key not in title:
            return None
        seg = title.split(key, 1)[1].split("]", 1)[0]
        if "#" in seg:
            cid, ch = seg.split("#", 1)
            return cid, (int(ch) if ch.isdigit() else 1)
        return seg, 1

    def find_channel_item(self, channel_id: str, kind: str = "channel") -> int | None:
        """Первый шард канала (для обратной совместимости)."""
        shards = self.channel_shards(channel_id, kind)
        return shards[0]["wi_id"] if shards else None

    def channel_shards(self, channel_id: str, kind: str = "channel") -> list[dict]:
        """Все чанки канала: [{wi_id, chunk}] по возрастанию chunk."""
        marker = self._marker(kind)
        proj = self.project.replace("'", "''")
        search = f"{marker}:{channel_id}".replace("'", "''")
        ids = self._wiql(
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{proj}' "
            f"AND [System.Title] CONTAINS '{search}'")
        out = []
        for wi in self.get_batch(ids):
            parsed = self._parse_channel_marker(wi["fields"].get("System.Title", ""), marker)
            if parsed and parsed[0] == channel_id:       # точное совпадение id
                out.append({"wi_id": wi["id"], "chunk": parsed[1]})
        return sorted(out, key=lambda x: x["chunk"])

    def create_channel_item(self, channel: dict, kind: str = "channel",
                            chunk: int = 1) -> int | None:
        """Создать шард-Epic канала (chunk>1 — следующий чанк).
        None, если такой шард уже есть."""
        marker = self._marker(kind)
        full = f"{marker}:{channel['id']}" + (f"#{chunk}" if chunk > 1 else "")
        if self._find_marker(f"[{full}]"):
            return None
        suffix = f" (chunk {chunk})" if chunk > 1 else ""
        title = f"[{full}] {channel.get('name', '')[:160]}{suffix}"
        desc = "<br>".join(f"{k}: {channel.get(k, '')}"
                           for k in ("url", "lang") if channel.get(k))
        ops = [
            {"op": "add", "path": "/fields/System.Title", "value": title},
            {"op": "add", "path": "/fields/System.Description", "value": desc},
            {"op": "add", "path": "/fields/System.Tags",
             "value": f"auto-mech; kind:{kind}; chunk:{chunk}; "
                      f"{config.ADO_STATE_PREFIX}active"},
        ]
        return self._create(config.ADO_CHANNEL_TYPE, ops)

    def _link_successor(self, from_id: int, to_id: int) -> None:
        """from --следующий чанк--> to (Dependency-Forward: предок->потомок)."""
        self._patch(from_id, [{"op": "add", "path": "/relations/-", "value": {
            "rel": "System.LinkTypes.Dependency-Forward",
            "url": f"https://dev.azure.com/{self.org}/_apis/wit/workItems/{to_id}",
        }}])

    def current_channel_shard(self, channel: dict, kind: str = "channel") -> int:
        """Текущий незаполненный чанк канала; создаёт первый или доливает
        следующий (с цепочкой Successor), если последний забит до CHUNK_CAP."""
        cid = channel["id"]
        shards = self.channel_shards(cid, kind)
        if not shards:
            return self.create_channel_item(channel, kind, chunk=1)
        last = shards[-1]
        if len(self.list_child_video_ids(last["wi_id"])) < self.CHUNK_CAP:
            return last["wi_id"]
        new_id = self.create_channel_item(channel, kind, chunk=last["chunk"] + 1)
        self._link_successor(last["wi_id"], new_id)
        return new_id

    def channel_all_child_video_ids(self, channel_id: str,
                                    kind: str = "channel") -> set[str]:
        """video_id всех детей по ВСЕМ чанкам канала — дедуп на уровне канала."""
        out: set[str] = set()
        for sh in self.channel_shards(channel_id, kind):
            out |= self.list_child_video_ids(sh["wi_id"])
        return out

    def attach_video(self, channel: dict, kind: str, video: dict,
                     known: set[str] | None = None) -> int | None:
        """Повесить видео на текущий чанк канала с дедупом по всему каналу.
        `known` — предвычисленный набор video_id (чтобы не дёргать ADO на каждое)."""
        if known is None:
            known = self.channel_all_child_video_ids(channel["id"], kind)
        if video["id"] in known:
            return None
        shard = self.current_channel_shard(channel, kind)
        wi = self.create_video_item(video, parent_id=shard, skip_dedup=True)
        if wi:
            known.add(video["id"])
        return wi

    def list_channel_items(self, kind: str = "channel",
                           active_only: bool = True) -> list[dict]:
        """Активные каналы-источники (по одному на канал, чанки схлопнуты).
        Возвращает [{wi_id, channel_id, name}] — wi_id первого чанка."""
        q = ("SELECT [System.Id] FROM WorkItems "
             f"WHERE [System.TeamProject] = '{self.project}' "
             f"AND [System.Tags] CONTAINS 'auto-mech' "
             f"AND [System.Tags] CONTAINS 'kind:{kind}'")
        if active_only:
            q += f" AND [System.Tags] CONTAINS '{config.ADO_STATE_PREFIX}active'"
        ids = self._wiql(q)
        marker = self._marker(kind)
        by_channel: dict[str, dict] = {}
        for wi in self.get_batch(ids):
            title = wi["fields"].get("System.Title", "")
            parsed = self._parse_channel_marker(title, marker)
            if not parsed:
                continue
            cid, chunk = parsed
            name = title.split("]", 1)[-1].strip()
            if cid not in by_channel or chunk < by_channel[cid]["chunk"]:
                by_channel[cid] = {"wi_id": wi["id"], "channel_id": cid,
                                   "name": name, "chunk": chunk}
        return list(by_channel.values())

    def list_child_video_ids(self, parent_id: int) -> set[str]:
        """video_id всех детей эпика — дешёвый пакетный дедуп при синке
        (1-2 запроса на канал вместо WIQL на каждое видео)."""
        r = self.s.get(f"{self.base}/wit/workitems/{parent_id}"
                       f"?$expand=relations&api-version={API}", timeout=30)
        r.raise_for_status()
        child_ids = [int(rel["url"].rsplit("/", 1)[1])
                     for rel in r.json().get("relations", [])
                     if rel.get("rel") == "System.LinkTypes.Hierarchy-Forward"]
        out: set[str] = set()
        for wi in self.get_batch(child_ids):
            vid = self.video_id_from_title(wi["fields"].get("System.Title", ""))
            if vid:
                out.add(vid)
        return out

    # --- видео/треды (child-уровень, «таблица фактов») -------------------------
    def find_video_item(self, video_id: str) -> int | None:
        return self._find_marker(f"vid:{video_id}")

    def create_video_item(self, video: dict, parent_id: int | None = None,
                          body_html: str = "", skip_dedup: bool = False) -> int | None:
        """video: {id, title, url, channel, channel_id, published_at, duration}.
        Task в state New (default) с Custom.url = ссылка на источник.
        body_html — сырой материал (текст постов форума) прямо в тело тикета:
        ADO = база, «материал уходит в тикет» (форумам так не нужен R2).
        parent_id — Epic-чанк канала. None если такое видео уже есть (дедуп).
        skip_dedup — пропустить WIQL-проверку (вызывающий уже дедупит по known)."""
        if not skip_dedup and self.find_video_item(video["id"]):
            return None
        title = f"[vid:{video['id']}] {video.get('title', '')[:180]}"
        desc = "<br>".join(
            f"{k}: {video.get(k, '')}" for k in
            ("url", "channel", "channel_id", "published_at", "duration"))
        if body_html:
            desc += "<hr>" + body_html
        ops = [
            {"op": "add", "path": "/fields/System.Title", "value": title},
            {"op": "add", "path": "/fields/System.Description", "value": desc},
            {"op": "add", "path": "/fields/System.Tags", "value": "auto-mech"},
        ]
        u = video.get("url", "")
        if u and len(u) <= 255:  # ADO string-поле = 255; длинные форум-URL с %-кириллицей
            ops.append({"op": "add", "path": f"/fields/{config.ADO_URL_FIELD}",
                        "value": u})   # полный URL всегда есть в Description (url:/источник)
        if parent_id:
            ops.append({"op": "add", "path": "/relations/-", "value": {
                "rel": "System.LinkTypes.Hierarchy-Reverse",
                "url": f"https://dev.azure.com/{self.org}/_apis/wit/workItems/{parent_id}",
            }})
        return self._create(config.ADO_WORKITEM_TYPE, ops)

    def append_description(self, wi_id: int, html_block: str) -> None:
        """Дописать HTML-блок в тело тикета — результат процессинга возвращается
        В САМ воркайтем (кейс ремонта рядом с исходным материалом)."""
        wi = self.get(wi_id)
        cur = wi["fields"].get("System.Description", "") or ""
        self._patch(wi_id, [{"op": "add", "path": "/fields/System.Description",
                             "value": cur + html_block}])

    # Маркер блока результата дистилляции — общий для ВСЕХ писателей
    # (tools.save-case, local_distill_batch, local_distill_kimi, import_distill_result).
    CASE_MARKER = "<hr><b>RepairCase"

    def replace_case_block(self, wi_id: int, html_block: str) -> None:
        """Записать блок RepairCase, ЗАМЕНИВ прошлые попытки, а не дописав к ним.

        Раньше все писатели звали append_description, и тикет копил историю: при
        каждой пере-дистилляции (возврат из offtopic/failed, смена модели) в тело
        падал ЕЩЁ один блок. Замер 2026-07-31: #2547 — 9 блоков, тело 232 КБ при
        полезном материале ~1 КБ. Вред двойной: (1) тело пухнет без предела,
        (2) читатели брали ПЕРВЫЙ блок, т.е. самый старый кейс — свежая
        дистилляция уезжала в мусор (переход Kimi -> Gemini так и хоронился).
        Материал тикета — всё, что ДО первого маркера, — сохраняется как есть.
        """
        wi = self.get(wi_id)
        cur = wi["fields"].get("System.Description", "") or ""
        base = cur.split(self.CASE_MARKER, 1)[0]
        self._patch(wi_id, [{"op": "add", "path": "/fields/System.Description",
                             "value": base + html_block}])

    def exists_url(self, url: str) -> int | None:
        """Дельта-поиск: есть ли уже Task с такой Custom.url. Возвращает id или None."""
        ids = self._wiql(
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{self.project}' "
            f"AND [{config.ADO_URL_FIELD}] = '{_wiql_quote(url)}'")
        return ids[0] if ids else None

    # --- бюджет минут CircleCI (стейт в ADO, помесячно на аккаунт) -----------
    def budget_ledger(self, account: str, month: str, create: bool = True) -> int | None:
        """Work item-леджер бюджета аккаунта за месяц. Effort = израсходовано мин.
        Свежий каждый месяц -> ревизии не упираются в лимит 10k."""
        marker = f"automech-budget:{account}:{month}"
        wid = self._find_marker(f"[{marker}]")
        if wid or not create:
            return wid
        ops = [
            {"op": "add", "path": "/fields/System.Title",
             "value": f"[{marker}] CircleCI minutes {account} {month}"},
            {"op": "add", "path": "/fields/System.Tags", "value": "automech-budget"},
            {"op": "add", "path": f"/fields/{config.ADO_BUDGET_FIELD}", "value": 0},
        ]
        return self._create(config.ADO_BUDGET_TYPE, ops)

    def budget_read(self, wi_id: int) -> float:
        wi = self.get(wi_id)
        return float(wi["fields"].get(config.ADO_BUDGET_FIELD, 0) or 0)

    def budget_add(self, wi_id: int, minutes: float) -> float:
        """Прибавить минуты атомарно (rev-test + ретрай при гонке аккаунтов)."""
        for _ in range(6):
            wi = self.get(wi_id)
            cur = float(wi["fields"].get(config.ADO_BUDGET_FIELD, 0) or 0)
            new = max(0.0, round(cur + minutes, 1))
            ops = [
                {"op": "test", "path": "/rev", "value": wi["rev"]},
                {"op": "add", "path": f"/fields/{config.ADO_BUDGET_FIELD}", "value": new},
            ]
            try:
                self._patch(wi_id, ops)
                return new
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code in (400, 409, 412):
                    continue          # кто-то записал раньше — перечитать и повторить
                raise
        raise RuntimeError("budget_add: слишком много конфликтов записи")

    def budget_ledgers(self) -> list[dict]:
        """Все леджеры бюджета (для мониторинга): [{account, month, minutes}]."""
        ids = self._wiql(
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{self.project}' "
            "AND [System.Tags] CONTAINS 'automech-budget'")
        out = []
        for wi in self.get_batch(ids, fields=("System.Title", config.ADO_BUDGET_FIELD)):
            title = wi["fields"].get("System.Title", "")
            if "[automech-budget:" in title:
                acc, month = title.split("[automech-budget:", 1)[1].split("]", 1)[0].split(":")
                out.append({"account": acc, "month": month,
                            "minutes": float(wi["fields"].get(config.ADO_BUDGET_FIELD, 0) or 0)})
        return out

    # --- стейт краула (frontier/seen) в ADO: облачные прогоны идут вглубь -------
    def crawl_state_item(self, zone: str, create: bool = True) -> int | None:
        """Work item-хранилище стейта краула зоны (тип Feature, gzip+base64 в
        Description). Позволяет эфемерным CI-агентам продолжать обход вглубь."""
        marker = f"automech-crawl:{zone}"
        wid = self._find_marker(f"[{marker}]")
        if wid or not create:
            return wid
        ops = [
            {"op": "add", "path": "/fields/System.Title",
             "value": f"[{marker}] crawl frontier zone {zone}"},
            {"op": "add", "path": "/fields/System.Tags", "value": "automech-crawl"},
        ]
        return self._create(config.ADO_BUDGET_TYPE, ops)

    def crawl_state_read(self, zone: str) -> str:
        wid = self.crawl_state_item(zone, create=False)
        if not wid:
            return ""
        return self.get(wid)["fields"].get("System.Description", "") or ""

    def crawl_state_write(self, zone: str, blob_b64: str) -> None:
        wid = self.crawl_state_item(zone, create=True)
        self._patch(wid, [{"op": "add", "path": "/fields/System.Description",
                           "value": blob_b64}])

    # --- глобальный замок эстафеты (кольцо GitHub Actions) ------------------------
    # ЗАЧЕМ. Каждый ручной workflow_dispatch на tick.yml запускает СВОЮ, независимую
    # эстафету: ring_handoff в конце тика дёргает следующий аккаунт по API, не
    # проверяя, бежит ли уже где-то ДРУГАЯ эстафета. concurrency: в workflow не
    # спасает — она держит один прогон только ВНУТРИ одного репозитория, а тут 22
    # разных репозитория. 2026-07-30: несколько ручных тестовых запусков за день
    # породили несколько параллельных цепочек, кольцо крутило 3-4 сканера разом.
    # Замок общий на ВСЁ кольцо (ADO shared, те же креды на всех 22 аккаунтах) —
    # захват атомарный по System.Rev (как claim()). Просроченный (RING_LOCK_TTL_MIN)
    # замок можно перехватить — страховка на случай, если раннер умер, не освободив.
    RING_LOCK_TTL_MIN = 55   # > timeout-minutes джобы (45) + запас на диспетчеризацию

    def _ring_lock_item(self, create: bool = True) -> int | None:
        marker = "automech-ring-lock"
        wid = self._find_marker(f"[{marker}]")
        if wid or not create:
            return wid
        ops = [{"op": "add", "path": "/fields/System.Title",
                "value": f"[{marker}] эстафета кольца — общий замок"},
               {"op": "add", "path": "/fields/System.Tags", "value": "automech-ring-lock"}]
        return self._create(config.ADO_BUDGET_TYPE, ops)

    @staticmethod
    def _lock_state_read(wi: dict) -> dict:
        """System.Description — rich-text поле: ADO HTML-экранирует кавычки при
        чтении (" -> &quot;), поэтому json.loads на сырой строке ломался ВСЕГДА,
        except тихо превращал результат в {} — проверка «занято ли» была мертва,
        замок захватывал кто угодно в любой момент. base64 не содержит кавычек/
        HTML-спецсимволов вовсе (тот же приём уже в crawl_state_read/write) —
        поэтому храним состояние в нём, а не сырым JSON."""
        raw = wi["fields"].get("System.Description", "") or ""
        try:
            return json.loads(base64.b64decode(raw.strip()).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _lock_state_write(state: dict) -> str:
        return base64.b64encode(json.dumps(state).encode("utf-8")).decode("ascii")

    def ring_lock_acquire(self, holder: str) -> bool:
        """Захватить замок эстафеты. False — уже держит другой (свежий) прогон."""
        import time as _time
        wid = self._ring_lock_item(create=True)
        if not wid:
            return True   # не смогли создать work item — не блокируем эстафету
        wi = self.get(wid)
        state = self._lock_state_read(wi)
        age_min = (_time.time() - float(state.get("ts", 0))) / 60.0
        if state.get("holder") and state["holder"] != holder and age_min < self.RING_LOCK_TTL_MIN:
            return False
        payload = self._lock_state_write({"holder": holder, "ts": _time.time()})
        ops = [{"op": "test", "path": "/rev", "value": wi["rev"]},
               {"op": "add", "path": "/fields/System.Description", "value": payload}]
        try:
            self._patch(wid, ops)
            return True
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (400, 409, 412):
                return False   # кто-то захватил в ту же секунду
            raise

    def ring_lock_note(self, holder: str, task: str) -> None:
        """Записать в замок, ЧЕМ узел занят прямо сейчас — для живого борда.

        Замок и так пишется раз в тик и уже хранит {holder, ts}; дописываем сюда
        же имя выбранной задачи. Борду этого хватает, чтобы показать «gh14 —
        дистилляция, 2 мин» вместо безликого «идёт»: узнать задачу иначе нельзя,
        лог активного прогона GitHub не отдаёт, а имя шага у всех задач одно.
        Тихо игнорируем сбой: подпись для UI не стоит того, чтобы ронять тик.
        """
        import time as _time
        wid = self._ring_lock_item(create=False)
        if not wid:
            return
        try:
            wi = self.get(wid)
            state = self._lock_state_read(wi)
            if state.get("holder") != holder:
                return              # замок уже чужой — не перетираем
            state["task"] = task
            state["task_ts"] = _time.time()
            self._patch(wid, [{"op": "add", "path": "/fields/System.Description",
                               "value": self._lock_state_write(state)}])
        except Exception:  # noqa: BLE001
            pass

    def ring_lock_state(self) -> dict:
        """Что записано в замке: {holder, ts, task, task_ts} — читает борд."""
        wid = self._ring_lock_item(create=False)
        if not wid:
            return {}
        try:
            return self._lock_state_read(self.get(wid))
        except Exception:  # noqa: BLE001
            return {}

    def ring_lock_release(self, holder: str) -> None:
        """Снять замок, если он всё ещё за нами (не трогаем чужой).

        holder/ts обнуляем — иначе следующий узел не сможет взять замок. А вот
        ЧТО делали (last_*) оставляем: между тиками замок пуст (сдали и ждём,
        пока GitHub выдаст раннер следующему), и без этой памяти живая строка на
        борде мигала бы, пропадая в каждую паузу. Теперь борд показывает либо
        «идёт X», либо «последнее: X, N мин назад» — и всегда честен.
        """
        import time as _time
        wid = self._ring_lock_item(create=False)
        if not wid:
            return
        wi = self.get(wid)
        state = self._lock_state_read(wi)
        if state.get("holder") != holder:
            return
        tail = {"last_holder": holder, "last_ts": _time.time()}
        if state.get("task"):
            tail["last_task"] = state["task"]
        try:
            self._patch(wid, [{"op": "test", "path": "/rev", "value": wi["rev"]},
                              {"op": "add", "path": "/fields/System.Description",
                               "value": self._lock_state_write(tail)}])
        except requests.HTTPError:
            pass   # не критично: TTL освободит сам

    def claim(self, wi_id: int, worker: str) -> bool:
        """Атомарно застолбить work item за воркером (оптимистичная блокировка
        по System.Rev). False — айтем уже увели (парный аккаунт успел раньше)."""
        wi = self.get(wi_id)
        ops = [
            {"op": "test", "path": "/rev", "value": wi["rev"]},
            {"op": "add", "path": "/fields/System.History",
             "value": f"claimed by {worker}"},
        ]
        try:
            self._patch(wi_id, ops)
            return True
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (400, 409, 412):
                return False
            raise

    def state_counts(self) -> dict[str, int]:
        """ТОЧНЫЙ счёт work items по ЛОГИЧЕСКИМ состояниям — один запрос к
        Analytics OData.

        Зачем не WIQL: он режется лимитом 20000 (VS402337), а бэклог subs уже
        >47k — то есть посчитать глубину очереди обычным запросом НЕЛЬЗЯ в
        принципе, только «есть/нет» (query_by_state с $top). Analytics отдаёт
        точные счётчики группировкой на сервере и лимитом не ограничен.

        Возвращает {логическое_состояние: количество}. Пустой dict, если
        Analytics недоступен (другой скоуп PAT / отключён) — вызывающий должен
        это пережить и откатиться на статическое поведение.
        """
        # Один System.State может отвечать двум логическим (failed/offtopic ->
        # Removed): раскладываем обратно по первому совпадению — конвейеру важны
        # именно очереди new/subs/distilled, а не разбор Removed.
        back = {}
        for logical, real in STATE_MAP.items():
            back.setdefault(real, logical)
        flt = (f"WorkItemType eq '{config.ADO_WORKITEM_TYPE}' "
               "and Tags/any(t:t/TagName eq 'auto-mech')")
        apply_expr = f"filter({flt})/groupby((State),aggregate($count as Count))"
        url = (f"https://analytics.dev.azure.com/{self.org}/{self.project}"
               "/_odata/v3.0-preview/WorkItems?"
               + urllib.parse.urlencode({"$apply": apply_expr}))
        try:
            r = self.s.get(url, headers={"Accept": "application/json"}, timeout=30)
            r.raise_for_status()
            rows = r.json().get("value", [])
        except Exception:  # noqa: BLE001 — не критично, вызывающий откатится
            return {}
        out: dict[str, int] = {}
        for row in rows:
            real = row.get("State")
            if not real:
                continue
            out[back.get(real, real)] = int(row.get("Count") or 0)
        return out

    def query_by_state(self, state: str, top: int = 50,
                       partition: str | None = None) -> list[int]:
        """Task'и в логическом состоянии (маппится на реальный System.State)."""
        real = STATE_MAP.get(state, state)
        # Ограничиваем выборку НА СЕРВЕРЕ (ORDER BY CreatedDate ASC -> самые старые
        # первыми, ровно что нужно конвейеру). Иначе на бэклоге >20000 ADO вернёт
        # VS402337 и весь клеймо-цикл встанет. При partition берём с запасом, т.к.
        # чётность отфильтрует ~половину. Жёсткий потолок 19999 < лимита ADO.
        srv_top = top if partition is None else top * 2 + 10
        srv_top = min(srv_top, 19999)
        proj = _wiql_quote(self.project)
        workitem_type = _wiql_quote(config.ADO_WORKITEM_TYPE)
        state = _wiql_quote(real)
        ids = self._wiql(
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{proj}' "
            f"AND [System.WorkItemType] = '{workitem_type}' "
            f"AND [System.State] = '{state}' "
            "AND [System.Tags] CONTAINS 'auto-mech' "
            "ORDER BY [System.CreatedDate] ASC", top=srv_top)
        # partition оставлен опцией; по умолчанию не делим — полагаемся на claim
        if partition == "even":
            ids = [i for i in ids if i % 2 == 0]
        elif partition == "odd":
            ids = [i for i in ids if i % 2 == 1]
        return ids[:top]

    def set_state(self, wi_id: int, new_state: str, comment: str = "",
                  link: str = "") -> None:
        """Перевести Task в логическое состояние (System.State). offtopic/failed
        -> Removed, но с тегом-причиной, чтобы различать."""
        real = STATE_MAP.get(new_state, new_state)
        ops = [{"op": "add", "path": "/fields/System.State", "value": real}]
        if new_state in ("offtopic", "failed"):
            wi = self.get(wi_id)
            tags = [t.strip() for t in (wi["fields"].get("System.Tags") or "").split(";")
                    if t.strip()]
            if new_state not in tags:
                tags.append(new_state)
            ops.append({"op": "add", "path": "/fields/System.Tags",
                        "value": "; ".join(tags)})
        if comment or link:
            ops.append({"op": "add", "path": "/fields/System.History",
                        "value": f"{comment} {link}".strip()})
        self._patch(wi_id, ops)

    @staticmethod
    def video_id_from_title(title: str) -> str | None:
        if "[vid:" in title:
            return title.split("[vid:", 1)[1].split("]", 1)[0]
        return None

    @staticmethod
    def url_from_description(wi: dict) -> str:
        """Реальный URL источника из description ('url: https://...')."""
        import re
        desc = wi.get("fields", {}).get("System.Description", "") or ""
        m = re.search(r"url:\s*(https?://[^\s<]+)", desc)
        return m.group(1) if m else ""

    @staticmethod
    def source_url(wi: dict) -> str:
        """URL источника: поле Custom.url, иначе из description (обратн. совм.)."""
        return (wi.get("fields", {}).get(config.ADO_URL_FIELD)
                or AdoClient.url_from_description(wi))
