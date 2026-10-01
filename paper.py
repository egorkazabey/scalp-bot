"""Бумажная торговля: виртуальные сделки по сигналам с учётом комиссий и проскальзывания."""
import time
from datetime import datetime


def near_stop_step(state, entry, sl0, price, zone, reset):
    """Считает подходы цены к стопу. state: dict с near_in, near_n. True, если это второй подход."""
    span = entry - sl0
    if span == 0:
        return False
    frac = (price - sl0) / span      # 1 на входе, 0 на стопе (для лонга и шорта одинаково)
    if frac <= zone:
        if not state.get("near_in"):
            state["near_in"] = True
            state["near_n"] = state.get("near_n", 0) + 1
            return state["near_n"] >= 2
    elif frac >= reset:
        state["near_in"] = False
    return False


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

    def try_open(self, sig, signal_id, book=None, maker_entry=False, size_mult=1.0):
        """Возвращает (trade или None, причина отказа). С book вход считается проходом по стакану."""
        if not self.s.get("paper_enabled"):
            return None, "бумажная торговля выключена"
        if any(t["symbol"] == sig["symbol"] for t in self.open.values()):
            return None, "по монете уже есть открытая сделка"
        # длинные сделки (тренд, фандинг) держатся часами: у них свои места, чтобы не занимать скальпинг
        swing = sig["type"] in self.swing_types
        group = [t for t in self.open.values() if (t.get("type") in self.swing_types) == swing]
        if len(group) >= self.s.get("swing_max_open" if swing else "max_open"):
            return None, "достигнут лимит открытых " + ("длинных сделок" if swing else "сделок")
        same = sum(1 for t in group if t["side"] == sig["side"])
        if same >= self.s.get("max_same_side"):
            return None, f"уже {same} сделки в {sig['side']}: рынок двигается вместе, не удваиваем ставку"
        if self.daily_stop_hit():
            return None, "дневной лимит убытка достигнут"

        slip = self.s.get("slippage_pct") / 100
        fee = self.s.get("fee_pct") / 100
        entry_fee = self.s.get("maker_fee_pct") / 100 if maker_entry else fee
        sign = 1 if sig["side"] == "LONG" else -1
        # лимитка исполняется по своей цене, без проскальзывания; рыночный вход с проскальзыванием
        entry = sig["price"] if maker_entry else sig["price"] * (1 + sign * slip)
        risk_dist = abs(entry - sig["sl"]) / entry
        if risk_dist <= 0 or (sign == 1 and sig["sl"] >= entry) or (sign == -1 and sig["sl"] <= entry):
            return None, "стоп с неправильной стороны"
        bal = self.balance()
        if bal <= 0:
            return None, "бумажный баланс закончился, сбрось счёт"
        lev = self.s.get("max_leverage")
        if self.s.get("size_mode") == "margin":
            # фиксированный залог: margin_pct% баланса x плечо
            notional = bal * self.s.get("margin_pct") / 100 * lev
        else:
            # фиксированный риск: на стопе теряем risk_pct% баланса ВМЕСТЕ с комиссиями за вход
            # и выход и проскальзыванием на стопе
            risk_usd = bal * self.s.get("risk_pct") / 100
            loss_per_usd = risk_dist + entry_fee + fee + slip
            notional = risk_usd / loss_per_usd
        # сильный сигнал: позиция больше (и риск на стопе больше во столько же раз)
        notional = min(notional * max(size_mult, 0.1), bal * lev)
        qty = notional / entry
        if book is not None and not maker_entry:
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
            "open_ts": time.time(), "fees": notional * entry_fee, "entry_fee_pct": entry_fee * 100,
            "size_mult": size_mult,
        }
        t["id"] = self.db.open_trade(t)
        self.open[t["id"]] = t
        return t, None

    def on_price(self, symbol, price):
        """Проверка стопов/тейков. Возвращает закрытые сделки."""
        closed = []
        # тейк это лимитка в очереди: одного касания мало, цена должна пройти сквозь уровень
        through = self.s.get("tp_through_pct") / 100
        for tid, t in list(self.open.items()):
            if t["symbol"] != symbol:
                continue
            long = t["side"] == "LONG"
            t.setdefault("sl0", t["sl"])   # исходный стоп: от него считаем подходы
            self._maybe_breakeven(t, price)
            if (long and price <= t["sl"]) or (not long and price >= t["sl"]):
                closed.append(self._close(tid, t["sl"], "безубыток" if t.get("be") else "стоп", slip=True))
            elif (self.s.get("near_stop_exit") and not t.get("be")
                  and near_stop_step(t, t["entry"], t["sl0"], price,
                                     self.s.get("near_stop_zone"), self.s.get("near_stop_reset"))):
                closed.append(self._close(tid, price, "второй подход к стопу", slip=True))
            elif (long and price > t["tp"] * (1 + through)) or (not long and price < t["tp"] * (1 - through)):
                # тейк стоит лимитным ордером: без проскальзывания и с мейкерской комиссией
                closed.append(self._close(tid, t["tp"], "тейк", slip=False, maker=True))
        return closed

    def be_price(self, entry, side, entry_fee_pct=None):
        """Цена стопа, при которой сделка закроется примерно в ноль с учётом комиссий и проскальзывания."""
        sign = 1 if side == "LONG" else -1
        ef = self.s.get("fee_pct") if entry_fee_pct is None else entry_fee_pct
        cost = (ef + self.s.get("fee_pct") + self.s.get("slippage_pct")) / 100
        return entry * (1 + sign * cost)

    def _maybe_breakeven(self, t, price):
        if t.get("be") or not self.s.get("breakeven"):
            return
        span = t["tp"] - t["entry"]
        if span == 0:
            return
        if (price - t["entry"]) / span >= self.s.get("be_trigger"):
            t["sl"] = self.be_price(t["entry"], t["side"], t.get("entry_fee_pct"))
            t["be"] = True
            self.db.update_trade_sl(t["id"], t["sl"])

    swing_types = ("trend", "funding")
    hold_fn = None   # движок подставляет функцию: время удержания по типу сигнала

    def check_timeouts(self, prices):
        closed = []
        for tid, t in list(self.open.items()):
            limit = self.hold_fn(t.get("type")) if self.hold_fn else self.s.get("max_hold_min") * 60
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
