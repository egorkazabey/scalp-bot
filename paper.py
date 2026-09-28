"""Бумажная торговля: виртуальные сделки по сигналам с учётом комиссий и проскальзывания."""
import time
from datetime import datetime


def today_start():
    d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp()


class PaperTrader:
    def __init__(self, settings, storage):
        self.s = settings
        self.db = storage
        self.open = {}   # id -> dict
        for r in storage.open_trades():
            self.open[r["id"]] = dict(r)

    def balance(self):
        return self.s.get("start_balance") + self.db.realized_pnl()

    def day_pnl(self):
        return self.db.realized_pnl(since=today_start())

    def daily_stop_hit(self):
        return self.day_pnl() <= -self.balance() * self.s.get("daily_loss_pct") / 100

    def unrealized(self, prices):
        total = 0.0
        for t in self.open.values():
            p = prices.get(t["symbol"])
            if p:
                sign = 1 if t["side"] == "LONG" else -1
                total += sign * (p - t["entry"]) * t["qty"] - t["fees"]
        return total

    @staticmethod
    def walk_book(book, side, qty):
        """Средняя цена исполнения рыночного ордера на qty по текущему стакану.
        None, если стакана не хватило."""
        if book is None:
            return None
        levels = sorted(book.asks.items()) if side == "LONG" else sorted(book.bids.items(), reverse=True)
        left, cost = qty, 0.0
        for px, sz in levels:
            take = min(left, sz)
            cost += take * px
            left -= take
            if left <= 1e-12:
                return cost / qty
        return None

    def try_open(self, sig, signal_id, book=None):
        """Возвращает (trade или None, причина отказа). С book вход считается проходом по стакану."""
        if not self.s.get("paper_enabled"):
            return None, "бумажная торговля выключена"
        if any(t["symbol"] == sig["symbol"] for t in self.open.values()):
            return None, "по монете уже есть открытая сделка"
        if len(self.open) >= self.s.get("max_open"):
            return None, "достигнут лимит открытых сделок"
        if self.daily_stop_hit():
            return None, "дневной лимит убытка достигнут"

        slip = self.s.get("slippage_pct") / 100
        fee = self.s.get("fee_pct") / 100
        sign = 1 if sig["side"] == "LONG" else -1
        entry = sig["price"] * (1 + sign * slip)
        risk_dist = abs(entry - sig["sl"]) / entry
        if risk_dist <= 0 or (sign == 1 and sig["sl"] >= entry) or (sign == -1 and sig["sl"] <= entry):
            return None, "стоп с неправильной стороны"
        bal = self.balance()
        if bal <= 0:
            return None, "бумажный баланс закончился, сбрось счёт"
        risk_usd = bal * self.s.get("risk_pct") / 100
        notional = min(risk_usd / risk_dist, bal * self.s.get("max_leverage"))
        qty = notional / entry
        if book is not None:
            fill = self.walk_book(book, sig["side"], qty)
            if fill is None:
                return None, "в стакане не хватает заявок на такой объём"
            # берём худшее из фиксированного проскальзывания и реального прохода по стакану
            entry = max(entry, fill) if sign == 1 else min(entry, fill)
            if (sign == 1 and sig["sl"] >= entry) or (sign == -1 and sig["sl"] <= entry):
                return None, "проскальзывание по стакану дошло до стопа"
            notional = qty * entry
        t = {
            "signal_id": signal_id, "symbol": sig["symbol"], "type": sig["type"],
            "side": sig["side"], "entry": entry, "qty": qty, "sl": sig["sl"], "tp": sig["tp"],
            "open_ts": time.time(), "fees": notional * fee,
        }
        t["id"] = self.db.open_trade(t)
        self.open[t["id"]] = t
        return t, None

    def on_price(self, symbol, price):
        """Проверка стопов/тейков. Возвращает закрытые сделки."""
        closed = []
        for tid, t in list(self.open.items()):
            if t["symbol"] != symbol:
                continue
            long = t["side"] == "LONG"
            if (long and price <= t["sl"]) or (not long and price >= t["sl"]):
                closed.append(self._close(tid, t["sl"], "стоп", slip=True))
            elif (long and price >= t["tp"]) or (not long and price <= t["tp"]):
                # тейк стоит лимитным ордером: без проскальзывания и с мейкерской комиссией
                closed.append(self._close(tid, t["tp"], "тейк", slip=False, maker=True))
        return closed

    def check_timeouts(self, prices):
        closed = []
        limit = self.s.get("max_hold_min") * 60
        for tid, t in list(self.open.items()):
            if time.time() - t["open_ts"] >= limit and prices.get(t["symbol"]):
                closed.append(self._close(tid, prices[t["symbol"]], "время", slip=True))
        return closed

    def close_manual(self, tid, price):
        if tid in self.open:
            return self._close(tid, price, "вручную", slip=True)

    def _close(self, tid, price, reason, slip, maker=False):
        t = self.open.pop(tid)
        sign = 1 if t["side"] == "LONG" else -1
        if slip:
            price = price * (1 - sign * self.s.get("slippage_pct") / 100)
        fee_pct = self.s.get("maker_fee_pct") if maker else self.s.get("fee_pct")
        fees = t["fees"] + t["qty"] * price * fee_pct / 100
        pnl = sign * (price - t["entry"]) * t["qty"] - fees
        self.db.close_trade(tid, price, pnl, fees, reason)
        t.update(exit=price, pnl=pnl, fees=fees, reason=reason, close_ts=time.time())
        return t

    def reset(self):
        self.open.clear()
        self.db.reset_paper()
