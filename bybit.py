"""Данные Bybit (USDT-фьючерсы): стакан, лента сделок, тикеры, ликвидации.
Только публичные данные, API-ключи не нужны."""
import asyncio
import json
import logging
import time

import aiohttp

log = logging.getLogger("bybit")

WS_URL = "wss://stream.bybit.com/v5/public/linear"
REST_URL = "https://api.bybit.com"
SYMBOLS_PER_CONN = 10


def _ws_timeout():
    # если 60 сек нет ни одного сообщения, соединение считаем мёртвым и переподключаемся
    return aiohttp.ClientWSTimeout(ws_receive=60, ws_close=10)


class OrderBook:
    __slots__ = ("bids", "asks", "ts")

    def __init__(self):
        self.bids = {}
        self.asks = {}
        self.ts = 0

    def apply(self, msg_type, data, ts):
        if msg_type == "snapshot" or data.get("u") == 1:
            self.bids.clear()
            self.asks.clear()
        for p, s in data.get("b", []):
            p, s = float(p), float(s)
            if s == 0:
                self.bids.pop(p, None)
            else:
                self.bids[p] = s
        for p, s in data.get("a", []):
            p, s = float(p), float(s)
            if s == 0:
                self.asks.pop(p, None)
            else:
                self.asks[p] = s
        self.ts = ts

    def best(self):
        if not self.bids or not self.asks:
            return None, None
        return max(self.bids), min(self.asks)

    def mid(self):
        b, a = self.best()
        return (b + a) / 2 if b and a else None


class BybitFeed:
    """Держит WebSocket-соединения и вызывает колбэки движка."""

    def __init__(self, on_book, on_trades, on_ticker, on_liq, depth=200):
        self.on_book = on_book
        self.on_trades = on_trades
        self.on_ticker = on_ticker
        self.on_liq = on_liq
        self.depth = depth
        self.books = {}
        self.tickers = {}
        self.symbols = []
        self._tasks = []
        self._session = None
        self.connected = 0
        self.last_msg = 0

    async def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ---------- REST ----------
    async def fetch_tickers(self):
        s = await self.session()
        async with s.get(f"{REST_URL}/v5/market/tickers", params={"category": "linear"},
                         timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
        return data["result"]["list"]

    async def market(self, min_turnover=0):
        """Все USDT-фьючерсы с оборотом и изменением за 24ч. Заодно обновляет self.tickers,
        чтобы автопороги работали сразу, не дожидаясь WebSocket."""
        items = await self.fetch_tickers()
        out = []
        for t in items:
            sym = t["symbol"]
            if not sym.endswith("USDT"):
                continue
            try:
                turnover = float(t.get("turnover24h") or 0)
                change = float(t.get("price24hPcnt") or 0) * 100
            except ValueError:
                continue
            self.tickers.setdefault(sym, {}).update({k: v for k, v in t.items() if v not in ("", None)})
            if turnover >= min_turnover:
                out.append({"symbol": sym, "turnover": turnover, "change": change})
        return out

    async def top_symbols(self, n, min_turnover):
        items = sorted(await self.market(min_turnover), key=lambda t: -t["turnover"])
        return [t["symbol"] for t in items[:n]]

    async def fetch_klines(self, symbol, interval, limit=200):
        s = await self.session()
        params = {"category": "linear", "symbol": symbol, "interval": str(interval), "limit": limit}
        async with s.get(f"{REST_URL}/v5/market/kline", params=params,
                         timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
        return data["result"]["list"]

    async def valid_symbols(self):
        items = await self.fetch_tickers()
        return {t["symbol"] for t in items}

    # ---------- WebSocket ----------
    async def start(self, symbols, depth=None, extra=()):
        """symbols: монеты для сигналов. extra: монеты, по которым нужны только данные
        (BTC для BTC-фильтра, монеты с открытыми бумажными сделками)."""
        await self.stop()
        if depth:
            self.depth = depth
        self.symbols = list(dict.fromkeys(symbols))
        feed_syms = list(dict.fromkeys(self.symbols + [s for s in extra if s not in self.symbols]))
        self.books = {s: OrderBook() for s in feed_syms}
        for i in range(0, len(feed_syms), SYMBOLS_PER_CONN):
            chunk = feed_syms[i:i + SYMBOLS_PER_CONN]
            self._tasks.append(asyncio.create_task(self._run_conn(chunk)))
        log.info("Feed started: %s (depth %s)", ", ".join(self.symbols), self.depth)

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        self.connected = 0

    async def close(self):
        await self.stop()
        if self._session and not self._session.closed:
            await self._session.close()

    async def _run_conn(self, symbols):
        topics = []
        for s in symbols:
            topics += [f"orderbook.{self.depth}.{s}", f"publicTrade.{s}", f"tickers.{s}", f"allLiquidation.{s}"]
        backoff = 1
        while True:
            try:
                s = await self.session()
                async with s.ws_connect(WS_URL, heartbeat=None, max_msg_size=0,
                                        timeout=_ws_timeout()) as ws:
                    for i in range(0, len(topics), 10):
                        await ws.send_json({"op": "subscribe", "args": topics[i:i + 10]})
                    self.connected += 1
                    backoff = 1
                    ping = asyncio.create_task(self._pinger(ws))
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self.last_msg = time.time()
                                self._handle(json.loads(msg.data))
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                    finally:
                        ping.cancel()
                        self.connected -= 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("WS error (%s): %s", ",".join(symbols), e)
            # после обрыва стакан надо собрать заново
            for sym in symbols:
                self.books[sym] = OrderBook()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _pinger(self, ws):
        while True:
            await asyncio.sleep(20)
            await ws.send_json({"op": "ping"})

    def _handle(self, m):
        topic = m.get("topic")
        if not topic:
            if m.get("op") == "subscribe" and not m.get("success", True):
                log.error("Subscribe failed: %s", m.get("ret_msg"))
            return
        try:
            kind, _, rest = topic.partition(".")
            if kind == "orderbook":
                sym = rest.split(".", 1)[1]
                book = self.books.setdefault(sym, OrderBook())
                book.apply(m.get("type"), m["data"], m.get("ts", 0))
                self.on_book(sym, book)
            elif kind == "publicTrade":
                trades = [
                    (t["T"] / 1000, float(t["p"]), float(t["v"]), t["S"]) for t in m["data"]
                ]
                self.on_trades(rest, trades)
            elif kind == "tickers":
                d = self.tickers.setdefault(rest, {})
                d.update({k: v for k, v in m["data"].items() if v not in ("", None)})
                self.on_ticker(rest, d)
            elif kind == "allLiquidation":
                for x in m["data"]:
                    # S=Buy -> ликвидирован лонг, S=Sell -> ликвидирован шорт
                    self.on_liq(x["s"], x["T"] / 1000, float(x["p"]), float(x["v"]), x["S"])
        except Exception:
            log.exception("Bad message on %s", topic)
