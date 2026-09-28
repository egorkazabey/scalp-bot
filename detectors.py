"""Детекторы: плотности (с рейтингом доверия), всплески объёма, ликвидации."""
import statistics
from collections import deque


class Wall:
    __slots__ = ("side", "price", "usd", "max_usd", "min_usd", "first_seen", "last_seen",
                 "traded_usd", "touched", "moves", "signaled")

    def __init__(self, side, price, usd, now, moves=0):
        self.side = side          # "bid" (поддержка) или "ask" (сопротивление)
        self.price = price
        self.usd = usd
        self.max_usd = usd
        self.min_usd = usd
        self.first_seen = now
        self.last_seen = now
        self.traded_usd = 0.0     # сколько по ней уже исполнили
        self.touched = False      # цена доходила до неё
        self.moves = moves        # сколько раз она «переезжала» (признак спуфинга)
        self.signaled = False

    def age(self, now):
        return now - self.first_seen

    def trust(self, now):
        """0-100. Живёт долго, её едят, а она стоит, размер стабилен -> выше.
        Переставляется за ценой -> ниже."""
        s = min(self.age(now) / 120, 1) * 40
        if self.max_usd > 0:
            s += 20 * (self.min_usd / self.max_usd)
            if self.traded_usd > 0:
                s += 30 * min(self.traded_usd / (0.1 * self.max_usd), 1)
        if self.touched:
            s += 10
        s -= 25 * self.moves
        return max(0, min(100, round(s)))


class WallTracker:
    def __init__(self, symbol):
        self.symbol = symbol
        self.walls = {}                    # (side, price) -> Wall
        self.recent_pulled = deque(maxlen=20)  # (ts, side, price, usd, moves) для поиска «переездов»

    def scan(self, book, now, p):
        """p: функция get(key). Возвращает список событий о пропавших плотностях."""
        best_bid, best_ask = book.best()
        if not best_bid:
            return []
        mid = (best_bid + best_ask) / 2
        max_dist = p("wall_max_dist_pct") / 100
        min_usd = p("min_wall_usd")
        mult = p("wall_mult")

        found = {}
        for side, levels in (("bid", book.bids), ("ask", book.asks)):
            near = [(px, sz * px) for px, sz in levels.items() if abs(px - mid) / mid <= max_dist]
            if len(near) < 10:
                continue
            med = statistics.median(u for _, u in near)
            thr = max(min_usd, med * mult)
            for px, usd in near:
                key = (side, px)
                # гистерезис: уже известную плотность держим, пока она > половины порога
                if usd >= thr or (key in self.walls and usd >= thr * 0.5):
                    found[key] = usd

        events = []
        for key, w in list(self.walls.items()):
            if key in found:
                continue
            side, px = key
            crossed = (side == "bid" and best_bid < px) or (side == "ask" and best_ask > px)
            del self.walls[key]
            if crossed:
                events.append(("eaten", w))
            else:
                self.recent_pulled.append((now, side, px, w.max_usd, w.moves))
                events.append(("pulled", w))

        for key, usd in found.items():
            w = self.walls.get(key)
            if w:
                w.usd = usd
                w.max_usd = max(w.max_usd, usd)
                w.min_usd = min(w.min_usd, usd)
                w.last_seen = now
            else:
                side, px = key
                moves = 0
                for ts, s2, px2, usd2, mv in self.recent_pulled:
                    if (s2 == side and now - ts < 5 and abs(px2 - px) / px < 0.005
                            and 0.6 < usd / usd2 < 1.6):
                        moves = mv + 1
                        break
                self.walls[key] = Wall(side, px, usd, now, moves)
        return events

    def on_trade(self, price, usd, touch_pct):
        for w in self.walls.values():
            if abs(price - w.price) / w.price <= 1e-9:
                w.traded_usd += usd
                w.touched = True
            elif abs(price - w.price) / w.price * 100 <= touch_pct:
                w.touched = True


class VolumeTracker:
    """Минутный объём против среднего за последние 30 минут."""

    def __init__(self):
        self.trades = deque()      # (ts, usd, price) за последние 60 сек
        self.minutes = {}          # minute -> usd
        self.started = None

    def add(self, ts, price, usd):
        if self.started is None:
            self.started = ts
        self.trades.append((ts, usd, price))
        m = int(ts // 60)
        self.minutes[m] = self.minutes.get(m, 0) + usd
        while self.trades and self.trades[0][0] < ts - 60:
            self.trades.popleft()
        if len(self.minutes) > 40:
            for k in sorted(self.minutes)[:-35]:
                del self.minutes[k]

    def check(self, now):
        """(объём за 60с, средний минутный, движение % за 60с) или None, если мало истории."""
        if self.started is None or now - self.started < 600 or not self.trades:
            return None
        cur_m = int(now // 60)
        hist = [self.minutes.get(m, 0) for m in range(cur_m - 31, cur_m - 1)]
        avg = sum(hist) / len(hist)
        vol = sum(u for _, u, _ in self.trades)
        p0 = self.trades[0][2]
        p1 = self.trades[-1][2]
        return vol, avg, (p1 / p0 - 1) * 100


class LiqTracker:
    def __init__(self):
        self.items = deque()  # (ts, usd, pos_side)

    def add(self, ts, usd, pos_side):
        self.items.append((ts, usd, pos_side))

    def check(self, now):
        while self.items and self.items[0][0] < now - 60:
            self.items.popleft()
        longs = sum(u for _, u, s in self.items if s == "Buy")
        shorts = sum(u for _, u, s in self.items if s == "Sell")
        return longs, shorts

    def clear(self):
        self.items.clear()


class PriceHistory:
    """Цена раз в секунду за последние 20 минут: для исходов сигналов и BTC-фильтра."""

    def __init__(self):
        self.points = deque()
        self.last = None

    def add(self, ts, price):
        self.last = price
        if not self.points or ts - self.points[-1][0] >= 1:
            self.points.append((ts, price))
            while self.points and self.points[0][0] < ts - 1200:
                self.points.popleft()

    def ago(self, sec, now):
        target = now - sec
        best = None
        for ts, p in self.points:
            if ts <= target:
                best = p
            else:
                break
        return best
