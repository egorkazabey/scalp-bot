"""Движок: получает данные, гоняет детекторы, создаёт сигналы, ведёт бумажные сделки."""
import asyncio
import os
import logging
import time

from bybit import BybitFeed
from config import TYPE_NAMES
from detectors import FlowTracker, LiqTracker, OITracker, PriceHistory, VolumeTracker, WallTracker
from chart import ChartState, parse_klines, swing_state
from config import DATA_DIR, PARAMS
from news import Calendar
from paper import PaperTrader, near_stop_step

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
        return f"{sign}${a / 1e9:.2f}".rstrip("0").rstrip(".") + "B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.2f}".rstrip("0").rstrip(".") + "M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:.0f}K" if a >= 1e4 else f"{sign}${a:,.0f}".replace(",", " ")
    if a >= 100:
        return f"{sign}${a:.0f}"
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
        self.oi, self.flow = {}, {}   # открытый интерес и лента (дельта, поглощение, айсберги)
        self.sweeps = {}              # монета -> состояние проколов уровней для сигнала «вынос стопов»
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
        self.stop_block = {} # монета -> время последнего стопа (пауза по монете)
        self.virtual = {}    # монета -> {id сигнала: {...}}: каждый сигнал доводим до стопа/тейка виртуально
        self.limits = {}     # монета -> [лимитки, ждущие исполнения]
        self.confirms = {}   # монета -> [отскоки, ждущие подтверждения]
        self.charts = {}     # монета -> ChartState (свечи 15м и 1ч)
        self.calendar = Calendar(os.path.join(DATA_DIR, "calendar.json"))
        self._news_active = None   # событие, вокруг которого сейчас пауза
        self.swing_last = {}       # (монета, тип) -> когда был последний длинный сигнал
        self._swing_checked = {}   # монета -> когда последний раз проверяли длинные стратегии
        self.paper.hold_fn = self.hold_sec
        self._stale_alerted = False

    # ---------- жизненный цикл ----------
    async def start(self):
        horizon = time.time() - max(self.s.get("max_hold_min") * 60, self.s.get("swing_hold_hours") * 3600)
        self.db.expire_virtual(horizon)
        for r in self.db.open_virtual(horizon):
            self.virtual.setdefault(r["symbol"], {})[r["id"]] = dict(r)
        await self.restart_feed()
        self.tasks = [
            asyncio.create_task(self._loop()),
            asyncio.create_task(self._slow_loop()),
            asyncio.create_task(self._auto_refresh()),
            asyncio.create_task(self._chart_loop()),
            asyncio.create_task(self._news_loop()),
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
            extra = ({"BTCUSDT"} | {t["symbol"] for t in self.paper.open.values()}
                     | {s for s, v in self.virtual.items() if v})
            syms = set(self.symbols) | extra
            for d, cls in ((self.walls, None), (self.vol, VolumeTracker), (self.liq, LiqTracker),
                           (self.hist, PriceHistory), (self.oi, OITracker), (self.flow, FlowTracker)):
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
                    added = [f"{s.replace('USDT', '')} {self.coin_tags.get(s, '')}".strip() for s in new if s not in old]
                    removed = [s.replace("USDT", "") for s in old if s not in new]
                    msg = [f"🔄 <b>Список монет обновлён</b> · теперь {len(new)}"]
                    if added:
                        msg.append("➕ " + ", ".join(added))
                    if removed:
                        msg.append("➖ " + ", ".join(removed))
                    self.say("\n".join(msg))
            except Exception:
                log.exception("auto refresh")

    async def _chart_loop(self):
        """Раз в 5 минут обновляем свечи по отслеживаемым монетам."""
        await asyncio.sleep(5)
        while True:
            for sym in list(dict.fromkeys(self.symbols + ["BTCUSDT"])):
                try:
                    k15 = parse_klines(await self.feed.fetch_klines(sym, 15, 100))
                    k60 = parse_klines(await self.feed.fetch_klines(sym, 60, 220))
                    self.charts[sym] = ChartState(k15, k60)
                except Exception as e:
                    log.debug("klines %s: %s", sym, e)
                await asyncio.sleep(0.2)
            for sym in [s for s in self.charts if s not in self.symbols and s != "BTCUSDT"]:
                del self.charts[sym]
            await asyncio.sleep(300)

    async def _news_loop(self):
        """Календарь событий: обновление раз в 2 часа (при ошибке через 10 минут)."""
        while True:
            try:
                await self.calendar.refresh(await self.feed.session())
                await asyncio.sleep(7200)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("calendar: %s", e)
                await asyncio.sleep(600)

    def news_event(self, now):
        if not self.s.get("news_pause"):
            return None
        return self.calendar.window(now, self.s.get("news_before_min"), self.s.get("news_after_min"),
                                    self.s.get("news_currencies"), self.s.get("news_impact"))

    def market_regime(self):
        """Режим рынка по BTC: тренд вверх или вниз, боковик, тихо, паника."""
        ch = self.charts.get("BTCUSDT")
        px = self.price("BTCUSDT")
        if not ch or not px or len(ch.k15) < 17:
            return None
        f = ch.features(px, "LONG")
        tr1, tr4 = f.get("tr1h", 0), f.get("tr4h", 0)
        last = ch.k15[-5:-1]
        vol = sum((k[2] - k[3]) / k[4] * 100 for k in last) / len(last)   # средний размах 15м свечи, %
        if tr1 <= -1.5 or (vol >= 1.0 and tr1 < 0):
            return "паника"
        if abs(tr4) >= 1.5:
            return "тренд вверх" if tr4 > 0 else "тренд вниз"
        if vol <= 0.15:
            return "тихо"
        return "боковик"

    def say(self, text, symbol=None):
        """symbol: к сообщению добавится кнопка с графиком этой монеты на Bybit."""
        self.outbox.put_nowait((text, symbol))

    def price(self, sym):
        h = self.hist.get(sym)
        return h.last if h else None

    def prices(self):
        return {s: h.last for s, h in self.hist.items() if h.last}

    # ---------- колбэки фида ----------
    def _on_book(self, sym, book):
        pass  # стакан анализируется в _loop раз в 0.5 сек

    def _on_ticker(self, sym, data):
        # тикеры лежат в self.feed.tickers; открытый интерес копим для истории
        v = data.get("openInterestValue")
        if v and sym in self.oi:
            try:
                self.oi[sym].add(time.time(), float(v))
            except ValueError:
                pass

    def _on_trades(self, sym, trades):
        if sym not in self.hist:
            return
        touch = self.s.get("approach_pct", sym)
        has_open = any(t["symbol"] == sym for t in self.paper.open.values())
        virt = self.virtual.get(sym)
        flow = self.flow.get(sym)
        for ts, price, qty, taker in trades:
            if flow is not None:
                flow.add(ts, price, price * qty, taker)
            if virt:
                self._check_virtual(sym, price)
            if self.limits.get(sym):
                self._check_limits(sym, price)
            usd = price * qty
            self.vol[sym].add(ts, price, usd)
            self.walls[sym].on_trade(price, usd, touch)
            self.hist[sym].add(ts, price)
            if has_open:
                for t in self.paper.on_price(sym, price):
                    self.say(self._fmt_close(t), t["symbol"])
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
                typ = "breakout"
                if p("breakout_mode") == "fade":
                    # ставка на ложный пробой: входим против, стоп по минимальному расстоянию
                    side = "SHORT" if side == "LONG" else "LONG"
                    typ = "breakout_fade"
                    sl = last * (1 - buf) if side == "LONG" else last * (1 + buf)
                else:
                    sl = w.price * (1 - buf) if side == "LONG" else w.price * (1 + buf)
                self.emit(sym, typ, side, last, sl, {
                    "wall_price": w.price, "wall_usd": w.max_usd, "age": w.age(now), "ratio": w.ratio,
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
                # цена должна прийти к плотности издалека, а не просто стоять рядом с момента её появления
                if 0 <= dist <= p("approach_pct") and w.far >= p("approach_pct") * 1.5:
                    w.signaled = True
                    side = "LONG" if w.side == "bid" else "SHORT"
                    buf = p("sl_buffer_pct") / 100
                    sl = w.price * (1 - buf) if side == "LONG" else w.price * (1 + buf)
                    details = {"wall_price": w.price, "wall_usd": w.usd, "age": w.age(now), "ratio": w.ratio,
                               "trust": trust, "moves": w.moves, "eaten_usd": w.traded_usd}
                    live = p("bounce_entry")
                    # все три способа входа проверяются, торгует только выбранный, остальные виртуально
                    self.emit(sym, "bounce", side, last, sl, details, shadow=(live != "touch"))
                    off = p("limit_offset_pct") / 100
                    self.limits.setdefault(sym, []).append({
                        "typ": "bounce_limit", "side": side, "shadow": live != "limit",
                        "price": w.price * (1 + off) if side == "LONG" else w.price * (1 - off),
                        "sl": sl, "wall": (w.side, w.price), "placed": now,
                        "expires": now + p("entry_wait_sec"), "details": dict(details)})
                    self.confirms.setdefault(sym, []).append({
                        "side": side, "shadow": live != "confirm", "wall": (w.side, w.price), "sl": sl,
                        "eaten0": w.traded_usd, "t0": now, "expires": now + p("confirm_window_sec"),
                        "details": dict(details)})

        self._check_pending(sym, tracker, mid, last, now, p)

        # длинные стратегии: тренд и перекос фандинга (проверяем раз в 30 секунд)
        if (on.get("trend") or on.get("funding")) and now - self._swing_checked.get(sym, 0) >= 30:
            self._swing_checked[sym] = now
            self._check_swing(sym, last, now, p, on)

        # вынос стопов: прокол максимума или минимума суток / 4 часов и быстрый возврат
        if on.get("sweep"):
            self._check_sweeps(sym, last, now, p)

        # всплеск объёма
        if on["volume"]:
            r = self.vol[sym].check(now)
            if r:
                vol, avg, move = r
                if (avg > 0 and vol >= p("vol_min_usd") and vol >= p("vol_mult") * avg
                        and abs(move) >= p("vol_min_move_pct")):
                    side = "LONG" if move > 0 else "SHORT"
                    typ = "volume"
                    if p("volume_mode") == "reversal":
                        side = "SHORT" if side == "LONG" else "LONG"
                        typ = "volume_rev"
                    d = p("default_sl_pct") / 100
                    sl = last * (1 - d) if side == "LONG" else last * (1 + d)
                    self.emit(sym, typ, side, last, sl, {"vol": vol, "avg": avg, "move": move})

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

    def emit(self, sym, typ, side, price, sl, details, shadow=False, maker_entry=False):
        """shadow: вариант проверяется только виртуально (без сделки и уведомления).
        maker_entry: вход лимиткой (комиссия мейкера, без проскальзывания)."""
        if self.s["paused"] or self.thin(sym):
            return
        now = time.time()
        key = (sym, typ, side)
        if now - self.cooldown.get(key, 0) < self.s.get("cooldown_sec", sym):
            return
        # стоп не ближе min_sl_pct: иначе шум и комиссия выбивают сделку
        min_d = max(self.eff("min_sl_pct", sym), 0.1) / 100
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
        f = sig["features"] = self._features(sym, typ, side, price, sl, details, now)
        if shadow:
            f["shadow"] = 1
        if maker_entry:
            f["maker"] = 1
        blocked = self._filter_reason(sym, f, typ)
        if blocked:
            f["filtered"] = blocked
        sig["id"] = self.db.add_signal(sig)
        # каждый сигнал доводим до стопа/тейка виртуально: так результат есть по всем сигналам,
        # даже если бумажная сделка не открылась, и по ним учится автопауза и /analyze
        self.virtual.setdefault(sym, {})[sig["id"]] = {
            "id": sig["id"], "ts": now, "symbol": sym, "type": typ, "side": side,
            "price": price, "sl": sl, "tp": tp, "maker": maker_entry, "be_sl": None, "res": {}}
        if shadow or blocked or self.auto_paused(typ, sym):
            return  # сигнал записан и сопровождается виртуально, но без сделки и уведомления
        pause_left = self.stop_block.get(sym, 0) + self.s.get("stop_pause_min") * 60 - now
        if pause_left > 0:
            trade, why = None, f"пауза по монете после стопа, ещё {fdur(pause_left)}"
        else:
            mult = self.eff("strong_size_mult", sym) if self.is_strong(sym, f) else 1.0
            trade, why = self.paper.try_open(sig, sig["id"], book=self.feed.books.get(sym),
                                             maker_entry=maker_entry, size_mult=mult)
        if self.s["notify"].get(typ.split("_")[0], True):
            self.say(self._fmt_signal(sig, trade, why), sym)

    # ---------- лимитки и подтверждения ----------
    def _check_limits(self, sym, price):
        """Лимитка исполняется, когда цена прошла сквозь её уровень (одного касания мало: очередь)."""
        keep = []
        for o in self.limits.get(sym, []):
            long = o["side"] == "LONG"
            if (long and price < o["price"]) or (not long and price > o["price"]):
                d = dict(o["details"], waited=time.time() - o["placed"])
                self.emit(sym, o["typ"], o["side"], o["price"], o["sl"], d,
                          shadow=o["shadow"], maker_entry=True)
            else:
                keep.append(o)
        self.limits[sym] = keep

    def _check_pending(self, sym, tracker, mid, last, now, p):
        # лимитки: отмена по времени или если плотность исчезла
        self.limits[sym] = [o for o in self.limits.get(sym, [])
                            if now < o["expires"] and o["wall"] in tracker.walls]
        # подтверждение отскока: плотность выдержала удар и цена пошла назад
        keep = []
        for c in self.confirms.get(sym, []):
            w = tracker.walls.get(c["wall"])
            if w is None or now >= c["expires"]:
                continue
            away = ((mid - w.price) / w.price if c["side"] == "LONG" else (w.price - mid) / w.price) * 100
            eaten = w.traded_usd - c["eaten0"]
            if eaten >= w.max_usd * p("confirm_eat_pct") / 100 and away >= p("confirm_move_pct"):
                d = dict(c["details"], confirm_sec=now - c["t0"], eaten_after=eaten, trust=w.trust(now))
                self.emit(sym, "bounce_confirm", c["side"], last, c["sl"], d, shadow=c["shadow"])
            else:
                keep.append(c)
        self.confirms[sym] = keep

    def _check_swing(self, sym, last, now, p, on):
        ch = self.charts.get(sym)
        if not ch:
            return
        st = swing_state(ch, last)
        if not st or not st["atr"]:
            return
        stop = p("swing_atr_mult") * st["atr"]
        base = {"ema20": st["ema20"], "ema50": st["ema50"], "ema200": st["ema200"],
                "atr": st["atr"], "atr_pct": st["atr"] / last * 100}
        # тренд: по направлению часового тренда, на откате к EMA20 и развороте 15-минутки
        if on.get("trend") and now - self.swing_last.get((sym, "trend"), 0) >= p("trend_cooldown_hours") * 3600:
            e20, e50, e200 = st["ema20"], st["ema50"], st["ema200"]
            side = None
            if e50 > e200 and last > e200 and st["low3"] <= e20 * 1.002 and last > e20 and st["last_green"]:
                side = "LONG"
            elif e50 < e200 and last < e200 and st["high3"] >= e20 * 0.998 and last < e20 and st["last_red"]:
                side = "SHORT"
            if side:
                self.swing_last[(sym, "trend")] = now
                sl = last - stop if side == "LONG" else last + stop
                self.emit(sym, "trend", side, last, sl, dict(base))
        # перекос фандинга: толпа сильно в одну сторону, а цена за час уже пошла против неё
        if on.get("funding") and now - self.swing_last.get((sym, "funding"), 0) >= p("funding_cooldown_hours") * 3600:
            try:
                fr = float(self.feed.tickers.get(sym, {}).get("fundingRate")) * 100
            except (TypeError, ValueError):
                fr = None
            if fr is not None and abs(fr) >= p("funding_extreme_pct"):
                tr1h = ch.features(last, "LONG").get("tr1h", 0)
                side = None
                if fr > 0 and tr1h < 0:
                    side = "SHORT"     # толпа в лонгах, а цена падает: лонгам придётся закрываться
                elif fr < 0 and tr1h > 0:
                    side = "LONG"      # толпа в шортах, а цена растёт
                if side:
                    self.swing_last[(sym, "funding")] = now
                    sl = last - stop if side == "LONG" else last + stop
                    self.emit(sym, "funding", side, last, sl, dict(base, funding=fr, tr1h=tr1h))

    def _sweep_levels(self, sym):
        """Уровни, за которыми обычно стоят стопы: максимум и минимум суток и последних 4 часов
        (по закрытым свечам)."""
        ch = self.charts.get(sym)
        if not ch or len(ch.k60) < 25 or len(ch.k15) < 17:
            return []
        d, h4 = ch.k60[-25:-1], ch.k15[-17:-1]
        out = [("суток", "hi", max(k[2] for k in d)), ("суток", "lo", min(k[3] for k in d))]
        hi4, lo4 = max(k[2] for k in h4), min(k[3] for k in h4)
        if abs(hi4 / out[0][2] - 1) > 0.001:
            out.append(("4ч", "hi", hi4))
        if abs(lo4 / out[1][2] - 1) > 0.001:
            out.append(("4ч", "lo", lo4))
        return out

    def _check_sweeps(self, sym, last, now, p):
        st = self.sweeps.setdefault(sym, {})
        mx, win, reclaim = p("sweep_max_pct"), p("sweep_window_sec"), p("sweep_reclaim_pct")
        # минимальный прокол: не меньше настройки и не меньше половины обычной 15-минутной свечи,
        # иначе это просто дрожание цены у уровня
        mn = p("sweep_min_pct")
        ch = self.charts.get(sym)
        if ch and len(ch.k15) >= 9:
            rng = sum((k[2] - k[3]) / k[4] * 100 for k in ch.k15[-9:-1]) / 8
            mn = max(mn, rng * 0.5)
        vr = self.vol[sym].check(now) if sym in self.vol else None
        avg_sec = vr[1] / 60 if vr and vr[1] > 0 else None
        for name, kind, lvl in self._sweep_levels(sym):
            key = (name, kind)
            s = st.get(key)
            if s and s["level"] != lvl:      # уровень обновился (новые свечи): начинаем заново
                s = st[key] = None
            beyond = (last - lvl) / lvl * 100 if kind == "hi" else (lvl - last) / lvl * 100
            if s is None:
                if beyond > 0:
                    st[key] = {"level": lvl, "ext": last, "t0": now, "dead": False}
                continue
            if kind == "hi":
                s["ext"] = max(s["ext"], last)
            else:
                s["ext"] = min(s["ext"], last)
            pierce = abs(s["ext"] / lvl - 1) * 100
            if pierce > mx:
                s["dead"] = True             # ушла слишком далеко: это настоящий пробой
            if beyond <= -reclaim:           # цена уверенно вернулась за уровень
                took = now - s["t0"]
                ok = not s["dead"] and took <= win and pierce >= mn
                vol_x = None
                if ok and avg_sec and sym in self.flow:
                    # объём в сторону прокола за время прокола: сработавшие стопы
                    taker = "Buy" if kind == "hi" else "Sell"
                    pv = sum(u for ts, _, u, sd in self.flow[sym].trades if ts >= s["t0"] - 2 and sd == taker)
                    vol_x = pv / (avg_sec * max(took, 15))
                    ok = vol_x >= p("sweep_vol_mult")
                if ok:
                    side = "SHORT" if kind == "hi" else "LONG"
                    buf = p("sl_buffer_pct") / 100
                    sl = s["ext"] * (1 + buf) if side == "SHORT" else s["ext"] * (1 - buf)
                    self.emit(sym, "sweep", side, last, sl, {
                        "level": lvl, "level_name": name, "kind": kind, "pierce": pierce,
                        "extreme": s["ext"], "took": took, "vol_x": vol_x})
                st[key] = None
            elif beyond <= 0 and not s["dead"] and pierce < mn:
                st[key] = None               # вернулась, не дойдя до нужного прокола: просто шум
            elif now - s["t0"] > win:
                s["dead"] = True

    def _filter_reason(self, sym, f, typ=None):
        """Фильтры из выводов анализа. Отфильтрованный сигнал всё равно проверяется виртуально."""
        if f.get("news"):
            return f"важные новости: {f['news']}"
        block = [x.strip() for x in self.s.get("regime_block").split(",") if x.strip()]
        if f.get("regime") and f["regime"] in block:
            return f"режим рынка: {f['regime']}"
        if typ in self.SWING_TYPES:
            # фильтры скальпинга (глубина, движение за сутки, факторы, дельта) к длинным сделкам не относятся
            coins = [c.strip() for c in (self.s.get("blocked_coins") or "").split(",") if c.strip()] \
                if "blocked_coins" in PARAMS else []
            return "монета в списке запрещённых" if sym.replace("USDT", "") in coins else None
        mx = self.eff("max_depth_usd", sym)
        if mx and f.get("depth") and f["depth"] > mx:
            return "крупная монета с очень глубоким стаканом"
        mn = self.eff("min_coin_move_pct", sym)
        if mn and f.get("chg24") is not None and abs(f["chg24"]) < mn:
            return "монета почти не двигается за сутки"
        coins = [c.strip() for c in (self.s.get("blocked_coins") or "").split(",") if c.strip()]
        if sym.replace("USDT", "") in coins:
            return "монета в списке запрещённых"
        mc = self.eff("min_confluence", sym)
        if mc and f.get("conf", 0) < mc:
            return f"совпало факторов {f.get('conf', 0)} из нужных {mc}"
        lvl = self.eff("delta_block_lvl", sym)
        if self.eff("delta_block", sym) and f.get("delta1") is not None and f["delta1"] <= -lvl:
            return f"за минуту рынок давит против сделки (дельта {f['delta1']:+.2f})"
        return None

    def is_strong(self, sym, f):
        sc = self.eff("strong_conf", sym)
        return bool(sc) and f.get("conf", 0) >= sc

    # ---------- обучение: обстановка, виртуальный результат, автопауза ----------
    def _features(self, sym, typ, side, price, sl, details, now):
        lt = time.localtime(now)
        f = {"hour": lt.tm_hour, "wd": lt.tm_wday, "side": side,
             "sl_pct": round(abs(sl / price - 1) * 100, 4),
             "chg24": self.coin_change(sym), "depth": self.depth.get(sym)}
        book = self.feed.books.get(sym)
        if book and book.bids and book.asks:
            b, a = book.best()
            f["spread"] = round((a - b) / ((a + b) / 2) * 100, 5)
        h = self.hist.get(sym)
        if h:
            f["vola5"] = h.range_pct(300, now)
            p15 = h.ago(900, now)
            if p15:
                f["move15"] = round((price / p15 - 1) * 100, 4)
        bh = self.hist.get("BTCUSDT")
        b5 = bh.ago(300, now) if bh else None
        if b5 and bh.last:
            bm = (bh.last / b5 - 1) * 100
            f["btc5"] = round(bm, 4)
            f["btc_dir"] = "flat" if abs(bm) < 0.05 else (
                "with" if (bm > 0) == (side == "LONG") else "against")
        for k in ("trust", "ratio", "age", "moves"):
            if k in details:
                f[k] = details[k]
        if "vol" in details and details.get("avg"):
            f["vol_x"] = round(details["vol"] / details["avg"], 2)
            f["vol_move"] = details.get("move")
        if "longs" in details:
            f["liq_usd"] = details["longs"] + details["shorts"]
        # режим рынка и новости
        reg = self.market_regime()
        if reg:
            f["regime"] = reg
        ev = self.news_event(now)
        if ev:
            f["news"] = ev[1]
        # картина на графике: тренд, EMA, RSI, уровни суток
        ch = self.charts.get(sym)
        if ch:
            try:
                f.update(ch.features(price, side))
            except Exception:
                log.exception("chart features %s", sym)
        long = side == "LONG"
        # открытый интерес: новые деньги заходят или позиции закрываются
        if sym in self.oi:
            oi5 = self.oi[sym].change_pct(300, now)
            if oi5 is not None:
                f["oi5"] = round(oi5, 3)
                p5 = h.ago(300, now) if h else None
                if p5:
                    pc = (price / p5 - 1) * 100
                    if abs(oi5) >= 0.2:
                        f["oi_regime"] = ("новые лонги" if oi5 > 0 else "закрытие шортов") if pc >= 0 else \
                                         ("новые шорты" if oi5 > 0 else "закрытие лонгов")
        # фандинг: куда перекошена толпа
        try:
            fr = float(self.feed.tickers.get(sym, {}).get("fundingRate")) * 100
            f["funding"] = round(fr, 4)
            if abs(fr) > 0.012:
                f["crowd"] = "с толпой" if (fr > 0) == long else "против толпы"
            else:
                f["crowd"] = "нейтрально"
        except (TypeError, ValueError):
            pass
        # лента: дельта, поглощение, айсберги
        flow = self.flow.get(sym)
        if flow is not None:
            d1, b1, s1, _ = flow.delta(60, now)
            d5, _, _, _ = flow.delta(300, now)
            f["delta1"] = round(d1 if long else -d1, 3)
            f["delta5"] = round(d5 if long else -d5, 3)
            vr = self.vol[sym].check(now) if sym in self.vol else None
            avg_min = vr[1] if vr else None
            ab = flow.absorption(now, avg_min)
            if ab:
                f["absorb"] = "за сделку" if (ab == "buy") == long else "против сделки"
            book = self.feed.books.get(sym)
            if book and book.bids and book.asks:
                ice = flow.icebergs(book, book.mid(), now, max(10_000, (avg_min or 0) * 0.3))
                # айсберги по плотностям: съели больше, чем было видно, а она стоит
                for w in self.walls.get(sym, WallTracker(sym)).walls.values():
                    if w.traded_usd >= 1.5 * w.max_usd:
                        ice[w.side] = max(ice.get(w.side, 0), w.traded_usd)
                ours, theirs = ("bid", "ask") if long else ("ask", "bid")
                if ours in ice and theirs not in ice:
                    f["iceberg"] = "за сделку"
                elif theirs in ice and ours not in ice:
                    f["iceberg"] = "против сделки"
                elif ice:
                    f["iceberg"] = "с обеих сторон"
        # совпадение факторов в пользу сделки
        conf = []
        if f.get("btc_dir") == "with":
            conf.append("btc")
        vr = self.vol[sym].check(now) if sym in self.vol else None
        if vr and vr[1] > 0 and vr[0] >= 2 * vr[1]:
            conf.append("volume")
        if sym in self.liq:
            lg, sh = self.liq[sym].check(now)
            if lg + sh >= 0.3 * self.eff("liq_usd", sym):
                conf.append("liq")
        if details.get("ratio", 0) >= 20:
            conf.append("wall")
        elif sym in self.walls:
            want = "bid" if side == "LONG" else "ask"
            if any(w.side == want and abs(w.price / price - 1) <= 0.005 for w in self.walls[sym].walls.values()):
                conf.append("wall")
        f["conf"] = len(conf)
        f["conf_list"] = conf
        return f

    def _virtual_r(self, v, exit_price, result):
        """Итог виртуальной сделки в % от позиции, с комиссиями и проскальзыванием."""
        sign = 1 if v["side"] == "LONG" else -1
        slip = self.s.get("slippage_pct") / 100
        fee = self.s.get("fee_pct")
        maker = self.s.get("maker_fee_pct")
        entry = v["price"] if v.get("maker") else v["price"] * (1 + sign * slip)
        fees = maker if v.get("maker") else fee
        if result == "tp":
            fees += maker
        else:
            exit_price = exit_price * (1 - sign * slip)
            fees += fee
        return sign * (exit_price / entry - 1) * 100 - fees

    # варианты выхода: (безубыток, выход на втором подходе к стопу) -> колонка в БД
    EXITS = {"base": (False, False, "r_pct"), "be": (True, False, "r_be"),
             "near": (False, True, "r_near"), "both": (True, True, "r_both")}

    def exit_col(self):
        """Колонка результата для текущих настроек выхода."""
        be, near = bool(self.s.get("breakeven")), bool(self.s.get("near_stop_exit"))
        return next(c for b, n, c in self.EXITS.values() if b == be and n == near)

    def _check_virtual(self, sym, price):
        through = self.s.get("tp_through_pct") / 100
        trig = self.s.get("be_trigger")
        zone, reset = self.s.get("near_stop_zone"), self.s.get("near_stop_reset")
        for sid, v in list(self.virtual.get(sym, {}).items()):
            long = v["side"] == "LONG"
            res = v.setdefault("res", {})
            legs = v.setdefault("legs", {})
            tp_hit = (long and price > v["tp"] * (1 + through)) or (not long and price < v["tp"] * (1 - through))
            for leg, (use_be, use_near, _) in self.EXITS.items():
                if leg in res:
                    continue
                st = legs.setdefault(leg, {})
                if use_be and st.get("be_sl") is None:
                    span = v["tp"] - v["price"]
                    if span and (price - v["price"]) / span >= trig:
                        st["be_sl"] = self.paper.be_price(
                            v["price"], v["side"], self.s.get("maker_fee_pct") if v.get("maker") else None)
                stop = st.get("be_sl") or v["sl"]
                if (long and price <= stop) or (not long and price >= stop):
                    res[leg] = ("sl", stop)
                elif tp_hit:
                    res[leg] = ("tp", v["tp"])
                elif use_near and st.get("be_sl") is None and near_stop_step(st, v["price"], v["sl"], price,
                                                                              zone, reset):
                    res[leg] = ("near", price)
            if all(leg in res for leg in self.EXITS):
                self._finish_virtual(v)

    def _finish_virtual(self, v, price=None):
        res = v.setdefault("res", {})
        for leg in self.EXITS:
            if leg not in res and price:
                res[leg] = ("time", price)
        self.virtual.get(v["symbol"], {}).pop(v["id"], None)
        r = {leg: self._virtual_r(v, res[leg][1], res[leg][0]) for leg in self.EXITS}
        self.db.set_signal_result(v["id"], res["base"][0], r["base"], r["be"], r["near"], r["both"])
        if self.s.get("auto_pause"):
            self._update_pauses(v["type"], v["symbol"])

    def _timeout_virtual(self, now):
        for sym, d in list(self.virtual.items()):
            for sid, v in list(d.items()):
                limit = self.hold_sec(v["type"])
                if now - v["ts"] >= limit:
                    px = self.price(sym)
                    if px:
                        self._finish_virtual(v, px)
                    elif now - v["ts"] >= limit * 3:
                        d.pop(sid, None)
                        self.db.set_signal_result(sid, "lost", None)

    SWING_TYPES = ("trend", "funding")

    def hold_sec(self, typ):
        """Сколько держать сделку: длинные стратегии часами, скальпинг минутами."""
        if typ in self.SWING_TYPES:
            return self.s.get("swing_hold_hours") * 3600
        return self.s.get("max_hold_min") * 60

    def auto_paused(self, typ, sym):
        ap = self.s["auto_paused"]
        return ap.get(f"type:{typ}") or ap.get(f"coin:{sym}")

    @staticmethod
    def _perf(rs):
        n = len(rs)
        if not n:
            return 0, 0.0, 0.0
        pos = sum(r for r in rs if r > 0)
        neg = -sum(r for r in rs if r < 0)
        pf = pos / neg if neg else float("inf")
        return n, sum(rs) / n, pf

    def _update_pauses(self, typ, sym):
        ap = self.s["auto_paused"]
        changed = False
        checks = (
            (f"type:{typ}", TYPE_NAMES.get(typ, typ), self.s.get("pause_window"),
             dict(typ=typ), -0.05, 0.8),
            (f"coin:{sym}", sym.replace("USDT", ""), self.s.get("pause_coin_window"), dict(symbol=sym), -0.15, 0.7),
        )
        for key, name, win, flt, bad_avg, bad_pf in checks:
            if key not in ap:
                since = self.s["pause_reset"].get(key, 0)
                n, avg, pf = self._perf(self.db.last_results(win, since=since, col=self.exit_col(), **flt))
                if n >= win and avg < bad_avg and pf < bad_pf:
                    why = f"последние {n} сигналов: в среднем {avg:+.2f}% на сделку, профит-фактор {pf:.2f}"
                    ap[key] = {"since": time.time(), "why": why}
                    changed = True
                    self.say(f"🧠 <b>Автопауза: {name}</b>\n"
                             f"Последние {n} сигналов в минусе: в среднем {avg:+.2f}% на сделку, "
                             f"профит-фактор {pf:.2f}.\n\n"
                             "Сделок и уведомлений по ним не будет. Сигналы продолжаю проверять "
                             "виртуально и верну сам, когда станет плюс. Вернуть сейчас: /unpause")
            else:
                # возвращаем, когда свежая половина окна (собранная уже во время паузы) в плюсе
                half = max(5, win // 2)
                since = ap[key]["since"]
                col = self.exit_col()
                rs = [(r[col] if r[col] is not None else r["r_pct"]) for r in self.db.results(since=since)
                      if (flt.get("typ") in (None, r["type"])) and (flt.get("symbol") in (None, r["symbol"]))]
                n, avg, pf = self._perf(rs[-half:])
                if n >= half and avg > 0.02 and pf > 1.1:
                    del ap[key]
                    self.s["pause_reset"][key] = time.time()
                    changed = True
                    self.say(f"🧠 <b>Автопауза снята: {name}</b>\n"
                             f"Пока была пауза, последние {n} сигналов дали в среднем {avg:+.2f}% "
                             f"на сделку, профит-фактор {pf:.2f}. Снова торгую.")
        if changed:
            self.s.save()

    def _fmt_signal(self, sig, trade, why):
        d = sig["details"]
        long = sig["side"] == "LONG"
        pr = sig["price"]
        typ = sig["type"]
        t = typ.split("_")[0]
        sl_pct = (sig["sl"] / pr - 1) * 100
        tp_pct = (sig["tp"] / pr - 1) * 100
        f = sig.get("features", {})
        strong = self.is_strong(sig["symbol"], f)
        lines = [
            f"{'🟢' if long else '🔴'} <b>{sig['side']} {sig['symbol'].replace('USDT', '')}</b>"
            + ("  💪 <b>сильный сигнал</b>" if strong else ""),
            f"<i>{TYPE_NAMES.get(typ, typ)}</i>",
        ]
        if f.get("conf_list"):
            names = {"btc": "BTC по пути", "volume": "объём", "liq": "ликвидации", "wall": "плотность"}
            lines.append(f"Факторов {f['conf']}: " + ", ".join(names.get(c, c) for c in f["conf_list"]))
        lines += [
            "",
            f"Вход   <code>{fp(pr)}</code>",
            f"Стоп   <code>{fp(sig['sl'])}</code>  {sl_pct:+.2f}%",
            f"Тейк   <code>{fp(sig['tp'])}</code>  {tp_pct:+.2f}%",
            "",
        ]
        if t in ("bounce", "breakout"):
            wall = f"<code>{fp(d['wall_price'])}</code>"
            if t == "bounce":
                kind = "на покупку" if long else "на продажу"
                lines.append(f"🧱 Цена у плотности {kind} {wall}")
                lines.append("ждём отскок " + ("вверх" if long else "вниз"))
            else:
                # у пробоя стенка стояла против движения цены
                up = (long and typ == "breakout") or (not long and typ == "breakout_fade")
                kind = "на продажу" if up else "на покупку"
                lines.append(f"🧱 Съели плотность {kind} {wall}, цена прошла " + ("вверх" if up else "вниз"))
                if typ == "breakout_fade":
                    lines.append("ставка на ложный пробой: возврат " + ("вниз" if up else "вверх"))
            if typ == "bounce_limit":
                lines.append(f"📌 Вошли лимиткой у плотности, ждали {fdur(d.get('waited', 0))}")
            elif typ == "bounce_confirm":
                lines.append(f"✔️ Плотность выдержала: съели {fusd(d.get('eaten_after', 0))}, "
                             f"цена пошла назад через {fdur(d.get('confirm_sec', 0))}")
            info = [fusd(d["wall_usd"])]
            if d.get("eaten_usd"):
                info.append(f"съели {fusd(d['eaten_usd'])}")
            if d.get("ratio"):
                info.append(f"x{d['ratio']:.0f} к соседям")
            lines.append(" · ".join(info))
            trust = f"Доверие {d['trust']}/100 · живёт {fdur(d['age'])}"
            if d.get("moves"):
                trust += f" · переставлялась {d['moves']}x"
            lines.append(trust)
        elif t == "volume":
            lines.append(f"📊 Объём {fusd(d['vol'])} за минуту, x{d['vol'] / d['avg']:.0f} к среднему")
            lines.append(f"цена за минуту {d['move']:+.2f}%" +
                         (", ставка на откат" if typ == "volume_rev" else ", вход по импульсу"))
        elif t == "liq":
            lines.append(f"💥 Ликвидации за минуту: лонги {fusd(d['longs'])} · шорты {fusd(d['shorts'])}")
        elif t == "trend":
            up = sig["side"] == "LONG"
            lines.append(f"📈 Часовой тренд {'вверх' if up else 'вниз'} (EMA50 {'выше' if up else 'ниже'} EMA200), "
                         f"цена откатилась к EMA20 <code>{d['ema20']:.5g}</code> и развернулась")
            lines.append(f"стоп {self.s.get('swing_atr_mult'):g} ATR (ATR часа {d['atr_pct']:.2f}%) · "
                         f"держим до {self.s.get('swing_hold_hours')} ч")
        elif t == "funding":
            crowd = "в лонгах" if d["funding"] > 0 else "в шортах"
            lines.append(f"💸 Фандинг {d['funding']:+.3f}% за 8ч: толпа {crowd}, "
                         f"а цена за час {d['tr1h']:+.2f}%")
            lines.append(f"ставка против толпы · стоп {self.s.get('swing_atr_mult'):g} ATR "
                         f"(ATR часа {d['atr_pct']:.2f}%) · держим до {self.s.get('swing_hold_hours')} ч")
        elif t == "sweep":
            what = "максимум" if d["kind"] == "hi" else "минимум"
            lines.append(f"🎣 Прокололи {what} {d['level_name']} <code>{fp(d['level'])}</code> на {d['pierce']:.2f}% "
                         f"и за {fdur(d['took'])} вернулись назад")
            if d.get("vol_x"):
                lines.append(f"объём на проколе x{d['vol_x']:.1f} к обычному")
            lines.append("стопы собраны, ставка на возврат · стоп за проколом")
        ch = self.charts.get(sig["symbol"])
        if ch:
            summ = ch.summary(pr)
            if summ:
                lines.append(f"🕯 График: {summ}")
        lines.append("")
        if trade:
            notional = trade["qty"] * trade["entry"]
            lev = self.s.get("max_leverage")
            fee = self.s.get("fee_pct") / 100
            entry_fee = trade.get("entry_fee_pct", self.s.get("fee_pct")) / 100
            risk = (abs(trade["entry"] - trade["sl"]) * trade["qty"] + notional * (entry_fee + fee)
                    + trade["sl"] * trade["qty"] * self.s.get("slippage_pct") / 100)
            lines.append(f"📝 Сделка #{trade['id']} · позиция {fusd(notional)} · залог {fusd(notional / lev)} ×{lev:g}"
                         + (f" · размер ×{trade['size_mult']:g}" if trade.get("size_mult", 1) != 1 else ""))
            lines.append(f"Риск на стопе ≈ {fusd(risk)} с комиссиями")
        elif why and self.s.get("paper_enabled"):
            lines.append(f"📝 Без сделки: {why}")
        lines.append(f"<i>сигнал #{sig['id']}</i>")
        return "\n".join(lines)

    def _fmt_close(self, t):
        if t.get("reason") == "стоп":
            self.stop_block[t["symbol"]] = time.time()
        icon = {"тейк": "✅", "стоп": "❌", "время": "⏱", "вручную": "✋", "безубыток": "⚪",
                "второй подход к стопу": "🚪"}.get(t["reason"], "•")
        if t["reason"] in ("время", "вручную", "второй подход к стопу"):
            icon = ("✅ " if t["pnl"] > 0 else "❌ ") + icon
        pct = (t["exit"] / t["entry"] - 1) * 100 * (1 if t["side"] == "LONG" else -1)
        mins = fdur(time.time() - t["open_ts"]) if t.get("open_ts") else ""
        return (f"{icon} <b>{t['reason'].capitalize()} · {t['side']} {t['symbol'].replace('USDT', '')}</b>"
                f"  <i>#{t['id']}</i>\n"
                f"<code>{fp(t['entry'])} → {fp(t['exit'])}</code>  {pct:+.2f}%\n"
                f"PnL <b>{'+' if t['pnl'] >= 0 else '-'}{fusd(abs(t['pnl']))}</b>"
                f"  (комиссии {fusd(t['fees'])}) · {mins}\n"
                f"Баланс ${self.paper.balance():,.2f}".replace(",", " "))

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
                    self.say(self._fmt_close(t), t["symbol"])
                self._timeout_virtual(now)
                self._news_notify(now)
                await self._watchdog(now)
            except Exception:
                log.exception("slow loop")

    def _news_notify(self, now):
        ev = self.news_event(now)
        if ev and ev != self._news_active:
            self._news_active = ev
            t = time.strftime("%H:%M", time.localtime(ev[0]))
            until = time.strftime("%H:%M", time.localtime(ev[0] + self.s.get("news_after_min") * 60))
            self.say(f"📰 <b>Пауза на новостях</b>\n{ev[1]} ({ev[2]}) в {t}.\n"
                     f"Новые сделки не открываю до {until}, открытые веду как обычно.")
        elif not ev and self._news_active:
            self._news_active = None
            self.say("📰 Пауза на новостях закончилась, снова открываю сделки.")

    async def _watchdog(self, now):
        """Если данных с Bybit нет больше 90 сек, предупреждаем и перезапускаем поток."""
        last = self.feed.last_msg
        if last and now - last > 90:
            if not self._stale_alerted:
                self.say("⚠️ <b>Bybit молчит больше 90 секунд</b>\nПереподключаюсь, напишу, когда данные пойдут.")
                self._stale_alerted = True
            await self.restart_feed()
            self.feed.last_msg = now  # даём время на подключение
        elif self._stale_alerted and last and now - last < 10:
            self._stale_alerted = False
            self.say("✅ <b>Связь с Bybit восстановлена</b>")

    # ---------- для команд бота ----------
    def walls_text(self, sym):
        tr = self.walls.get(sym)
        book = self.feed.books.get(sym)
        name = sym.replace("USDT", "")
        if sym not in self.feed.books:
            return (f"🧱 <b>{name}</b> сейчас не отслеживается.\n"
                    f"Добавь её: <code>/add {name}</code> (работает в режимах «Свои» и «Всё»)")
        if not tr or not book or not book.bids or not book.asks:
            return f"🧱 По {name} стакан ещё загружается, попробуй через пару секунд."
        now = time.time()
        mid = book.mid()
        max_dist = self.eff("wall_max_dist_pct", sym) / 100
        lines = [f"🧱 <b>{name}</b> · цена <code>{fp(mid)}</code>", ""]

        def row(kind, px, usd, extra=""):
            return f"{kind:<4} {fp(px):>10} {(px / mid - 1) * 100:+6.2f}% {fusd(usd):>7}{extra}"

        ws = tr.walls.values()
        asks = sorted((w for w in ws if w.side == "ask"), key=lambda w: -w.price)
        bids = sorted((w for w in ws if w.side == "bid"), key=lambda w: -w.price)
        if asks or bids:
            t = [row("прод", w.price, w.usd, f"  x{w.ratio:<3.0f} {w.trust(now):>3} {fdur(w.age(now)):>7}")
                 for w in asks]
            t.append(f"{'':4} {fp(mid):>10}  цена")
            t += [row("пок", w.price, w.usd, f"  x{w.ratio:<3.0f} {w.trust(now):>3} {fdur(w.age(now)):>7}")
                  for w in bids]
            lines.append("<b>Плотности</b>")
            lines.append("<pre>" + "\n".join(t) + "</pre>")
            lines.append("<i>x: во сколько раз больше соседей, затем доверие 0-100 и сколько живёт</i>")
        else:
            lines.append("Плотностей по текущим порогам нет.")
            lines.append(f"\n<b>Самые крупные заявки</b> (в пределах {max_dist * 100:g}%)")
            t = []
            for kind, levels in (("прод", book.asks), ("пок", book.bids)):
                near = sorted(((px, sz * px) for px, sz in levels.items() if abs(px - mid) / mid <= max_dist),
                              key=lambda x: -x[1])[:3]
                t += [row(kind, px, usd) for px, usd in sorted(near, key=lambda x: -x[0])]
            lines.append("<pre>" + "\n".join(t) + "</pre>")

        thr = " · ".join(f"{'покупка' if side == 'bid' else 'продажа'} {fusd(tr.thr[side])}"
                         for side in ("bid", "ask") if side in tr.thr)
        if sym in self.s["overrides"] and "min_wall_usd" in self.s["overrides"][sym]:
            how = "своя настройка монеты"
        elif self.s.get("auto_scale"):
            how = (f"авто: минимум x{self.eff('wall_mult', sym):g} к соседям "
                   f"и {self.s.get('wall_share_pct'):g}% заявок своей стороны")
        else:
            how = "общая настройка"
        lines.append(f"\nПорог: {thr or '-'}\n<i>{how}</i>")
        bd, ad = book_depth(book, mid)
        lines.append(f"\nСтакан ±1%: покупка {fusd(bd)} · продажа {fusd(ad)}")
        if self.thin(sym):
            lines.append(f"⚠️ <b>Тонкий стакан</b> (меньше {fusd(self.eff('min_book_usd', sym))}): "
                         "сигналы по монете не даются, проскальзывание съест прибыль")
        lo = (min(book.bids) / mid - 1) * 100
        hi = (max(book.asks) / mid - 1) * 100
        seen = f"Стакан виден от {lo:+.2f}% до {hi:+.2f}%"
        if min(-lo, hi) < max_dist * 100 * 0.8:
            if self.s.get("ob_depth") < 1000:
                seen += ". Дальние плотности не видны, можно поднять глубину: /set ob_depth 1000"
            else:
                seen += ". Это максимум Bybit, дальние плотности не видны"
        lines.append(f"<i>{seen}</i>")
        return "\n".join(lines)
