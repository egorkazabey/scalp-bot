"""Telegram-интерфейс: всё управление ботом через кнопки и команды."""
import asyncio
import html
import logging
import os
import time

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from config import PARAMS, SIGNAL_TYPES
from engine import Engine, fdur, fp, fusd
from paper import today_start

log = logging.getLogger("bot")

GROUPS = {
    "coins": ("🪙 Монеты и стакан", ["coin_mode", "auto_top_n", "movers_n", "auto_min_turnover", "max_coins",
                                    "refresh_min", "ob_depth", "min_book_usd"]),
    "scale": ("📐 Автоподстройка порогов", ["auto_scale", "wall_share_pct", "liq_turnover_pct"]),
    "walls": ("🧱 Плотности", ["min_wall_usd", "wall_mult", "wall_max_dist_pct", "max_walls_side", "min_wall_age_sec",
                              "min_trust", "approach_pct"]),
    "flow": ("📊 Объём и ликвидации", ["vol_mult", "vol_min_move_pct", "vol_min_usd", "liq_usd", "liq_mode"]),
    "risk": ("💼 Риск и бумажная торговля", ["paper_enabled", "start_balance", "size_mode", "risk_pct",
                                            "margin_pct", "max_leverage",
                                            "rr", "sl_buffer_pct", "default_sl_pct", "max_hold_min",
                                            "max_open", "daily_loss_pct", "fee_pct", "maker_fee_pct", "slippage_pct"]),
    "general": ("⚙️ Общее", ["cooldown_sec", "btc_filter", "btc_filter_pct"]),
}
FEED_KEYS = {"coin_mode", "auto_top_n", "movers_n", "auto_min_turnover", "max_coins", "ob_depth"}
MODES = {
    "manual": "свой список",
    "auto": "топ по обороту",
    "movers": "топ роста и падения за 24ч",
    "mix": "свой список + оборот + рост/падение",
}

HELP = """<b>Команды</b>
/menu - главное меню
/status - состояние бота
/coins - монеты · /add SOL ETH · /remove SOL
/movers 5 - топ-5 роста и топ-5 падения за 24ч
/mix - свой список + топ по обороту + рост/падение
/auto 15 - топ-15 монет по обороту · /manual - свой список
/walls SOL - текущие плотности по монете
/signals - последние сигналы
/stats - статистика · /trades - бумажные сделки
/settings - все настройки
/set min_wall_usd 500k - изменить параметр
/set BTC min_wall_usd 3m - параметр только для одной монеты
/unset BTC [параметр] - убрать настройку монеты
/pause · /resume - пауза сигналов
/reset_paper - сбросить бумажный счёт
/reset_settings - настройки по умолчанию

Числа можно писать как 500k, 2.5m, 0,3."""


def norm_symbol(s):
    s = s.strip().upper().replace("/", "").replace("-", "")
    if not s.endswith("USDT"):
        s += "USDT"
    return s


def fval(key, v):
    if isinstance(v, bool):
        return "вкл" if v else "выкл"
    if key.endswith("_usd") or key in ("auto_min_turnover",):
        return fusd(v)
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


