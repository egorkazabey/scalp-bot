"""SQLite: все сигналы и бумажные сделки, чтобы потом честно считать статистику."""
import json
import os
import sqlite3
import time

from config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, symbol TEXT, type TEXT, side TEXT,
    price REAL, sl REAL, tp REAL, details TEXT,
    p1 REAL, p5 REAL, p15 REAL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER, symbol TEXT, type TEXT, side TEXT,
    entry REAL, qty REAL, sl REAL, tp REAL,
    open_ts REAL, close_ts REAL, exit REAL,
    pnl REAL, fees REAL, reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_sig_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_tr_close ON trades(close_ts);
"""


class Storage:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # новые колонки для старых баз: обстановка сигнала и его виртуальный результат
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(signals)")}
        for col, typ in (("features", "TEXT"), ("result", "TEXT"), ("r_pct", "REAL"), ("closed_ts", "REAL"),
                         ("r_be", "REAL"), ("r_near", "REAL"), ("r_both", "REAL")):
            if col not in cols:
                self.db.execute(f"ALTER TABLE signals ADD COLUMN {col} {typ}")
        self.db.commit()

    # ---------- сигналы ----------
    def add_signal(self, sig):
        cur = self.db.execute(
            "INSERT INTO signals (ts, symbol, type, side, price, sl, tp, details, features) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (sig["ts"], sig["symbol"], sig["type"], sig["side"], sig["price"],
             sig["sl"], sig["tp"], json.dumps(sig.get("details", {}), ensure_ascii=False),
             json.dumps(sig.get("features", {}), ensure_ascii=False)),
        )
        self.db.commit()
        return cur.lastrowid

    def set_signal_outcome(self, sig_id, col, price):
        assert col in ("p1", "p5", "p15")
        self.db.execute(f"UPDATE signals SET {col}=? WHERE id=?", (price, sig_id))
        self.db.commit()

    def pending_outcomes(self):
        return self.db.execute(
            "SELECT id, ts, symbol, p1, p5, p15 FROM signals WHERE p15 IS NULL AND ts > ?",
            (time.time() - 3600,),
        ).fetchall()

    # ---------- виртуальный результат каждого сигнала ----------
    def set_signal_result(self, sig_id, result, r_pct, r_be=None, r_near=None, r_both=None):
        """Результат по вариантам выхода: r_pct просто стоп/тейк, r_be с безубытком,
        r_near с выходом на втором подходе к стопу, r_both с обоими."""
        self.db.execute("UPDATE signals SET result=?, r_pct=?, r_be=?, r_near=?, r_both=?, closed_ts=? "
                        "WHERE id=?", (result, r_pct, r_be, r_near, r_both, time.time(), sig_id))
        self.db.commit()

    def open_virtual(self, since):
        return self.db.execute(
            "SELECT id, ts, symbol, type, side, price, sl, tp FROM signals WHERE result IS NULL AND ts > ?",
            (since,)).fetchall()

    def expire_virtual(self, before):
        """Сигналы, которые не удалось довести до конца (например, бот был выключен)."""
        self.db.execute("UPDATE signals SET result='lost' WHERE result IS NULL AND ts <= ?", (before,))
        self.db.commit()

    def last_results(self, n, typ=None, symbol=None, since=0, col="r_pct"):
        assert col in ("r_pct", "r_be", "r_near", "r_both")
        col = f"COALESCE({col}, r_pct)"
        q = f"SELECT {col} FROM signals WHERE result IN ('tp','sl','time') AND ts >= ?"
        args = [since]
        if typ:
            q += " AND type=?"
            args.append(typ)
        if symbol:
            q += " AND symbol=?"
            args.append(symbol)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(n))
        return [r[0] for r in self.db.execute(q, args).fetchall()]

    def results(self, since=0):
        """Сигналы с результатом и обстановкой, для анализа."""
        rows = self.db.execute(
            "SELECT id, ts, symbol, type, side, result, r_pct, r_be, r_near, r_both, features FROM signals "
            "WHERE result IN ('tp','sl','time') AND ts >= ? ORDER BY id", (since,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["f"] = json.loads(r["features"] or "{}")
            except ValueError:
                d["f"] = {}
            out.append(d)
        return out

    def recent_signals(self, limit=10):
        return self.db.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def signal_stats(self, since=0):
        """Средний ход цены в сторону сигнала через 1/5/15 минут, по типам."""
        rows = self.db.execute(
            "SELECT type, side, price, p1, p5, p15 FROM signals WHERE ts >= ?", (since,)
        ).fetchall()
        out = {}
        for r in rows:
            d = out.setdefault(r["type"], {"n": 0, "m1": [], "m5": [], "m15": []})
            d["n"] += 1
            sign = 1 if r["side"] == "LONG" else -1
            for col, key in (("p1", "m1"), ("p5", "m5"), ("p15", "m15")):
                if r[col]:
                    d[key].append(sign * (r[col] / r["price"] - 1) * 100)
        return out

    # ---------- сделки ----------
    def open_trade(self, t):
        cur = self.db.execute(
            "INSERT INTO trades (signal_id, symbol, type, side, entry, qty, sl, tp, open_ts, fees) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (t["signal_id"], t["symbol"], t["type"], t["side"], t["entry"], t["qty"],
             t["sl"], t["tp"], t["open_ts"], t["fees"]),
        )
        self.db.commit()
        return cur.lastrowid

    def update_trade_sl(self, trade_id, sl):
        self.db.execute("UPDATE trades SET sl=? WHERE id=?", (sl, trade_id))
        self.db.commit()

    def close_trade(self, trade_id, exit_price, pnl, fees, reason):
        self.db.execute(
            "UPDATE trades SET close_ts=?, exit=?, pnl=?, fees=?, reason=? WHERE id=?",
            (time.time(), exit_price, pnl, fees, reason, trade_id),
        )
        self.db.commit()

    def open_trades(self):
        return self.db.execute("SELECT * FROM trades WHERE close_ts IS NULL").fetchall()

    def closed_trades(self, since=0, limit=None):
        q = "SELECT * FROM trades WHERE close_ts IS NOT NULL AND close_ts >= ? ORDER BY id DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        return self.db.execute(q, (since,)).fetchall()

    def realized_pnl(self, since=0):
        r = self.db.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE close_ts IS NOT NULL AND close_ts >= ?", (since,)
        ).fetchone()
        return r[0]

    def reset_paper(self):
        self.db.execute("DELETE FROM trades")
        self.db.commit()

    def reset_signals(self):
        self.db.execute("DELETE FROM signals")
        self.db.commit()
