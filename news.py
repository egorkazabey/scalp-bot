"""Календарь важных экономических событий (ФРС, инфляция, занятость и т.п.).
Источник: публичная лента ForexFactory, обновляется раз в пару часов и кешируется на диск."""
import json
import logging
import time
from datetime import datetime

import aiohttp

log = logging.getLogger("news")
URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
IMPACT = {"High": 3, "Medium": 2, "Low": 1}


class Calendar:
    def __init__(self, path):
        self.path = path
        self.events = []      # (ts, title, country, impact)
        self.updated = 0
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.events = [tuple(e) for e in d.get("events", [])]
            self.updated = d.get("updated", 0)
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"updated": self.updated, "events": self.events}, f, ensure_ascii=False)
        except OSError:
            pass

    @staticmethod
    def parse(raw):
        out = []
        for e in raw:
            try:
                ts = datetime.fromisoformat(e["date"]).timestamp()
            except (KeyError, ValueError, TypeError):
                continue
            out.append((ts, e.get("title", ""), e.get("country", ""), e.get("impact", "")))
        return sorted(out)

    async def refresh(self, session):
        async with session.get(URL, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            raw = await r.json(content_type=None)
        self.events = self.parse(raw)
        self.updated = time.time()
        self._save()

    def relevant(self, currencies, min_impact):
        need = IMPACT.get(min_impact, 3)
        cur = {c.strip().upper() for c in currencies.split(",") if c.strip()}
        return [e for e in self.events if e[2].upper() in cur and IMPACT.get(e[3], 0) >= need]

    def window(self, now, before_min, after_min, currencies, min_impact):
        """Событие, в окно которого попадает now, или None."""
        for e in self.relevant(currencies, min_impact):
            if e[0] - before_min * 60 <= now <= e[0] + after_min * 60:
                return e
        return None

    def upcoming(self, now, currencies, min_impact, limit=10):
        return [e for e in self.relevant(currencies, min_impact) if e[0] >= now - 3600][:limit]
