"""Картина на графике: тренд на старших таймфреймах, EMA, RSI, максимум и минимум суток.
Свечи грузятся с Bybit раз в несколько минут, текущая цена подставляется живая."""


def ema(values, n):
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, n=14):
    """RSI по Уайлдеру."""
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def parse_klines(raw):
    """Ответ Bybit (новые первыми) -> список свечей от старых к новым: (ts, o, h, l, c)."""
    out = []
    for k in reversed(raw):
        out.append((int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])))
    return out


class ChartState:
    def __init__(self, k15, k60):
        self.k15 = k15   # 15-минутные свечи, от старых к новым
        self.k60 = k60   # часовые свечи

    def features(self, price, side):
        """Признаки для сигнала при текущей цене price."""
        f = {}
        c15 = [k[4] for k in self.k15]
        c60 = [k[4] for k in self.k60]
        long = side == "LONG"
        # тренд: изменение цены за последний час и 4 часа (по 15-минутным свечам)
        if len(c15) >= 17:
            f["tr1h"] = round((price / self.k15[-4][1] - 1) * 100, 3)    # от открытия свечи 45 мин назад
            f["tr4h"] = round((price / self.k15[-16][1] - 1) * 100, 3)
        # RSI 14 на 15-минутках, последняя свеча с живой ценой
        if len(c15) >= 30:
            r = rsi(c15[:-1] + [price])
            if r is not None:
                f["rsi15"] = round(r, 1)
                f["rsi_side"] = round(r if long else 100 - r, 1)
        # EMA 50 и 200 на часовиках
        if len(c60) >= 50:
            e50 = ema(c60[:-1] + [price], 50)
            f["ema50_1h"] = "above" if price > e50 else "below"
        if len(c60) >= 200:
            e200 = ema(c60[:-1] + [price], 200)
            f["ema200_1h"] = "above" if price > e200 else "below"
        # максимум и минимум последних 24 часов
        if len(self.k60) >= 24:
            last = self.k60[-24:]
            hi = max(max(k[2] for k in last), price)
            lo = min(min(k[3] for k in last), price)
            f["dist_hi"] = round((hi / price - 1) * 100, 3)
            f["dist_lo"] = round((price / lo - 1) * 100, 3)
        return f

    def summary(self, price):
        """Короткая строка для сообщения о сигнале."""
        f = self.features(price, "LONG")
        parts = []
        if "tr1h" in f:
            parts.append(f"1ч {f['tr1h']:+.1f}%")
        if "tr4h" in f:
            parts.append(f"4ч {f['tr4h']:+.1f}%")
        if "rsi15" in f:
            parts.append(f"RSI {f['rsi15']:.0f}")
        if "ema200_1h" in f:
            parts.append("выше EMA200" if f["ema200_1h"] == "above" else "ниже EMA200")
        return " · ".join(parts)


def atr(candles, n=14):
    """Средний истинный диапазон по свечам (ts, o, h, l, c), в цене."""
    if len(candles) < n + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i][2], candles[i][3], candles[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def swing_state(ch, price):
    """Картина для трендовой стратегии на часовиках: EMA20/50/200, ATR, откат к EMA20 на 15м."""
    if len(ch.k60) < 210 or len(ch.k15) < 8:
        return None
    closes = [k[4] for k in ch.k60[:-1]] + [price]
    s = {"ema20": ema(closes, 20), "ema50": ema(closes, 50), "ema200": ema(closes, 200),
         "atr": atr(ch.k60[:-1], 14)}
    last3 = ch.k15[-4:-1]                     # три последние закрытые 15-минутки
    s["low3"] = min(k[3] for k in last3)
    s["high3"] = max(k[2] for k in last3)
    s["last_green"] = ch.k15[-2][4] > ch.k15[-2][1]
    s["last_red"] = ch.k15[-2][4] < ch.k15[-2][1]
    return s
