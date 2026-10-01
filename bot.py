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

from analysis import analyze
from config import ENUMS, LABELS, PARAMS, SHORT_NAMES, SIGNAL_TYPES, TYPE_NAMES
from engine import Engine, fdur, fp, fusd
from paper import today_start

log = logging.getLogger("bot")

GROUPS = {
    "coins": ("🪙 Монеты", ["coin_mode", "auto_top_n", "movers_n", "auto_min_turnover", "max_coins",
                           "refresh_min", "ob_depth", "min_book_usd"]),
    "walls": ("🧱 Плотности", ["breakout_mode", "wall_mult", "wall_share_pct", "min_wall_usd", "wall_max_dist_pct",
                              "max_walls_side", "min_wall_age_sec", "min_trust", "approach_pct"]),
    "flow": ("📊 Объём и ликвидации", ["volume_mode", "vol_mult", "vol_min_move_pct", "vol_min_usd",
                                      "liq_mode", "liq_usd", "liq_turnover_pct", "auto_scale"]),
    "swing": ("📈 Тренд и фандинг", ["swing_atr_mult", "swing_hold_hours", "trend_cooldown_hours",
                                     "funding_extreme_pct", "funding_cooldown_hours"]),
    "market": ("📰 Новости и режим рынка", ["news_pause", "news_before_min", "news_after_min",
                                          "news_currencies", "news_impact", "regime_block"]),
    "sweep": ("🎣 Вынос стопов", ["sweep_min_pct", "sweep_max_pct", "sweep_reclaim_pct", "sweep_vol_mult",
                                 "sweep_window_sec"]),
    "size": ("💼 Размер сделки", ["paper_enabled", "start_balance", "size_mode", "risk_pct", "margin_pct",
                                 "max_leverage", "rr"]),
    "entry": ("🎯 Вход и выход", ["bounce_entry", "limit_offset_pct", "entry_wait_sec", "confirm_window_sec",
                                  "confirm_move_pct", "confirm_eat_pct", "breakeven", "be_trigger",
                                  "near_stop_exit", "near_stop_zone", "near_stop_reset"]),
    "filters": ("🔍 Фильтры", ["min_confluence", "strong_conf", "strong_size_mult", "delta_block",
                              "delta_block_lvl", "blocked_coins", "max_depth_usd", "min_coin_move_pct",
                              "btc_filter", "btc_filter_pct"]),
    "protect": ("🛡 Защита", ["min_sl_pct", "sl_buffer_pct", "default_sl_pct", "max_hold_min", "max_open",
                             "max_same_side", "stop_pause_min", "daily_loss_pct", "cooldown_sec"]),
    "fees": ("🧾 Комиссии", ["fee_pct", "maker_fee_pct", "slippage_pct", "tp_through_pct"]),
    "learn": ("🧠 Обучение", ["auto_pause", "pause_window", "pause_coin_window", "analyze_min"]),
}
FEED_KEYS = {"coin_mode", "auto_top_n", "movers_n", "auto_min_turnover", "max_coins", "ob_depth"}
MODES = ENUMS["coin_mode"]
MONEY_KEYS = {"max_depth_usd", "auto_min_turnover", "min_book_usd", "min_wall_usd", "vol_min_usd", "liq_usd", "start_balance"}

HELP = """📖 <b>Команды</b>

<b>Главное</b>
/menu · меню с кнопками
/status · состояние бота
/pause · /resume · пауза и продолжение

<b>Монеты</b>
/coins · список монет
/add SOL ETH · добавить свои
/remove SOL · убрать
/mix · свои + топ по обороту + рост и падение
/movers 5 · топ-5 роста и топ-5 падения за сутки
/auto 15 · топ-15 по обороту
/manual · только свои
/walls SOL · плотности по монете

<b>Результаты</b>
/stats · статистика
/trades · бумажные сделки
/signals · последние сигналы
/analyze · что работает, а что нет
/unpause · снять автопаузы
/news · важные новости недели

<b>Настройки</b>
/settings · все настройки кнопками
/set min_wall_usd 500k · изменить параметр
/set BTC min_wall_usd 3m · только для одной монеты
/unset BTC · убрать настройки монеты
/reset_settings · всё по умолчанию
/reset_paper · обнулить бумажный счёт

<i>Числа можно писать как 500k, 2.5m или 0,3</i>"""


