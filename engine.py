"""Движок: получает данные, гоняет детекторы, создаёт сигналы, ведёт бумажные сделки."""
import asyncio
import logging
import time

from bybit import BybitFeed
from config import SIGNAL_TYPES
from detectors import LiqTracker, PriceHistory, VolumeTracker, WallTracker
from paper import PaperTrader

log = logging.getLogger("engine")


# ---------- форматирование ----------
def fp(p):
    if p is None:
        return "-"
    if p >= 1000:
        return f"{p:,.1f}".replace(",", " ")
    if p >= 10:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.6g}"


def fusd(v):
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e9:
        return f"{sign}${a / 1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.2f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:.0f}K"
    return f"{sign}${a:.2f}"


def fdur(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec}с"
    if sec < 3600:
        return f"{sec // 60}м {sec % 60}с"
    return f"{sec // 3600}ч {sec % 3600 // 60}м"


class Engine:
    def __init__(self, settings, storage):
        self.s = settings
        self.db = storage
        self.paper = PaperTrader(settings, storage)
        self.feed = BybitFeed(self._on_book, self._on_trades, self._on_ticker, self._on_liq,
                              depth=settings.get("ob_depth"))
        self.walls, self.vol, self.liq, self.hist = {}, {}, {}, {}
        self.cooldown = {}
        self.outbox = asyncio.Queue()
        self.tasks = []
        self.started_at = time.time()
        self.symbols = []
        self.last_error = None
        self._restart_lock = asyncio.Lock()
        self._stale_alerted = False

    # ---------- жизненный цикл ----------
    async def start(self):
        await self.restart_feed()
        self.tasks = [
            asyncio.create_task(self._loop()),
            asyncio.create_task(self._slow_loop()),
            asyncio.create_task(self._auto_refresh()),
        ]

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        await self.feed.close()

    async def resolve_symbols(self):
        if self.s.get("coin_mode") == "auto":
            try:
                return await self.feed.top_symbols(self.s.get("auto_top_n"), self.s.get("auto_min_turnover"))
            except Exception as e:
                self.last_error = f"авто-подбор монет: {e}"
                log.warning("auto symbols failed: %s", e)
        return list(self.s["coins"])

    async def restart_feed(self):
        async with self._restart_lock:  # защита от одновременных перезапусков (быстрые нажатия кнопок)
            self.symbols = await self.resolve_symbols()
            # данные нужны и по BTC (фильтр), и по монетам с открытыми бумажными сделками,
            # даже если их убрали из списка: иначе сделка не закроется
            extra = {"BTCUSDT"} | {t["symbol"] for t in self.paper.open.values()}
            syms = set(self.symbols) | extra
            for d, cls in ((self.walls, None), (self.vol, VolumeTracker), (self.liq, LiqTracker),
                           (self.hist, PriceHistory)):
                for sym in list(d):
                    if sym not in syms:
                        del d[sym]
                for sym in syms:
                    if sym not in d:
                        d[sym] = WallTracker(sym) if cls is None else cls()
            await self.feed.start(self.symbols, depth=self.s.get("ob_depth"), extra=extra)

    async def _auto_refresh(self):
        while True:
            await asyncio.sleep(1800)
            if self.s.get("coin_mode") == "auto":
                try:
                    new = await self.resolve_symbols()
                    if set(new) != set(self.symbols):
                        await self.restart_feed()
                        self.say(f"🔄 Обновил список монет (auto): {', '.join(new)}")
                except Exception:
                    log.exception("auto refresh")

    def say(self, text):
        self.outbox.put_nowait(text)

    def price(self, sym):
        h = self.hist.get(sym)
        return h.last if h else None

    def prices(self):
        return {s: h.last for s, h in self.hist.items() if h.last}

    # ---------- колбэки фида ----------
    def _on_book(self, sym, book):
        pass  # стакан анализируется в _loop раз в 0.5 сек

    def _on_ticker(self, sym, data):
        pass  # тикеры лежат в self.feed.tickers

    def _on_trades(self, sym, trades):
        if sym not in self.hist:
            return
        touch = self.s.get("approach_pct", sym)
        has_open = any(t["symbol"] == sym for t in self.paper.open.values())
        for ts, price, qty, _side in trades:
            usd = price * qty
            self.vol[sym].add(ts, price, usd)
            self.walls[sym].on_trade(price, usd, touch)
            self.hist[sym].add(ts, price)
            if has_open:
                for t in self.paper.on_price(sym, price):
                    self.say(self._fmt_close(t))
                has_open = any(t["symbol"] == sym for t in self.paper.open.values())

    def _on_liq(self, sym, ts, price, qty, pos_side):
        if sym in self.liq:
            self.liq[sym].add(ts, price * qty, pos_side)

    # ---------- основной цикл ----------
    async def _loop(self):
        while True:
            await asyncio.sleep(0.5)
            now = time.time()
            for sym in self.symbols:
                try:
                    self._check_symbol(sym, now)
                except Exception:
                    log.exception("check %s", sym)

    def _check_symbol(self, sym, now):
        book = self.feed.books.get(sym)
        last = self.price(sym)
        if not book or not book.bids or not book.asks or not last:
            return
        p = lambda k: self.s.get(k, sym)  # noqa: E731
        on = self.s["signals_on"]
        tracker = self.walls[sym]
        events = tracker.scan(book, now, p)
        mid = book.mid()

        # пробой: плотность съели, цена прошла через неё
        if on["breakout"]:
            for ev, w in events:
                if ev != "eaten" or w.age(now) < p("min_wall_age_sec") or w.traded_usd < 0.3 * w.max_usd:
                    continue
                side = "LONG" if w.side == "ask" else "SHORT"
                buf = p("sl_buffer_pct") / 100
                sl = w.price * (1 - buf) if side == "LONG" else w.price * (1 + buf)
                self.emit(sym, "breakout", side, last, sl, {
                    "wall_price": w.price, "wall_usd": w.max_usd, "age": w.age(now),
                    "eaten_usd": w.traded_usd, "trust": w.trust(now),
                })

        # отскок: цена подошла к надёжной плотности
        if on["bounce"]:
            for w in tracker.walls.values():
                if w.signaled or w.age(now) < p("min_wall_age_sec"):
                    continue
                trust = w.trust(now)
                if trust < p("min_trust"):
                    continue
                if w.side == "bid":
                    dist = (mid - w.price) / w.price * 100
                else:
                    dist = (w.price - mid) / mid * 100
                if 0 <= dist <= p("approach_pct"):
                    w.signaled = True
                    side = "LONG" if w.side == "bid" else "SHORT"
                    buf = p("sl_buffer_pct") / 100
                    sl = w.price * (1 - buf) if side == "LONG" else w.price * (1 + buf)
                    self.emit(sym, "bounce", side, last, sl, {
                        "wall_price": w.price, "wall_usd": w.usd, "age": w.age(now),
                        "trust": trust, "moves": w.moves, "eaten_usd": w.traded_usd,
                    })

        # всплеск объёма
        if on["volume"]:
            r = self.vol[sym].check(now)
            if r:
                vol, avg, move = r
                if (avg > 0 and vol >= p("vol_min_usd") and vol >= p("vol_mult") * avg
                        and abs(move) >= p("vol_min_move_pct")):
                    side = "LONG" if move > 0 else "SHORT"
                    d = p("default_sl_pct") / 100
                    sl = last * (1 - d) if side == "LONG" else last * (1 + d)
                    self.emit(sym, "volume", side, last, sl, {"vol": vol, "avg": avg, "move": move})

        # каскад ликвидаций
        if on["liq"]:
            longs, shorts = self.liq[sym].check(now)
            thr = p("liq_usd")
            if longs >= thr or shorts >= thr:
                dumped = longs >= shorts  # ликвидировали лонги -> цена падала
                if p("liq_mode") == "reversal":
                    side = "LONG" if dumped else "SHORT"
                else:
                    side = "SHORT" if dumped else "LONG"
                d = p("default_sl_pct") / 100
                sl = last * (1 - d) if side == "LONG" else last * (1 + d)
                self.liq[sym].clear()
                self.emit(sym, "liq", side, last, sl, {"longs": longs, "shorts": shorts})

    # ---------- сигнал ----------
    def emit(self, sym, typ, side, price, sl, details):
        if self.s["paused"]:
            return
        now = time.time()
        key = (sym, typ, side)
        if now - self.cooldown.get(key, 0) < self.s.get("cooldown_sec", sym):
            return
        # стоп не ближе 0.1%, иначе комиссия съест всё
        min_d = 0.001
        if side == "LONG":
            sl = min(sl, price * (1 - min_d))
        else:
            sl = max(sl, price * (1 + min_d))
        # BTC-фильтр
        if self.s.get("btc_filter") and sym != "BTCUSDT":
            bh = self.hist.get("BTCUSDT")
            b5 = bh.ago(300, now) if bh else None
            if b5 and bh.last:
                bm = (bh.last / b5 - 1) * 100
                lim = self.s.get("btc_filter_pct")
                if (side == "LONG" and bm <= -lim) or (side == "SHORT" and bm >= lim):
                    return
        self.cooldown[key] = now
        risk = abs(price - sl)
        rr = self.s.get("rr", sym)
        tp = price + rr * risk if side == "LONG" else price - rr * risk
        sig = {"ts": now, "symbol": sym, "type": typ, "side": side, "price": price,
               "sl": sl, "tp": tp, "details": details}
        sig["id"] = self.db.add_signal(sig)
        trade, why = self.paper.try_open(sig, sig["id"])
        if self.s["notify"].get(typ, True):
            self.say(self._fmt_signal(sig, trade, why))

    def _fmt_signal(self, sig, trade, why):
        d = sig["details"]
        icon = "🟢" if sig["side"] == "LONG" else "🔴"
        pr = sig["price"]
        lines = [
            f"{icon} <b>{sig['side']} {sig['symbol']}</b> · {SIGNAL_TYPES[sig['type']]}",
            f"Цена: <code>{fp(pr)}</code>",
        ]
        t = sig["type"]
        if t in ("bounce", "breakout"):
            if t == "bounce":
                side_name = "bid" if sig["side"] == "LONG" else "ask"
            else:
                side_name = "ask" if sig["side"] == "LONG" else "bid"
            lines.append(f"Плотность: <code>{fp(d['wall_price'])}</code> {side_name}, {fusd(d['wall_usd'])}, "
                         f"живёт {fdur(d['age'])}")
            extra = f"доверие {d['trust']}/100"
            if d.get("eaten_usd"):
                extra += f", съели {fusd(d['eaten_usd'])}"
            if d.get("moves"):
                extra += f", переставлялась {d['moves']}x"
            lines.append(extra)
        elif t == "volume":
            lines.append(f"Объём за минуту: {fusd(d['vol'])} (в {d['vol'] / d['avg']:.1f}x выше среднего), "
                         f"движение {d['move']:+.2f}%")
        elif t == "liq":
            lines.append(f"Ликвидации за 60с: лонги {fusd(d['longs'])}, шорты {fusd(d['shorts'])}")
        sl_pct = (sig["sl"] / pr - 1) * 100
        tp_pct = (sig["tp"] / pr - 1) * 100
        lines.append(f"Стоп: <code>{fp(sig['sl'])}</code> ({sl_pct:+.2f}%) · Тейк: <code>{fp(sig['tp'])}</code> ({tp_pct:+.2f}%)")
        if trade:
            lines.append(f"📝 Бумажная сделка #{trade['id']}: {trade['qty']:.4g} на {fusd(trade['qty'] * trade['entry'])}")
        elif why and self.s.get("paper_enabled"):
            lines.append(f"📝 Сделка не открыта: {why}")
        lines.append(f"<i>сигнал #{sig['id']}</i>")
        return "\n".join(lines)

    def _fmt_close(self, t):
        icon = "✅" if t["pnl"] > 0 else "❌"
        pct = (t["exit"] / t["entry"] - 1) * 100 * (1 if t["side"] == "LONG" else -1)
        return (f"{icon} Сделка #{t['id']} {t['side']} {t['symbol']} закрыта ({t['reason']})\n"
                f"Вход {fp(t['entry'])} → выход {fp(t['exit'])} ({pct:+.2f}%)\n"
                f"PnL: <b>{t['pnl']:+.2f}$</b> (комиссии {t['fees']:.2f}$) · Баланс: {self.paper.balance():.2f}$")

    # ---------- медленный цикл: исходы сигналов, таймауты ----------
    async def _slow_loop(self):
        while True:
            await asyncio.sleep(5)
            try:
                now = time.time()
                for r in self.db.pending_outcomes():
                    h = self.hist.get(r["symbol"])
                    if not h:
                        continue
                    for col, sec in (("p1", 60), ("p5", 300), ("p15", 900)):
                        if r[col] is None and now - r["ts"] >= sec:
                            px = h.ago(now - (r["ts"] + sec), now)
                            if px is None and now - r["ts"] - sec < 30:
                                px = h.last  # история ещё тонкая, но момент замера только что
                            if px:
                                self.db.set_signal_outcome(r["id"], col, px)
                for t in self.paper.check_timeouts(self.prices()):
                    self.say(self._fmt_close(t))
                await self._watchdog(now)
            except Exception:
                log.exception("slow loop")

    async def _watchdog(self, now):
        """Если данных с Bybit нет больше 90 сек, предупреждаем и перезапускаем поток."""
        last = self.feed.last_msg
        if last and now - last > 90:
            if not self._stale_alerted:
                self.say("⚠️ Нет данных с Bybit больше 90 сек, переподключаюсь...")
                self._stale_alerted = True
            await self.restart_feed()
            self.feed.last_msg = now  # даём время на подключение
        elif self._stale_alerted and last and now - last < 10:
            self._stale_alerted = False
            self.say("✅ Данные с Bybit снова идут")

    # ---------- для команд бота ----------
    def walls_text(self, sym):
        tr = self.walls.get(sym)
        book = self.feed.books.get(sym)
        if not tr or not book or not book.bids:
            return f"По {sym} пока нет данных стакана."
        now = time.time()
        mid = book.mid()
        ws = sorted(tr.walls.values(), key=lambda w: -w.price)
        if not ws:
            return f"<b>{sym}</b> · цена {fp(mid)}\nПлотностей по текущим порогам нет."
        lines = [f"<b>{sym}</b> · цена {fp(mid)}"]
        for w in ws:
            dist = (w.price / mid - 1) * 100
            icon = "🟥" if w.side == "ask" else "🟩"
            lines.append(f"{icon} <code>{fp(w.price)}</code> ({dist:+.2f}%) {fusd(w.usd)} · "
                         f"{fdur(w.age(now))} · доверие {w.trust(now)}")
        return "\n".join(lines)
