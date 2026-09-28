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
    """Цена с 7 значащими цифрами: хватает для шага цены любой монеты (1.09125, 2674.05, 0.0001234)."""
    if p is None:
        return "-"
    s = f"{p:.7g}"
    if "e" in s:
        s = f"{p:.12f}".rstrip("0").rstrip(".")
    if "." in s:
        s = s.rstrip("0").rstrip(".") if abs(p) < 1000 else s
    whole, _, frac = s.partition(".")
    if len(whole.lstrip("-")) > 3:
        whole = f"{int(whole):,}".replace(",", " ")
    return whole + ("." + frac if frac else "")


def book_depth(book, mid, pct=1.0):
    """Сумма заявок в $ в пределах pct% от цены: (bid, ask)."""
    lim = pct / 100
    bid = sum(px * sz for px, sz in book.bids.items() if (mid - px) / mid <= lim)
    ask = sum(px * sz for px, sz in book.asks.items() if (px - mid) / mid <= lim)
    return bid, ask


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
        self.coin_tags = {}  # монета -> почему она в списке (свой / объём / рост / падение)
        self._picks = {}     # прошлый выбор по каждой категории, для устойчивости списка
        self.depth = {}      # монета -> глубина стакана в пределах 1% (меньшая из сторон), $
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

    @staticmethod
    def _pick(ranked, n, current):
        """Топ-n из ranked, но монеты, которые уже отслеживаются, держим, пока они в топ-2n:
        чтобы список не дёргался из-за монет на границе."""
        keep = [s for s in ranked[:n * 2] if s in current][:n]
        for s in ranked:
            if len(keep) >= n:
                break
            if s not in keep:
                keep.append(s)
        return keep

    async def resolve_symbols(self):
        mode = self.s.get("coin_mode")
        if mode == "manual":
            self.coin_tags = {s: "свой" for s in self.s["coins"]}
            return list(self.s["coins"])
        try:
            market = await self.feed.market(self.s.get("auto_min_turnover"))
        except Exception as e:
            self.last_error = f"авто-подбор монет: {e}"
            log.warning("auto symbols failed: %s", e)
            return list(self.symbols or self.s["coins"])
        self.last_error = None
        info = {m["symbol"]: m for m in market}
        by_vol = [m["symbol"] for m in sorted(market, key=lambda m: -m["turnover"])]
        up = [m["symbol"] for m in sorted(market, key=lambda m: -m["change"]) if m["change"] > 0]
        down = [m["symbol"] for m in sorted(market, key=lambda m: m["change"]) if m["change"] < 0]
        tags = {}
        picks = {}

        def take(cat, ranked, n, label):
            ranked = [x for x in ranked if x not in tags]  # не дублируем уже взятые монеты
            picks[cat] = self._pick(ranked, n, set(self._picks.get(cat, [])))
            for x in picks[cat]:
                tags[x] = label(x)

        if mode == "mix":
            for x in self.s["coins"]:
                tags.setdefault(x, "свой")
        if mode in ("auto", "mix"):
            take("vol", by_vol, self.s.get("auto_top_n"), lambda x: "объём")
        if mode in ("movers", "mix"):
            n = self.s.get("movers_n")
            take("up", up, n, lambda x: f"📈 {info[x]['change']:+.1f}%")
            take("down", down, n, lambda x: f"📉 {info[x]['change']:+.1f}%")
        self._picks = picks
        syms = list(tags)[:max(1, self.s.get("max_coins"))]
        self.coin_tags = {s: tags[s] for s in syms}
        return syms

    def coin_change(self, sym):
        """Изменение за 24ч, %, из тикера Bybit."""
        try:
            return float(self.feed.tickers.get(sym, {}).get("price24hPcnt")) * 100
        except (TypeError, ValueError):
            return None

    def eff(self, key, sym):
        """Значение параметра для монеты: своя настройка монеты > автоподстройка > общая."""
        if key in self.s["overrides"].get(sym, {}):
            return self.s["overrides"][sym][key]
        if key == "min_wall_usd" and self.s.get("auto_scale"):
            return 20_000  # остальное порог плотности берёт из самого стакана (доля и средний уровень)
        if key in ("vol_min_usd", "liq_usd") and self.s.get("auto_scale"):
            try:
                turnover = float(self.feed.tickers.get(sym, {}).get("turnover24h") or 0)
            except ValueError:
                turnover = 0
            if turnover > 0:
                if key == "liq_usd":
                    return max(20_000, turnover * self.s.get("liq_turnover_pct") / 100)
                # минимальный минутный объём для всплеска: 3 средних минуты, но не выше общей настройки
                return min(self.s.get("vol_min_usd"), max(20_000, 3 * turnover / 1440))
        return self.s.get(key)

    async def restart_feed(self, symbols=None):
        async with self._restart_lock:  # защита от одновременных перезапусков (быстрые нажатия кнопок)
            self.symbols = symbols if symbols is not None else await self.resolve_symbols()
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
            await asyncio.sleep(max(5, self.s.get("refresh_min")) * 60)
            if self.s.get("coin_mode") == "manual":
                continue
            try:
                old = set(self.symbols)
                new = await self.resolve_symbols()
                if set(new) != old:
                    await self.restart_feed(new)
                    added = [f"{s.replace('USDT', '')} ({self.coin_tags.get(s, '')})" for s in new if s not in old]
                    removed = [s.replace("USDT", "") for s in old if s not in new]
                    msg = ["🔄 <b>Обновил список монет</b>"]
                    if added:
                        msg.append("Добавил: " + ", ".join(added))
                    if removed:
                        msg.append("Убрал: " + ", ".join(removed))
                    self.say("\n".join(msg))
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
        p = lambda k: self.eff(k, sym)  # noqa: E731
        on = self.s["signals_on"]
        tracker = self.walls[sym]
        events = tracker.scan(book, now, p)
        mid = book.mid()
        self.depth[sym] = min(book_depth(book, mid))

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
    def thin(self, sym):
        """Тонкий стакан: сделка сама сдвинет цену, сигналы по такой монете не даём."""
        d = self.depth.get(sym)
        return d is not None and d < self.eff("min_book_usd", sym)

    def emit(self, sym, typ, side, price, sl, details):
        if self.s["paused"] or self.thin(sym):
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
        trade, why = self.paper.try_open(sig, sig["id"], book=self.feed.books.get(sym))
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
        if sym not in self.feed.books:
            return (f"{sym} сейчас не отслеживается.\n"
                    f"Добавь в свой список: <code>/add {sym.replace('USDT', '')}</code> "
                    "(в режиме manual или mix)")
        if not tr or not book or not book.bids or not book.asks:
            return f"По {sym} стакан ещё загружается, попробуй через пару секунд."
        now = time.time()
        mid = book.mid()
        ws = sorted(tr.walls.values(), key=lambda w: -w.price)
        if sym in self.s["overrides"] and "min_wall_usd" in self.s["overrides"][sym]:
            how = f"своя настройка монеты, мин. {fusd(self.eff('min_wall_usd', sym))}"
        elif self.s.get("auto_scale"):
            how = (f"авто: {self.s.get('wall_share_pct'):g}% заявок стороны или "
                   f"{self.eff('wall_mult', sym):g}x средний уровень")
        else:
            how = f"общая настройка, мин. {fusd(self.eff('min_wall_usd', sym))}"
        max_dist = self.eff("wall_max_dist_pct", sym) / 100
        lines = [f"<b>{sym}</b> · цена {fp(mid)}"]
        thr = " · ".join(f"{side} {fusd(tr.thr[side])}" for side in ("bid", "ask") if side in tr.thr)
        lines.append(f"Порог плотности: {thr or '-'}\n<i>({how})</i>")
        if ws:
            lines.append("")
            for w in ws:
                dist = (w.price / mid - 1) * 100
                icon = "🟥" if w.side == "ask" else "🟩"
                lines.append(f"{icon} <code>{fp(w.price)}</code> ({dist:+.2f}%) {fusd(w.usd)} · "
                             f"x{w.ratio:.0f} к соседям · {fdur(w.age(now))} · доверие {w.trust(now)}")
        else:
            lines.append("\nПлотностей по текущим порогам нет.")
        # самые крупные заявки в зоне поиска: видно, насколько они не дотягивают до порога
        lines.append(f"\n<b>Крупнейшие заявки в пределах {max_dist * 100:g}%:</b>")
        for side, levels, icon in (("ask", book.asks, "🔸"), ("bid", book.bids, "🔹")):
            near = sorted(((px, sz * px) for px, sz in levels.items() if abs(px - mid) / mid <= max_dist),
                          key=lambda x: -x[1])[:3]
            for px, usd in sorted(near, key=lambda x: -x[0]):
                lines.append(f"{icon} <code>{fp(px)}</code> ({(px / mid - 1) * 100:+.2f}%) {fusd(usd)}")
        bd, ad = book_depth(book, mid)
        lines.append(f"\nГлубина стакана ±1%: bid {fusd(bd)} · ask {fusd(ad)}")
        if self.thin(sym):
            lines.append(f"⚠️ <b>Тонкий стакан</b> (меньше {fusd(self.eff('min_book_usd', sym))}): "
                         "сигналы по монете не даются, проскальзывание съест прибыль")
        lo = (min(book.bids) / mid - 1) * 100
        hi = (max(book.asks) / mid - 1) * 100
        lines.append(f"<i>Стакан виден от {lo:+.2f}% до {hi:+.2f}% ({len(book.bids) + len(book.asks)} уровней)</i>")
        if min(-lo, hi) < max_dist * 100 * 0.8:
            lines.append("<i>Стакан виден уже зоны поиска: дальние плотности не видны. "
                         "Можно поднять глубину: /set ob_depth 1000</i>")
        return "\n".join(lines)
