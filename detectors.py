"""Детекторы: плотности (с рейтингом доверия), всплески объёма, ликвидации."""
from collections import deque


class Wall:
    __slots__ = ("side", "price", "usd", "max_usd", "min_usd", "first_seen", "last_seen",
                 "traded_usd", "touched", "moves", "signaled", "ratio", "far")

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
        self.ratio = 0.0          # во сколько раз больше соседних уровней
        self.far = 0.0            # самое большое расстояние цены от плотности за её жизнь, %

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
        self.thr = {}  # сглаженный порог плотности по сторонам, чтобы он не прыгал

    def scan(self, book, now, p):
        """p: функция get(key). Возвращает список событий о пропавших плотностях."""
        best_bid, best_ask = book.best()
        if not best_bid:
            return []
        mid = (best_bid + best_ask) / 2
        max_dist = p("wall_max_dist_pct") / 100
        min_usd = p("min_wall_usd")
        mult = p("wall_mult")

        top_n = max(1, int(p("max_walls_side")))
        share = p("wall_share_pct") / 100 if p("auto_scale") else 0
        win = 10  # сколько соседних уровней с каждой стороны сравниваем
        found = {}
        for side, levels in (("bid", book.bids), ("ask", book.asks)):
            # уровни в порядке стакана: от цены наружу
            items = sorted(levels.items(), reverse=(side == "bid"))
            items = [(px, sz * px) for px, sz in items if abs(px - mid) / mid <= max_dist]
            n = len(items)
            if n < 10:
                continue
            u = [x[1] for x in items]
            pre = [0.0]
            for v in u:
                pre.append(pre[-1] + v)
            # абсолютный минимум: из настройки и доля от всех заявок стороны в зоне
            raw = max(min_usd, pre[-1] * share)
            prev = self.thr.get(side)
            floor = self.thr[side] = raw if prev is None else prev * 0.9 + raw * 0.1
            cands, keep = [], []
            for i, (px, usd) in enumerate(items):
                if usd < floor * 0.7:
                    continue
                lo, hi = max(0, i - win), min(n, i + win + 1)
                cnt = hi - lo - 1
                local = (pre[hi] - pre[lo] - usd) / cnt if cnt else 0
                ratio = usd / local if local > 0 else 999
                if usd >= floor and ratio >= mult:
                    cands.append((usd, px, ratio))
                elif (side, px) in self.walls and ratio >= mult * 0.7:
                    # гистерезис: известную плотность держим, пока она не сильно просела
                    keep.append((usd, px, ratio))
            # новые плотности: только самые крупные аномалии стороны;
            # уже известные держим, пока они в двойном топе
            cands.sort(reverse=True)
            chosen = cands[:top_n] + [c for c in cands[top_n:top_n * 2] if (side, c[1]) in self.walls]
            chosen += sorted(keep, reverse=True)[:max(0, top_n * 2 - len(chosen))]
            for usd, px, ratio in chosen:
                found[(side, px)] = (usd, ratio)

        events = []
        for key, w in list(self.walls.items()):
            if key in found:
                continue
            side, px = key
            del self.walls[key]
            crossed = (side == "bid" and best_bid < px) or (side == "ask" and best_ask > px)
            if crossed:
                events.append(("eaten", w))
                continue
            levels = book.bids if side == "bid" else book.asks
            left_usd = levels.get(px, 0) * px
            if left_usd < 0.3 * w.max_usd:
                # заявку реально сняли: запоминаем, чтобы поймать «переезд»
                self.recent_pulled.append((now, side, px, w.max_usd, w.moves))
                events.append(("pulled", w))
            # иначе она просто выпала из топа по размеру, это не спуфинг

        for key, (usd, ratio) in found.items():
            w = self.walls.get(key)
            dist = abs(key[1] - mid) / mid * 100
            if w:
                w.far = max(w.far, dist)
                w.ratio = ratio
                w.usd = usd
                w.max_usd = max(w.max_usd, usd)
                w.min_usd = min(w.min_usd, usd)
                w.last_seen = now
            else:
                side, px = key
                moves = 0
                for ts, s2, px2, usd2, mv in self.recent_pulled:
                    if (s2 == side and now - ts < 3 and abs(px2 - px) / px < 0.002
                            and 0.7 < usd / usd2 < 1.4):
                        moves = mv + 1
                        break
                self.walls[key] = Wall(side, px, usd, now, moves)
                self.walls[key].ratio = ratio
                self.walls[key].far = dist
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
        while self.trades and self.trades[0][0] < now - 60:
            self.trades.popleft()
        if self.started is None or not self.trades:
            return None
        cur_m = int(now // 60)
        first_full = int(self.started // 60) + 1  # первая полная минута после запуска
        # берём только полные минуты с момента запуска, без текущей и предыдущей
        hist = [self.minutes.get(m, 0) for m in range(max(first_full, cur_m - 31), cur_m - 1)]
        if len(hist) < 8:
            return None
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
        """Цена sec секунд назад или None, если история так далеко не покрывает."""
        target = now - sec
        if not self.points or self.points[0][0] > target + 2:
            return None
        best = None
        for ts, p in self.points:
            if ts <= target:
                best = p
            else:
                break
        return best

    def range_pct(self, sec, now):
        """Размах цены (макс - мин) за последние sec секунд, % от цены. None, если истории мало."""
        if not self.points or self.points[0][0] > now - sec + 5:
            return None
        pts = [p for ts, p in self.points if ts >= now - sec]
        if len(pts) < 2:
            return None
        return (max(pts) - min(pts)) / pts[-1] * 100