class TgBot:
    def __init__(self, token, settings, storage):
        self.s = settings
        self.db = storage
        self.engine = Engine(settings, storage)
        env_owner = os.environ.get("OWNER_ID", "").strip()
        if env_owner:
            self.s["owner_id"] = int(env_owner)
        self.app = (Application.builder().token(token)
                    .post_init(self._post_init).post_shutdown(self._post_shutdown).build())
        a = self.app
        a.add_handler(CommandHandler("start", self.cmd_start))
        cmds = {
            "menu": self.cmd_menu, "help": self.cmd_help, "status": self.cmd_status,
            "coins": self.cmd_coins, "add": self.cmd_add, "remove": self.cmd_remove,
            "auto": self.cmd_auto, "manual": self.cmd_manual, "movers": self.cmd_movers, "mix": self.cmd_mix, "walls": self.cmd_walls,
            "signals": self.cmd_signals, "stats": self.cmd_stats, "trades": self.cmd_trades,
            "settings": self.cmd_settings, "set": self.cmd_set, "unset": self.cmd_unset,
            "pause": self.cmd_pause, "resume": self.cmd_resume,
            "reset_paper": self.cmd_reset_paper, "reset_settings": self.cmd_reset_settings,
        }
        for name, fn in cmds.items():
            a.add_handler(CommandHandler(name, self._guard(fn)))
        a.add_handler(CallbackQueryHandler(self._guard(self.on_button)))
        a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._guard(self.on_text)))

    # ---------- служебное ----------
    async def _post_init(self, app):
        await self.engine.start()
        app.create_task(self._sender())
        try:
            await app.bot.set_my_commands([
                ("menu", "Главное меню"), ("status", "Состояние"), ("coins", "Монеты"),
                ("walls", "Плотности по монете"), ("signals", "Последние сигналы"),
                ("stats", "Статистика"), ("trades", "Бумажные сделки"), ("settings", "Настройки"),
                ("pause", "Пауза"), ("resume", "Продолжить"), ("help", "Все команды"),
            ])
        except TelegramError as e:
            log.warning("set_my_commands failed: %s", e)
        if self.s["owner_id"]:
            await self._send("🚀 Бот запущен. /menu")

    async def _post_shutdown(self, app):
        await self.engine.stop()

    async def _sender(self):
        while True:
            text = await self.engine.outbox.get()
            await self._send(text)
            await asyncio.sleep(0.3)  # не упираться в лимиты Telegram при пачке сигналов

    async def _send(self, text, markup=None):
        if not self.s["owner_id"]:
            return
        for attempt in range(3):
            try:
                await self.app.bot.send_message(self.s["owner_id"], text, parse_mode=ParseMode.HTML,
                                                reply_markup=markup, disable_web_page_preview=True)
                return
            except RetryAfter as e:
                # Telegram просит подождать: ждём и отправляем снова, сообщение не теряется
                ra = e.retry_after
                await asyncio.sleep((ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)) + 1)
            except TelegramError as e:
                log.warning("send failed: %s", e)
                await asyncio.sleep(2)

    def _guard(self, fn):
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            uid = update.effective_user.id if update.effective_user else None
            if uid != self.s["owner_id"]:
                if update.callback_query:
                    await update.callback_query.answer("Нет доступа")
                return
            try:
                await fn(update, ctx)
            except Exception as e:
                log.exception("handler error")
                await self._reply(update, f"⚠️ Ошибка: {html.escape(str(e))}")
        return wrapper

    async def _reply(self, update, text, markup=None, edit=False):
        if edit and update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)
                return
            except TelegramError as e:
                if "not modified" in str(e).lower():
                    return
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                                 disable_web_page_preview=True)

    # ---------- экраны ----------
    def kb_main(self):
        paused = self.s["paused"]
        return InlineKeyboardMarkup([
            [B("📡 Статус", callback_data="scr:status"), B("🪙 Монеты", callback_data="scr:coins")],
            [B("🔔 Сигналы", callback_data="scr:sigs"), B("⚙️ Настройки", callback_data="scr:settings")],
            [B("📈 Статистика", callback_data="stats:today"), B("💼 Сделки", callback_data="scr:trades")],
            [B("🧾 Последние сигналы", callback_data="scr:last")],
            [B("▶️ Продолжить" if paused else "⏸ Пауза", callback_data="toggle:pause")],
        ])

    BACK = [B("⬅️ Меню", callback_data="scr:main")]

    def text_status(self):
        e = self.engine
        f = e.feed
        prices = e.prices()
        up = time.time() - e.started_at
        last = time.time() - f.last_msg if f.last_msg else None
        lines = [
            "⏸ <b>На паузе</b>" if self.s["paused"] else "✅ <b>Работает</b>",
            f"Аптайм: {fdur(up)} · соединений с Bybit: {f.connected}",
            f"Данные: {'нет' if last is None else f'{last:.0f}с назад'}",
            f"Монеты ({self.s.get('coin_mode')}): {', '.join(s.replace('USDT', '') for s in e.symbols)}",
            "",
            f"💰 Баланс: <b>{e.paper.balance():.2f}$</b> · за сегодня {e.paper.day_pnl():+.2f}$",
            f"Открыто сделок: {len(e.paper.open)} · нереализ. {e.paper.unrealized(prices):+.2f}$",
        ]
        if e.paper.daily_stop_hit():
            lines.append("🛑 Дневной лимит убытка достигнут, новые сделки не открываются")
        if e.last_error:
            lines.append(f"⚠️ {html.escape(e.last_error)}")
        return "\n".join(lines)

    def screen_coins(self):
        mode = self.s.get("coin_mode")
        e = self.engine
        text = [f"<b>Монеты</b> · режим <b>{mode}</b>: {MODES.get(mode, mode)}"]
        if mode != "manual":
            text.append(f"Только монеты с оборотом от {fusd(self.s.get('auto_min_turnover'))} за 24ч, "
                        f"список обновляется каждые {self.s.get('refresh_min')} мин.")
        text.append("")
        for s in e.symbols:
            ch = e.coin_change(s)
            ch_txt = f"{ch:+.1f}%" if ch is not None else ""
            tag = e.coin_tags.get(s, "")
            tag = "" if tag.startswith(("📈", "📉")) else f" · {tag}"
            if e.thin(s):
                tag += " · ⚠️ тонкий стакан"
            text.append(f"<code>{s.replace('USDT', ''):<10}</code> {ch_txt}{tag}")
        if not e.symbols:
            text.append("список пуст")
        ov = self.s["overrides"]
        if ov:
            text.append("\n<b>Настройки по монетам:</b>")
            for sym, d in ov.items():
                text.append(f"{sym}: " + ", ".join(f"{k}={fval(k, v)}" for k, v in d.items()))
        rows = []
        if mode in ("manual", "mix"):
            text.append("\n<i>Кнопки «✖» ниже убирают монету из твоего списка.</i>")
            btns = [B(f"✖ {s.replace('USDT', '')}", callback_data=f"rm:{s}") for s in self.s["coins"]]
            rows += [btns[i:i + 3] for i in range(0, len(btns), 3)]
            rows.append([B("➕ Добавить свою монету", callback_data="ask:add")])
        rows.append([B(("● " if mode == m else "") + label, callback_data=f"mode:{m}")
                     for m, label in (("manual", "Свои"), ("auto", "Объём"), ("movers", "±24ч"),
                                      ("mix", "Всё"))])
        rows.append([B("🔄 Обновить", callback_data="scr:coins")])
        rows.append([B("🧱 Плотности по монете", callback_data="ask:walls")])
        rows.append(self.BACK)
        return "\n".join(text), InlineKeyboardMarkup(rows)

    def screen_sigs(self):
        text = ("<b>Типы сигналов</b>\n"
                "Левая кнопка: искать сигнал или нет.\n"
                "🔔/🔕: присылать уведомление или только тихо записывать в статистику и бумажный счёт.")
        rows = []
        for k, name in SIGNAL_TYPES.items():
            on = self.s["signals_on"][k]
            nt = self.s["notify"][k]
            rows.append([B(f"{'✅' if on else '⬜'} {name}", callback_data=f"sig:{k}"),
                         B("🔔" if nt else "🔕", callback_data=f"ntf:{k}")])
        rows.append(self.BACK)
        return text, InlineKeyboardMarkup(rows)

    def screen_settings(self):
        rows = [[B(title, callback_data=f"grp:{g}")] for g, (title, _) in GROUPS.items()]
        rows.append([B("♻️ Сбросить по умолчанию", callback_data="ask:reset_settings")])
        rows.append(self.BACK)
        return "<b>Настройки</b>\nВыбери раздел. Нажми на параметр и отправь новое значение.", InlineKeyboardMarkup(rows)

    def screen_group(self, g):
        title, keys = GROUPS[g]
        lines = [f"<b>{title}</b>"]
        rows = []
        for k in keys:
            v = self.s.get(k)
            lines.append(f"• <code>{k}</code> = <b>{fval(k, v)}</b>\n   {PARAMS[k][2]}")
            rows.append([B(f"{k}: {fval(k, v)}", callback_data=f"set:{k}")])
        rows.append([B("⬅️ Настройки", callback_data="scr:settings")])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    def text_stats(self, period):
        since = {"today": today_start(), "7d": time.time() - 7 * 86400, "all": 0}[period]
        pname = {"today": "сегодня", "7d": "7 дней", "all": "всё время"}[period]
        tr = self.db.closed_trades(since=since)
        lines = [f"<b>📈 Статистика за {pname}</b>", "", "<b>Бумажные сделки</b>"]
        if tr:
            wins = [t["pnl"] for t in tr if t["pnl"] > 0]
            losses = [t["pnl"] for t in tr if t["pnl"] <= 0]
            total = sum(t["pnl"] for t in tr)
            fees = sum(t["fees"] for t in tr)
            pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")
            lines += [
                f"Сделок: {len(tr)} · винрейт {len(wins) / len(tr) * 100:.0f}%",
                f"PnL: <b>{total:+.2f}$</b> (из них комиссии {fees:.2f}$)",
                f"Средняя прибыль {sum(wins) / len(wins) if wins else 0:+.2f}$ · средний убыток "
                f"{sum(losses) / len(losses) if losses else 0:+.2f}$",
                f"Профит-фактор: {'∞' if pf == float('inf') else f'{pf:.2f}'}",
            ]
            by = {}
            for t in tr:
                d = by.setdefault(t["type"], [0, 0, 0.0])
                d[0] += 1
                d[1] += t["pnl"] > 0
                d[2] += t["pnl"]
            for k, (n, w, p) in by.items():
                lines.append(f"  {SIGNAL_TYPES.get(k, k)}: {n} сд., {w / n * 100:.0f}% плюс, {p:+.2f}$")
        else:
            lines.append("Закрытых сделок пока нет.")
        lines += ["", "<b>Сигналы: средний ход цены в сторону сигнала</b>"]
        st = self.db.signal_stats(since=since)
        if st:
            for k, d in st.items():
                def avg(a):
                    return f"{sum(a) / len(a):+.2f}%" if a else "-"
                pos5 = f"{sum(1 for x in d['m5'] if x > 0) / len(d['m5']) * 100:.0f}%" if d["m5"] else "-"
                lines.append(f"{SIGNAL_TYPES.get(k, k)} ({d['n']}): 1м {avg(d['m1'])} · 5м {avg(d['m5'])} · "
                             f"15м {avg(d['m15'])} · в плюс через 5м: {pos5}")
        else:
            lines.append("Сигналов пока нет.")
        lines.append("\n<i>Комиссия за круг ~0.11%: если средний ход меньше, сигнал не окупается.</i>")
        return "\n".join(lines)

    def kb_stats(self):
        return InlineKeyboardMarkup([
            [B("Сегодня", callback_data="stats:today"), B("7 дней", callback_data="stats:7d"),
             B("Всё время", callback_data="stats:all")],
            self.BACK,
        ])

    def screen_trades(self):
        e = self.engine
        prices = e.prices()
        lines = [f"<b>💼 Бумажный счёт</b> · баланс {e.paper.balance():.2f}$", ""]
        rows = []
        if e.paper.open:
            lines.append("<b>Открытые:</b>")
            lines.append("<i>Минус сразу после входа это комиссия за вход, она списывается сразу.</i>")
            for t in e.paper.open.values():
                p = prices.get(t["symbol"])
                sign = 1 if t["side"] == "LONG" else -1
                u = sign * (p - t["entry"]) * t["qty"] - t["fees"] if p else 0
                to_sl = abs(p - t["sl"]) / p * 100 if p else 0
                to_tp = abs(t["tp"] - p) / p * 100 if p else 0
                lines.append(f"#{t['id']} {t['side']} {t['symbol']} · {fdur(time.time() - t['open_ts'])}\n"
                             f"   вход {fp(t['entry'])} → сейчас {fp(p)} · <b>{u:+.2f}$</b> "
                             f"(вкл. комиссию входа {t['fees']:.2f}$)\n"
                             f"   стоп {fp(t['sl'])} (ещё {to_sl:.2f}%) · тейк {fp(t['tp'])} (ещё {to_tp:.2f}%)")
                rows.append([B(f"Закрыть #{t['id']} {t['symbol']}", callback_data=f"close:{t['id']}")])
        else:
            lines.append("Открытых сделок нет.")
        closed = self.db.closed_trades(limit=10)
        if closed:
            lines += ["", "<b>Последние закрытые:</b>"]
            for t in closed:
                icon = "✅" if t["pnl"] > 0 else "❌"
                lines.append(f"{icon} #{t['id']} {t['side']} {t['symbol']} {t['pnl']:+.2f}$ ({t['reason']})")
        rows.append([B("🔄 Обновить", callback_data="scr:trades"), B("♻️ Сбросить счёт", callback_data="ask:reset_paper")])
        rows.append(self.BACK)
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    def text_last(self):
        rows = self.db.recent_signals(15)
        if not rows:
            return "Сигналов пока не было."
        lines = ["<b>🧾 Последние сигналы</b>"]
        for r in rows:
            icon = "🟢" if r["side"] == "LONG" else "🔴"
            res = ""
            if r["p5"]:
                mv = (r["p5"] / r["price"] - 1) * 100 * (1 if r["side"] == "LONG" else -1)
                res = f" · 5м: {mv:+.2f}%"
            t = time.strftime("%d.%m %H:%M", time.localtime(r["ts"]))
            lines.append(f"{icon} {t} {r['symbol']} {SIGNAL_TYPES.get(r['type'], r['type'])} @ {fp(r['price'])}{res}")
        return "\n".join(lines)

    # ---------- команды ----------
    async def cmd_start(self, update: Update, ctx):
        uid = update.effective_user.id
        if not self.s["owner_id"]:
            self.s["owner_id"] = uid
            log.info("Owner set to %s", uid)
            await update.message.reply_text(f"👋 Ты владелец бота (id {uid}). Остальным доступ закрыт.")
        if uid != self.s["owner_id"]:
            await update.message.reply_text("Это приватный бот.")
            return
        await self.cmd_menu(update, ctx)

    async def cmd_menu(self, update, ctx):
        await self._reply(update, self.text_status(), self.kb_main())

    async def cmd_help(self, update, ctx):
        await self._reply(update, HELP)

    async def cmd_status(self, update, ctx):
        await self._reply(update, self.text_status(), self.kb_main())

    async def cmd_coins(self, update, ctx):
        await self._reply(update, *self.screen_coins())

    async def _add_coins(self, update, names):
        syms = [norm_symbol(n) for n in names if n.strip()]
        try:
            valid = await self.engine.feed.valid_symbols()
        except Exception:
            valid = None
        if valid is not None:
            # у мемкоинов на Bybit часто префикс 1000 (PEPE -> 1000PEPEUSDT)
            syms = [s if s in valid or "1000" + s not in valid else "1000" + s for s in syms]
        syms = list(dict.fromkeys(syms))
        bad = [s for s in syms if valid is not None and s not in valid]
        good = [s for s in syms if s not in bad and s not in self.s["coins"]]
        if good:
            self.s["coins"] = self.s["coins"] + good
            if self.s.get("coin_mode") in ("manual", "mix"):
                await self.engine.restart_feed()
        msg = []
        if good:
            msg.append("✅ Добавил: " + ", ".join(good))
        if bad:
            msg.append("❌ Нет на Bybit фьючерсах: " + ", ".join(bad))
        if not msg:
            msg.append("Уже в списке.")
        if self.s.get("coin_mode") in ("auto", "movers"):
            msg.append("ℹ️ Сейчас авто-режим, свои монеты заработают в /manual или /mix")
        await self._reply(update, "\n".join(msg))

    async def cmd_add(self, update, ctx):
        if not ctx.args:
            ctx.user_data["pending"] = ("add", None)
            await self._reply(update, "Напиши монеты через пробел, например: <code>SOL PEPE ARB</code>")
            return
        await self._add_coins(update, ctx.args)

    async def cmd_remove(self, update, ctx):
        if not ctx.args:
            await self._reply(update, "Формат: <code>/remove PEPE</code> или кнопки в /coins")
            return
        syms = [norm_symbol(n) for n in ctx.args]
        syms += ["1000" + s for s in syms]
        self.s["coins"] = [c for c in self.s["coins"] if c not in syms]
        if self.s.get("coin_mode") in ("manual", "mix"):
            await self.engine.restart_feed()
        await self._reply(update, "Готово. Сейчас: " + ", ".join(self.s["coins"]))

    async def _set_mode(self, update, mode, n_key=None, n=None):
        if n_key and n:
            self.s.set(n_key, n)
        self.s.set("coin_mode", mode)
        await self.engine.restart_feed()
        await self._reply(update, *self.screen_coins())

    async def cmd_auto(self, update, ctx):
        await self._set_mode(update, "auto", "auto_top_n", ctx.args[0] if ctx.args else None)

    async def cmd_movers(self, update, ctx):
        await self._set_mode(update, "movers", "movers_n", ctx.args[0] if ctx.args else None)

    async def cmd_mix(self, update, ctx):
        await self._set_mode(update, "mix")

    async def cmd_manual(self, update, ctx):
        await self._set_mode(update, "manual")

    async def cmd_walls(self, update, ctx):
        if not ctx.args:
            ctx.user_data["pending"] = ("walls", None)
            await self._reply(update, "Какая монета? Например <code>SOL</code>")
            return
        await self._reply(update, self.engine.walls_text(norm_symbol(ctx.args[0])))

    async def cmd_signals(self, update, ctx):
        await self._reply(update, self.text_last())

    async def cmd_stats(self, update, ctx):
        await self._reply(update, self.text_stats("today"), self.kb_stats())

    async def cmd_trades(self, update, ctx):
        await self._reply(update, *self.screen_trades())

    async def cmd_settings(self, update, ctx):
        await self._reply(update, *self.screen_settings())

    async def _apply_set(self, update, key, value, symbol=None):
        if key not in PARAMS:
            await self._reply(update, f"Нет параметра <code>{html.escape(key)}</code>. Список: /settings")
            return
        if symbol and key in FEED_KEYS:
            await self._reply(update, "Этот параметр общий, для отдельной монеты его задать нельзя.")
            return
        try:
            val = self.s.set(key, value, symbol=symbol)
        except ValueError as e:
            await self._reply(update, f"❌ Неверное значение: {html.escape(str(e))}")
            return
        where = f" для {symbol}" if symbol else ""
        await self._reply(update, f"✅ <code>{key}</code>{where} = <b>{fval(key, val)}</b>")
        if key in FEED_KEYS:
            await self.engine.restart_feed()
            await self._reply(update, "🔄 Перезапустил поток данных: " + ", ".join(self.engine.symbols))

    async def cmd_set(self, update, ctx):
        a = ctx.args
        if len(a) == 2:
            await self._apply_set(update, a[0], a[1])
        elif len(a) == 3:
            await self._apply_set(update, a[1], a[2], symbol=norm_symbol(a[0]))
        else:
            await self._reply(update, "Формат: <code>/set параметр значение</code> или "
                                      "<code>/set BTC параметр значение</code>")

    async def cmd_unset(self, update, ctx):
        if not ctx.args:
            await self._reply(update, "Формат: <code>/unset BTC</code> или <code>/unset BTC min_wall_usd</code>")
            return
        sym = norm_symbol(ctx.args[0])
        self.s.clear_override(sym, ctx.args[1] if len(ctx.args) > 1 else None)
        await self._reply(update, f"✅ Убрал настройки {sym}")

    async def cmd_pause(self, update, ctx):
        self.s["paused"] = True
        await self._reply(update, "⏸ Пауза. Новые сигналы не ищутся, открытые бумажные сделки ведутся дальше.")

    async def cmd_resume(self, update, ctx):
        self.s["paused"] = False
        await self._reply(update, "▶️ Работаю.")

    async def cmd_reset_paper(self, update, ctx):
        await self._reply(update, "Сбросить бумажный счёт и историю сделок?", InlineKeyboardMarkup(
            [[B("Да, сбросить", callback_data="do:reset_paper"), B("Отмена", callback_data="scr:main")]]))

    async def cmd_reset_settings(self, update, ctx):
        await self._reply(update, "Вернуть все настройки по умолчанию?", InlineKeyboardMarkup(
            [[B("Да", callback_data="do:reset_settings"), B("Отмена", callback_data="scr:main")]]))

    # ---------- кнопки ----------
    async def on_button(self, update: Update, ctx):
        q = update.callback_query
        await q.answer()
        kind, _, arg = q.data.partition(":")
        if kind == "scr":
            if arg == "main":
                await self._reply(update, self.text_status(), self.kb_main(), edit=True)
            elif arg == "status":
                await self._reply(update, self.text_status(), self.kb_main(), edit=True)
            elif arg == "coins":
                await self._reply(update, *self.screen_coins(), edit=True)
            elif arg == "sigs":
                await self._reply(update, *self.screen_sigs(), edit=True)
            elif arg == "settings":
                await self._reply(update, *self.screen_settings(), edit=True)
            elif arg == "trades":
                await self._reply(update, *self.screen_trades(), edit=True)
            elif arg == "last":
                await self._reply(update, self.text_last(), InlineKeyboardMarkup([self.BACK]), edit=True)
        elif kind == "stats":
            await self._reply(update, self.text_stats(arg), self.kb_stats(), edit=True)
        elif kind == "grp":
            await self._reply(update, *self.screen_group(arg), edit=True)
        elif kind == "toggle":
            if arg == "pause":
                self.s["paused"] = not self.s["paused"]
                await self._reply(update, self.text_status(), self.kb_main(), edit=True)
        elif kind == "mode":
            self.s.set("coin_mode", arg)
            await self.engine.restart_feed()
            await self._reply(update, *self.screen_coins(), edit=True)
        elif kind == "sig":
            self.s["signals_on"][arg] = not self.s["signals_on"][arg]
            self.s.save()
            await self._reply(update, *self.screen_sigs(), edit=True)
        elif kind == "ntf":
            self.s["notify"][arg] = not self.s["notify"][arg]
            self.s.save()
            await self._reply(update, *self.screen_sigs(), edit=True)
        elif kind == "rm":
            self.s["coins"] = [c for c in self.s["coins"] if c != arg]
            await self.engine.restart_feed()
            await self._reply(update, *self.screen_coins(), edit=True)
        elif kind == "set":
            if PARAMS[arg][1] is bool:
                await self._apply_set(update, arg, "off" if self.s.get(arg) else "on")
                grp = next(g for g, (_, keys) in GROUPS.items() if arg in keys)
                await self._reply(update, *self.screen_group(grp))
            else:
                ctx.user_data["pending"] = ("set", arg)
                await self._reply(update, f"<code>{arg}</code> сейчас <b>{fval(arg, self.s.get(arg))}</b>\n"
                                          f"{PARAMS[arg][2]}\n\nОтправь новое значение:")
        elif kind == "close":
            tid = int(arg)
            t = self.engine.paper.open.get(tid)
            p = self.engine.price(t["symbol"]) if t else None
            if t and p:
                closed = self.engine.paper.close_manual(tid, p)
                await self._reply(update, self.engine._fmt_close(closed))
            await self._reply(update, *self.screen_trades(), edit=True)
        elif kind == "ask":
            if arg == "add":
                ctx.user_data["pending"] = ("add", None)
                await self._reply(update, "Напиши монеты через пробел, например: <code>SOL PEPE ARB</code>")
            elif arg == "walls":
                ctx.user_data["pending"] = ("walls", None)
                await self._reply(update, "Какая монета? Например <code>SOL</code>")
            elif arg == "reset_paper":
                await self.cmd_reset_paper(update, ctx)
            elif arg == "reset_settings":
                await self.cmd_reset_settings(update, ctx)
        elif kind == "do":
            if arg == "reset_paper":
                self.engine.paper.reset()
                await self._reply(update, f"♻️ Счёт сброшен. Баланс {self.engine.paper.balance():.2f}$", edit=True)
            elif arg == "reset_settings":
                self.s.reset_params()
                await self.engine.restart_feed()
                await self._reply(update, "♻️ Настройки сброшены.", edit=True)

    async def on_text(self, update: Update, ctx):
        pending = ctx.user_data.pop("pending", None)
        text = update.message.text.strip()
        if not pending:
            await self._reply(update, "Не понял. Открой /menu или /help")
            return
        kind, arg = pending
        if kind == "add":
            await self._add_coins(update, text.replace(",", " ").split())
        elif kind == "walls":
            await self._reply(update, self.engine.walls_text(norm_symbol(text.split()[0])))
        elif kind == "set":
            await self._apply_set(update, arg, text)

    def run(self):
        self.app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