def chart_url(sym):
    """График бессрочного фьючерса на Bybit. На телефоне с приложением Bybit обычно открывается в нём."""
    return f"https://www.bybit.com/trade/usdt/{sym}"


def chart_kb(sym):
    return InlineKeyboardMarkup([[B("📈 График", url=chart_url(sym))]])


def norm_symbol(s):
    s = s.strip().upper().replace("/", "").replace("-", "")
    if not s.endswith("USDT"):
        s += "USDT"
    return s


def coin(sym):
    return sym.replace("USDT", "")


def money(v):
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}".replace(",", " ")


def fval(key, v):
    if isinstance(v, bool):
        return "✅ вкл" if v else "⬜ выкл"
    if key in ENUMS:
        return ENUMS[key].get(v, str(v))
    if key in MONEY_KEYS:
        return fusd(v)
    if isinstance(v, float):
        return f"{v:g}"
    if v == "":
        return "нет"
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
            "analyze": self.cmd_analyze, "unpause": self.cmd_unpause, "news": self.cmd_news,
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
        self._sender_task = asyncio.create_task(self._sender())  # живёт всё время работы бота
        try:
            await app.bot.set_my_commands([
                ("menu", "Главное меню"), ("status", "Состояние"), ("coins", "Монеты"),
                ("walls", "Плотности по монете"), ("signals", "Последние сигналы"),
                ("stats", "Статистика"), ("trades", "Бумажные сделки"), ("analyze", "Анализ: что работает"),
                ("news", "Важные новости"),
                ("settings", "Настройки"),
                ("pause", "Пауза"), ("resume", "Продолжить"), ("help", "Все команды"),
            ])
        except TelegramError as e:
            log.warning("set_my_commands failed: %s", e)
        if self.s["owner_id"]:
            await self._send("🚀 <b>Бот запущен</b>\nМеню: /menu")

    async def _post_shutdown(self, app):
        await self.engine.stop()

    async def _sender(self):
        while True:
            item = await self.engine.outbox.get()
            text, sym = item if isinstance(item, tuple) else (item, None)
            await self._send(text, chart_kb(sym) if sym else None)
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
                await self._reply(update, f"⚠️ Что-то пошло не так: {html.escape(str(e))}")
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
            [B("🧾 Последние сигналы", callback_data="scr:last"), B("🧠 Анализ", callback_data="an:all")],
            [B("▶️ Продолжить" if paused else "⏸ Пауза", callback_data="toggle:pause")],
        ])

    BACK = [B("⬅️ Меню", callback_data="scr:main")]

    def text_status(self):
        e = self.engine
        f = e.feed
        prices = e.prices()
        up = time.time() - e.started_at
        last = time.time() - f.last_msg if f.last_msg else None
        bal = e.paper.balance()
        start = self.s.get("start_balance")
        state = "⏸ <b>Пауза</b>, новые сигналы не ищутся" if self.s["paused"] else "✅ <b>Бот работает</b>"
        data = "нет данных" if last is None else ("данные идут" if last < 10 else f"данные {last:.0f}с назад ⚠️")
        mode = ENUMS["coin_mode"].get(self.s.get("coin_mode"), "")
        coins = ", ".join(coin(x) for x in e.symbols[:12]) + (f" и ещё {len(e.symbols) - 12}"
                                                             if len(e.symbols) > 12 else "")
        unreal = e.paper.unrealized(prices)
        lines = [
            f"{state} · {fdur(up)}",
            f"Bybit: {f.connected} соед. · {data}",
            "",
            f"🪙 <b>Монеты</b> ({mode}): {len(e.symbols)}",
            coins or "список пуст",
            "",
            f"💰 <b>Баланс {money(bal)[1:] if bal >= 0 else money(bal)}</b>"
            + (f"  ({(bal / start - 1) * 100:+.1f}%)" if start else ""),
            f"Сегодня {money(e.paper.day_pnl())}",
            f"Открыто сделок: {len(e.paper.open)}" + (f" ({money(unreal)})" if e.paper.open else ""),
        ]
        reg = e.market_regime()
        now = time.time()
        nxt = e.calendar.upcoming(now, self.s.get("news_currencies"), self.s.get("news_impact"), 1)
        lines += ["", f"🌡 Рынок (BTC): <b>{reg or 'ещё считаю'}</b>"]
        if e.news_event(now):
            lines.append("📰 <b>Сейчас пауза на новостях</b>")
        elif nxt:
            lines.append(f"📰 Ближайшая новость: {html.escape(nxt[0][1])} "
                         f"{time.strftime('%d.%m %H:%M', time.localtime(nxt[0][0]))}")
        warn = []
        if self.s["auto_paused"]:
            names = [k.split(":", 1)[1] for k in self.s["auto_paused"]]
            warn.append("🧠 На автопаузе: " + ", ".join(TYPE_NAMES.get(n, coin(n)) for n in names))
        if e.paper.daily_stop_hit():
            warn.append("🛑 Дневной лимит убытка: новые сделки сегодня не открываются")
        if e.last_error:
            warn.append(f"⚠️ {html.escape(e.last_error)}")
        if warn:
            lines += [""] + warn
        return "\n".join(lines)

    def screen_coins(self):
        mode = self.s.get("coin_mode")
        e = self.engine
        text = [f"🪙 <b>Монеты</b> · {MODES.get(mode, mode)}"]
        if mode != "manual":
            text.append(f"<i>Оборот от {fusd(self.s.get('auto_min_turnover'))} в сутки, "
                        f"список обновляется каждые {self.s.get('refresh_min')} мин</i>")
        rows_t = []
        for x in e.symbols:
            ch = e.coin_change(x)
            ch_txt = f"{ch:+.1f}%" if ch is not None else ""
            tag = e.coin_tags.get(x, "")
            tag = "рост" if tag.startswith("📈") else "падение" if tag.startswith("📉") else tag
            if e.thin(x):
                tag += " ⚠️тонкий"
            rows_t.append(f"{coin(x)[:10]:<10} {ch_txt:>7}  {tag}")
        text.append("<pre>" + ("\n".join(rows_t) if rows_t else "список пуст") + "</pre>")
        ov = self.s["overrides"]
        if ov:
            text.append("<b>Свои настройки монет</b>")
            for x, d in ov.items():
                text.append(f"{coin(x)}: " + ", ".join(f"{LABELS.get(k, k)} {fval(k, v)}" for k, v in d.items()))
        rows = []
        if mode in ("manual", "mix"):
            text.append("<i>✖ убирает монету из твоего списка</i>")
            btns = [B(f"✖ {coin(x)}", callback_data=f"rm:{x}") for x in self.s["coins"]]
            rows += [btns[i:i + 4] for i in range(0, len(btns), 4)]
            rows.append([B("➕ Добавить монету", callback_data="ask:add")])
        rows.append([B(("● " if mode == m else "") + label, callback_data=f"mode:{m}")
                     for m, label in (("manual", "Свои"), ("auto", "Объём"), ("movers", "±24ч"),
                                      ("mix", "Всё"))])
        rows.append([B("🧱 Плотности монеты", callback_data="ask:walls"), B("🔄 Обновить", callback_data="scr:coins")])
        rows.append(self.BACK)
        return "\n".join(text), InlineKeyboardMarkup(rows)

    def screen_sigs(self):
        text = ("🔔 <b>Типы сигналов</b>\n\n"
                "✅ / ⬜ · искать сигнал или нет\n"
                "🔔 / 🔕 · присылать уведомление или только тихо считать статистику и вести бумажный счёт")
        rows = []
        for k, name in SIGNAL_TYPES.items():
            on = self.s["signals_on"][k]
            nt = self.s["notify"][k]
            rows.append([B(f"{'✅' if on else '⬜'} {name}", callback_data=f"sig:{k}"),
                         B("🔔" if nt else "🔕", callback_data=f"ntf:{k}")])
        rows.append(self.BACK)
        return text, InlineKeyboardMarkup(rows)

    def screen_settings(self):
        titles = [B(title, callback_data=f"grp:{g}") for g, (title, _) in GROUPS.items()]
        rows = [titles[i:i + 2] for i in range(0, len(titles), 2)]
        rows.append([B("♻️ Всё по умолчанию", callback_data="ask:reset_settings")])
        rows.append(self.BACK)
        return "⚙️ <b>Настройки</b>\nВыбери раздел.", InlineKeyboardMarkup(rows)

    def screen_group(self, g):
        title, keys = GROUPS[g]
        lines = [f"<b>{title}</b>", ""]
        rows = []
        for k in keys:
            v = self.s.get(k)
            lines.append(f"<b>{LABELS[k]}</b> · {fval(k, v)}\n<i>{PARAMS[k][2]}</i>\n")
            rows.append([B(f"{LABELS[k]}: {fval(k, v)}", callback_data=f"set:{k}")])
        lines.append("👇 Нажми на параметр: варианты переключаются сразу, число нужно отправить сообщением.")
        rows.append([B("⬅️ Настройки", callback_data="scr:settings")])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    def text_stats(self, period):
        since = {"today": today_start(), "7d": time.time() - 7 * 86400, "all": 0}[period]
        pname = {"today": "сегодня", "7d": "7 дней", "all": "всё время"}[period]
        tr = self.db.closed_trades(since=since)
        lines = [f"📈 <b>Статистика · {pname}</b>", "", "<b>Бумажные сделки</b>"]
        if tr:
            wins = [t["pnl"] for t in tr if t["pnl"] > 0]
            losses = [t["pnl"] for t in tr if t["pnl"] <= 0]
            total = sum(t["pnl"] for t in tr)
            fees = sum(t["fees"] for t in tr)
            pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")
            lines += [
                f"Сделок {len(tr)} · в плюс {len(wins) / len(tr) * 100:.0f}%",
                f"Итог <b>{money(total)}</b>  (комиссии ${fees:.2f})",
                f"Средний плюс {money(sum(wins) / len(wins)) if wins else '-'} · "
                f"средний минус {money(sum(losses) / len(losses)) if losses else '-'}",
                f"Профит-фактор <b>{'∞' if pf == float('inf') else f'{pf:.2f}'}</b>  "
                "<i>(больше 1 значит в плюсе)</i>",
            ]
            by = {}
            for t in tr:
                d = by.setdefault(t["type"], [0, 0, 0.0])
                d[0] += 1
                d[1] += t["pnl"] > 0
                d[2] += t["pnl"]
            table = [f"{'Тип':<11} {'Сд.':>3} {'Плюс':>5} {'Итог $':>8}"]
            for k, (n, w, p) in sorted(by.items(), key=lambda kv: -kv[1][0]):
                table.append(f"{SHORT_NAMES.get(k, k)[:11]:<11} {n:>3} {w / n * 100:>4.0f}% {p:>+8.2f}")
            lines.append("<pre>" + "\n".join(table) + "</pre>")
        else:
            lines.append("Закрытых сделок пока нет.")
        lines += ["", "<b>Сигналы: ход цены в их сторону</b>"]
        st = self.db.signal_stats(since=since)
        if st:
            def avg(a):
                return f"{sum(a) / len(a):+.2f}" if a else "   -"
            table = [f"{'Тип':<11} {'N':>3} {'1м':>6} {'5м':>6} {'15м':>6}"]
            for k, d in sorted(st.items(), key=lambda kv: -kv[1]["n"]):
                table.append(f"{SHORT_NAMES.get(k, k)[:11]:<11} {d['n']:>3} {avg(d['m1']):>6} "
                             f"{avg(d['m5']):>6} {avg(d['m15']):>6}")
            lines.append("<pre>" + "\n".join(table) + "</pre>")
            lines.append("<i>Средний ход в %. Вход и выход стоят ~0.11%: если ход меньше, сигнал не окупается.</i>")
        else:
            lines.append("Сигналов пока нет.")
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
        bal = e.paper.balance()
        start = self.s.get("start_balance")
        day = e.paper.day_pnl()
        lines = [
            "💼 <b>Бумажный счёт</b>",
            f"Баланс <b>${bal:,.2f}</b>".replace(",", " ")
            + (f"  ({(bal / start - 1) * 100:+.1f}% от старта)" if start else ""),
            f"Сегодня {'+' if day >= 0 else '-'}${abs(day):.2f}",
            "",
        ]
        rows = []
        if e.paper.open:
            lines.append(f"<b>Открыто ({len(e.paper.open)})</b>  <i>в PnL уже вычтена комиссия входа</i>")
            for t in e.paper.open.values():
                p = prices.get(t["symbol"])
                sign = 1 if t["side"] == "LONG" else -1
                u = sign * (p - t["entry"]) * t["qty"] - t["fees"] if p else 0
                to_sl = abs(p - t["sl"]) / p * 100 if p else 0
                to_tp = abs(t["tp"] - p) / p * 100 if p else 0
                icon = "🟢" if t["side"] == "LONG" else "🔴"
                lines += [
                    "",
                    f"{icon} <b>{t['side']} {t['symbol'].replace('USDT', '')}</b>  "
                    f"<b>{'+' if u >= 0 else '-'}${abs(u):.2f}</b>  · {fdur(time.time() - t['open_ts'])}",
                    f"<code>{fp(t['entry'])} → {fp(p)}</code>",
                    f"до стопа {to_sl:.2f}% · до тейка {to_tp:.2f}%",
                ]
                rows.append([B(f"✋ Закрыть {t['symbol'].replace('USDT', '')} #{t['id']}",
                               callback_data=f"close:{t['id']}"),
                             B("📈 График", url=chart_url(t["symbol"]))])
        else:
            lines.append("Открытых сделок нет.")
        closed = self.db.closed_trades(limit=10)
        if closed:
            wins = sum(1 for t in closed if t["pnl"] > 0)
            lines += ["", f"<b>Последние {len(closed)}</b>  ({wins} в плюс)"]
            table = []
            for t in closed:
                icon = "✅" if t["pnl"] > 0 else "❌"
                sym = t["symbol"].replace("USDT", "")[:8]
                table.append(f"{icon} {t['side']:<5} {sym:<8} {t['pnl']:+7.2f}  {t['reason']}")
            lines.append("<pre>" + "\n".join(table) + "</pre>")
        rows.append([B("🔄 Обновить", callback_data="scr:trades"), B("♻️ Сбросить счёт", callback_data="ask:reset_paper")])
        rows.append(self.BACK)
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    def text_last(self):
        rows = [r for r in self.db.recent_signals(60) if '"shadow"' not in (r["features"] or "")][:15]
        if not rows:
            return "🧾 Сигналов пока не было."
        table = [f"{'Время':<5} {'Монета':<8} {'Тип':<11} {'':1} {'5 мин':>6}"]
        for r in rows:
            res = "   ..."
            if r["p5"]:
                mv = (r["p5"] / r["price"] - 1) * 100 * (1 if r["side"] == "LONG" else -1)
                res = f"{mv:+.2f}%"
            elif r["result"] in ("lost",):
                res = "     -"
            t = time.strftime("%H:%M", time.localtime(r["ts"]))
            side = "▲" if r["side"] == "LONG" else "▼"
            table.append(f"{t:<5} {coin(r['symbol'])[:8]:<8} {SHORT_NAMES.get(r['type'], r['type'])[:11]:<11} "
                         f"{side} {res:>6}")
        return ("🧾 <b>Последние сигналы</b>\n<pre>" + "\n".join(table) + "</pre>\n"
                "<i>▲ лонг, ▼ шорт. Справа: куда пошла цена через 5 минут, в сторону сигнала</i>")

    # ---------- команды ----------
    async def cmd_start(self, update: Update, ctx):
        uid = update.effective_user.id
        if not self.s["owner_id"]:
            self.s["owner_id"] = uid
            log.info("Owner set to %s", uid)
            await update.message.reply_text(f"👋 Привет! Теперь ты владелец бота (id {uid}), остальным доступ закрыт.")
        if uid != self.s["owner_id"]:
            await update.message.reply_text("🔒 Это приватный бот.")
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
            msg.append("✅ Добавил: " + ", ".join(coin(x) for x in good))
        if bad:
            msg.append("❌ Нет на фьючерсах Bybit: " + ", ".join(coin(x) for x in bad))
        if not msg:
            msg.append("Эти монеты уже в списке.")
        if self.s.get("coin_mode") in ("auto", "movers"):
            msg.append("ℹ️ Сейчас бот сам выбирает монеты. Твой список работает в режимах «Свои» и «Всё»: /manual или /mix")
        await self._reply(update, "\n".join(msg))

    async def cmd_add(self, update, ctx):
        if not ctx.args:
            ctx.user_data["pending"] = ("add", None)
            await self._reply(update, "➕ Напиши монеты через пробел, например: <code>SOL PEPE ARB</code>")
            return
        await self._add_coins(update, ctx.args)

    async def cmd_remove(self, update, ctx):
        if not ctx.args:
            await self._reply(update, "Напиши так: <code>/remove PEPE</code>. Или нажми ✖ у монеты в /coins")
            return
        syms = [norm_symbol(n) for n in ctx.args]
        syms += ["1000" + s for s in syms]
        self.s["coins"] = [c for c in self.s["coins"] if c not in syms]
        if self.s.get("coin_mode") in ("manual", "mix"):
            await self.engine.restart_feed()
        await self._reply(update, "✅ Убрал. Твой список: " + (", ".join(coin(x) for x in self.s["coins"]) or "пуст"))

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
            await self._reply(update, "🧱 Какая монета? Напиши, например, <code>SOL</code>")
            return
        sym = norm_symbol(ctx.args[0])
        await self._reply(update, self.engine.walls_text(sym), chart_kb(sym) if sym in self.engine.feed.books else None)

    def analyze_texts(self, period):
        since = {"7d": time.time() - 7 * 86400, "1d": time.time() - 86400}.get(period, 0)
        title = {"7d": "7 дней", "1d": "сутки"}.get(period, "всё время")
        texts = analyze(self.db.results(since=since), self.s.get("analyze_min"), title,
                        exit_col=self.engine.exit_col())
        ap = self.s["auto_paused"]
        if ap:
            lines = ["\n\n⏸ <b>На автопаузе</b>\n<i>без сделок и уведомлений, сигналы проверяются виртуально</i>"]
            for k, v in ap.items():
                kind, name = k.split(":", 1)
                what = TYPE_NAMES.get(name, name) if kind == "type" else name
                lines.append(f"• <b>{what}</b> с {time.strftime('%H:%M %d.%m', time.localtime(v['since']))}\n    {v['why']}")
            texts[0] += "\n".join(lines)
        return texts

    def kb_analyze(self):
        rows = [[B("Сутки", callback_data="an:1d"), B("7 дней", callback_data="an:7d"),
                 B("Всё время", callback_data="an:all")]]
        if self.s["auto_paused"]:
            rows.append([B("▶️ Снять автопаузы", callback_data="do:unpause")])
        rows.append(self.BACK)
        return InlineKeyboardMarkup(rows)

    async def cmd_analyze(self, update, ctx):
        period = ctx.args[0] if ctx.args else "all"
        texts = self.analyze_texts(period)
        for i, t in enumerate(texts):
            await self._reply(update, t, self.kb_analyze() if i == len(texts) - 1 else None)

    async def cmd_unpause(self, update, ctx):
        now = time.time()
        for k in self.s["auto_paused"]:
            self.s["pause_reset"][k] = now
        self.s["auto_paused"] = {}
        await self._reply(update, "▶️ Все автопаузы сняты.")

    async def cmd_news(self, update, ctx):
        e = self.engine
        now = time.time()
        evs = e.calendar.upcoming(now, self.s.get("news_currencies"), self.s.get("news_impact"), 15)
        if not evs:
            await self._reply(update, "📰 Важных новостей на эту неделю не нашёл"
                              + ("" if e.calendar.updated else " (календарь ещё не загрузился)") + ".")
            return
        rows = []
        for ts, title, country, impact in evs:
            mark = "🔴" if impact == "High" else "🟠"
            lt = time.localtime(ts)
            wd = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][lt.tm_wday]
            rows.append(f"{mark} {wd} {time.strftime('%d.%m %H:%M', lt)} · {country} · "
                        f"{html.escape(title)}")
        pause = (f"Пауза: за {self.s.get('news_before_min')} мин до и {self.s.get('news_after_min')} мин после"
                 if self.s.get("news_pause") else "Пауза на новостях выключена")
        await self._reply(update, "📰 <b>Важные новости</b> (время пражское)\n\n" + "\n".join(rows)
                          + f"\n\n<i>{pause}</i>")

    async def cmd_signals(self, update, ctx):
        await self._reply(update, self.text_last())

    async def cmd_stats(self, update, ctx):
        await self._reply(update, self.text_stats("today"), self.kb_stats())

    async def cmd_trades(self, update, ctx):
        await self._reply(update, *self.screen_trades())

    async def cmd_settings(self, update, ctx):
        await self._reply(update, *self.screen_settings())

    async def _apply_set(self, update, key, value, symbol=None, quiet=False):
        if key not in PARAMS:
            await self._reply(update, f"❓ Нет такого параметра: <code>{html.escape(key)}</code>. "
                                      "Все параметры: /settings")
            return False
        if symbol and key in FEED_KEYS:
            await self._reply(update, "Этот параметр общий для всех монет, для одной монеты его не задать.")
            return False
        try:
            val = self.s.set(key, value, symbol=symbol)
        except ValueError as e:
            await self._reply(update, f"❌ Не подходит: {html.escape(str(e))}")
            return False
        if not quiet:
            where = f" для {coin(symbol)}" if symbol else ""
            await self._reply(update, f"✅ <b>{LABELS[key]}</b>{where}: {fval(key, val)}")
        if key in FEED_KEYS:
            await self.engine.restart_feed()
            if not quiet:
                await self._reply(update, f"🔄 Переподключился к Bybit, монет: {len(self.engine.symbols)}")
        return True

    async def cmd_set(self, update, ctx):
        a = ctx.args
        if len(a) == 2:
            await self._apply_set(update, a[0], a[1])
        elif len(a) == 3:
            await self._apply_set(update, a[1], a[2], symbol=norm_symbol(a[0]))
        else:
            await self._reply(update, "Напиши так: <code>/set параметр значение</code> или "
                                      "<code>/set BTC параметр значение</code>")

    async def cmd_unset(self, update, ctx):
        if not ctx.args:
            await self._reply(update, "Напиши так: <code>/unset BTC</code> или <code>/unset BTC min_wall_usd</code>")
            return
        sym = norm_symbol(ctx.args[0])
        self.s.clear_override(sym, ctx.args[1] if len(ctx.args) > 1 else None)
        await self._reply(update, f"✅ У {coin(sym)} снова общие настройки")

    async def cmd_pause(self, update, ctx):
        self.s["paused"] = True
        await self._reply(update, "⏸ <b>Пауза</b>\nНовые сигналы не ищутся. Открытые сделки доводятся до стопа или тейка.\n"
                                  "Продолжить: /resume")

    async def cmd_resume(self, update, ctx):
        self.s["paused"] = False
        await self._reply(update, "▶️ <b>Работаю</b>, ищу сигналы.")

    async def cmd_reset_paper(self, update, ctx):
        await self._reply(update, f"♻️ <b>Обнулить бумажный счёт?</b>\nБаланс вернётся к {fusd(self.s.get('start_balance'))}, "
                                  "история сделок удалится. Статистика сигналов останется.", InlineKeyboardMarkup(
            [[B("Да, сбросить", callback_data="do:reset_paper"), B("Отмена", callback_data="scr:main")]]))

    async def cmd_reset_settings(self, update, ctx):
        await self._reply(update, "♻️ <b>Вернуть все настройки по умолчанию?</b>\nСвои настройки монет тоже сбросятся.",
                          InlineKeyboardMarkup(
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
        elif kind == "an":
            texts = self.analyze_texts(arg)
            for i, t in enumerate(texts):
                last = i == len(texts) - 1
                await self._reply(update, t, self.kb_analyze() if last else None, edit=(i == 0 and len(texts) == 1))
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
            grp = next(g for g, (_, keys) in GROUPS.items() if arg in keys)
            cur = self.s.get(arg)
            if PARAMS[arg][1] is bool or arg in ENUMS:
                # переключаем сразу, без ввода
                if PARAMS[arg][1] is bool:
                    new = "off" if cur else "on"
                else:
                    opts = list(ENUMS[arg])
                    new = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
                await self._apply_set(update, arg, new, quiet=True)
                await self._reply(update, *self.screen_group(grp), edit=True)
            else:
                ctx.user_data["pending"] = ("set", arg)
                ex = ("500k или 2m" if arg in MONEY_KEYS else "SOXL,ALGO" if arg == "blocked_coins"
                      else "0.5" if isinstance(cur, float) else "30")
                await self._reply(update, f"✏️ <b>{LABELS[arg]}</b>\nСейчас: <b>{fval(arg, cur)}</b>\n"
                                          f"<i>{PARAMS[arg][2]}</i>\n\nОтправь новое значение, например <code>{ex}</code>")
        elif kind == "close":
            tid = int(arg)
            t = self.engine.paper.open.get(tid)
            p = self.engine.price(t["symbol"]) if t else None
            if t and p:
                closed = self.engine.paper.close_manual(tid, p)
                await self._reply(update, self.engine._fmt_close(closed), chart_kb(closed["symbol"]))
            await self._reply(update, *self.screen_trades(), edit=True)
        elif kind == "ask":
            if arg == "add":
                ctx.user_data["pending"] = ("add", None)
                await self._reply(update, "Напиши монеты через пробел, например: <code>SOL PEPE ARB</code>")
            elif arg == "walls":
                ctx.user_data["pending"] = ("walls", None)
                await self._reply(update, "🧱 Какая монета? Напиши, например, <code>SOL</code>")
            elif arg == "reset_paper":
                await self.cmd_reset_paper(update, ctx)
            elif arg == "reset_settings":
                await self.cmd_reset_settings(update, ctx)
        elif kind == "do":
            if arg == "reset_paper":
                self.engine.paper.reset()
                await self._reply(update, f"♻️ Счёт обнулён. Баланс {fusd(self.engine.paper.balance())}", edit=True)
            elif arg == "unpause":
                now = time.time()
                for k in self.s["auto_paused"]:
                    self.s["pause_reset"][k] = now
                self.s["auto_paused"] = {}
                await self._reply(update, "▶️ Все автопаузы сняты.", edit=True)
            elif arg == "reset_settings":
                self.s.reset_params()
                await self.engine.restart_feed()
                await self._reply(update, "♻️ Все настройки по умолчанию.", edit=True)

    async def on_text(self, update: Update, ctx):
        pending = ctx.user_data.pop("pending", None)
        text = update.message.text.strip()
        if not pending:
            await self._reply(update, "🤔 Не понял. Меню: /menu, все команды: /help")
            return
        kind, arg = pending
        if kind == "add":
            await self._add_coins(update, text.replace(",", " ").split())
        elif kind == "walls":
            sym = norm_symbol(text.split()[0])
            await self._reply(update, self.engine.walls_text(sym), chart_kb(sym) if sym in self.engine.feed.books else None)
        elif kind == "set":
            if await self._apply_set(update, arg, text):
                grp = next(g for g, (_, keys) in GROUPS.items() if arg in keys)
                await self._reply(update, *self.screen_group(grp))
            else:
                ctx.user_data["pending"] = ("set", arg)  # даём попробовать ещё раз

    def run(self):
        self.app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
