"""Разбор результатов сигналов по обстановке: что работает, а что нет.

Результат каждого сигнала (r_pct) это виртуальная сделка до стопа, тейка или таймаута,
в % от позиции, уже с комиссиями. Вывод по группе делаем, только если в ней достаточно
сигналов и отличие от среднего больше двух стандартных ошибок (грубая проверка,
что это не случайность)."""
import html
import math

from config import SHORT_NAMES

HOURS = [(0, 6, "ночь 0-6"), (6, 12, "утро 6-12"), (12, 18, "день 12-18"), (18, 24, "вечер 18-24")]


def _bucket(v, edges, fmt):
    """edges: [(верхняя граница, подпись)], последняя граница None = бесконечность."""
    if v is None:
        return None
    for hi, label in edges:
        if hi is None or v < hi:
            return label
    return None


def _usd(v):
    return f"${v / 1e6:g}M" if v >= 1e6 else f"${v / 1e3:g}K"


def _trend(r, key, flat):
    """Тренд относительно направления сделки: по тренду, против, боковик."""
    v = r["f"].get(key)
    if v is None:
        return None
    if abs(v) < flat:
        return "боковик"
    return "по тренду" if (v > 0) == (r["side"] == "LONG") else "против тренда"


def _levels(r):
    """Где цена относительно максимума и минимума суток, с учётом направления сделки."""
    hi, lo = r["f"].get("dist_hi"), r["f"].get("dist_lo")
    if hi is None or lo is None:
        return None
    near = 0.7
    long = r["side"] == "LONG"
    at_support = (lo <= near) if long else (hi <= near)   # лонг от минимума, шорт от максимума
    at_wall = (hi <= near) if long else (lo <= near)      # лонг в максимум, шорт в минимум
    if at_support:
        return "от уровня суток"
    if at_wall:
        return "в уровень суток"
    return "середина диапазона"


GROUPS = [
    ("Тип сигнала", lambda r: SHORT_NAMES.get(r["type"], r["type"])),
    ("Направление", lambda r: r["side"]),
    ("Время (Прага)", lambda r: next((l for a, b, l in HOURS if a <= r["f"].get("hour", -1) < b), None)),
    ("BTC за 5 мин", lambda r: {"with": "по движению BTC", "against": "против BTC",
                                "flat": "BTC стоит"}.get(r["f"].get("btc_dir"))),
    ("Доверие плотности", lambda r: _bucket(r["f"].get("trust"),
                                            [(55, "<55"), (65, "55-65"), (80, "65-80"), (None, "80+")], None)),
    ("Плотность к соседям", lambda r: _bucket(r["f"].get("ratio"),
                                              [(8, "<x8"), (20, "x8-20"), (50, "x20-50"), (None, "x50+")], None)),
    ("Всплеск объёма", lambda r: _bucket(r["f"].get("vol_x"),
                                         [(6, "<x6"), (10, "x6-10"), (20, "x10-20"), (None, "x20+")], None)),
    ("Монета за 24ч", lambda r: _bucket(abs(r["f"]["chg24"]) if r["f"].get("chg24") is not None else None,
                                        [(3, "±0-3%"), (8, "±3-8%"), (15, "±8-15%"), (None, "±15%+")], None)),
    ("Размах цены за 5 мин", lambda r: _bucket(r["f"].get("vola5"),
                                               [(0.3, "<0.3%"), (0.7, "0.3-0.7%"), (1.5, "0.7-1.5%"),
                                                (None, "1.5%+")], None)),
    ("Цена за 15 мин до сигнала", lambda r: _bucket(
        (r["f"]["move15"] if r["side"] == "LONG" else -r["f"]["move15"]) if r["f"].get("move15") is not None else None,
        [(-1, "сильно против входа"), (-0.2, "против входа"), (0.2, "на месте"), (1, "в сторону входа"),
         (None, "сильно в сторону входа")], None)),
    ("Глубина стакана ±1%", lambda r: _bucket(r["f"].get("depth"),
                                              [(5e5, "<$500K"), (2e6, "$0.5-2M"), (1e7, "$2-10M"),
                                               (None, "$10M+")], None)),
    ("Тренд за 4ч", lambda r: _trend(r, "tr4h", 1.0)),
    ("Тренд за 1ч", lambda r: _trend(r, "tr1h", 0.4)),
    ("Цена и EMA200 (1ч)", lambda r: None if r["f"].get("ema200_1h") is None else
        ("по тренду" if (r["f"]["ema200_1h"] == "above") == (r["side"] == "LONG") else "против тренда")),
    ("Цена и EMA50 (1ч)", lambda r: None if r["f"].get("ema50_1h") is None else
        ("по тренду" if (r["f"]["ema50_1h"] == "above") == (r["side"] == "LONG") else "против тренда")),
    ("RSI 15м по сделке", lambda r: _bucket(r["f"].get("rsi_side"),
                                           [(30, "<30 сильно против"), (45, "30-45"), (55, "45-55"), (70, "55-70"),
                                            (None, "70+ сильно по")], None)),
    ("Уровни суток", lambda r: _levels(r)),
    ("Открытый интерес за 5 мин", lambda r: r["f"].get("oi_regime") or (
        None if r["f"].get("oi5") is None else "почти не менялся")),
    ("Фандинг", lambda r: r["f"].get("crowd")),
    ("Дельта за минуту", lambda r: _bucket(r["f"].get("delta1"),
                                         [(-0.3, "давят против сделки"), (0.3, "равновесие"),
                                          (None, "давят за сделку")], None)),
    ("Поглощение", lambda r: r["f"].get("absorb") or ("нет" if "delta1" in r["f"] else None)),
    ("Айсберг рядом", lambda r: r["f"].get("iceberg") or ("нет" if "delta1" in r["f"] else None)),
    ("Режим рынка (BTC)", lambda r: r["f"].get("regime")),
    ("Новости", lambda r: "рядом с важной новостью" if r["f"].get("news") else
        ("обычное время" if "regime" in r["f"] else None)),
    ("Совпало факторов", lambda r: None if r["f"].get("conf") is None else
        {0: "0", 1: "1", 2: "2"}.get(r["f"]["conf"], "3+")),
    ("Монета", lambda r: r["symbol"].replace("USDT", "")),
]


def stats(rs):
    n = len(rs)
    if not n:
        return None
    mean = sum(rs) / n
    var = sum((x - mean) ** 2 for x in rs) / (n - 1) if n > 1 else 0
    pos = sum(x for x in rs if x > 0)
    neg = -sum(x for x in rs if x < 0)
    return {"n": n, "mean": mean, "se": math.sqrt(var / n) if n > 1 else 0,
            "win": sum(1 for x in rs if x > 0) / n * 100, "pf": pos / neg if neg else float("inf")}


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


EXIT_NAMES = {"r_pct": "просто стоп и тейк", "r_be": "с безубытком",
              "r_near": "выход на втором подходе к стопу", "r_both": "безубыток + второй подход"}


def analyze(rows, min_n=15, title="всё время", exit_col="r_pct"):
    """Возвращает список сообщений (Telegram ограничивает длину).
    Тип сигнала сравниваем по всем вариантам (включая виртуальные способы входа),
    остальные группы только по сигналам, которые реально торговались бы."""
    # варианты выхода: сравниваем на сигналах, где посчитаны все, основной берём по настройкам
    both = [r for r in rows if r["r_pct"] is not None and all(r.get(c) is not None for c in EXIT_NAMES)]
    for r in rows:
        r["_orig"] = {c: r.get(c) for c in EXIT_NAMES}
        if r.get(exit_col) is not None:
            r["r_pct"] = r[exit_col]
    all_rows = rows
    rows = [r for r in rows if not r["f"].get("shadow")]
    all_r = [r["r_pct"] for r in rows if r["r_pct"] is not None]
    base = stats(all_r)
    if not base:
        return ["🧠 <b>Анализ</b>\nЗавершённых сигналов пока нет, нужно подождать."]
    head = [
        f"🧠 <b>Анализ · {title}</b>",
        "",
        f"Сигналов с результатом: <b>{base['n']}</b>",
        f"В плюс {base['win']:.0f}% · в среднем <b>{base['mean']:+.3f}%</b> на сделку · "
        f"профит-фактор <b>{_pf(base['pf'])}</b>",
        "<i>Каждый сигнал проверяется как сделка до стопа, тейка или таймаута, с комиссиями.</i>",
    ]
    live_both = [r for r in both if not r["f"].get("shadow")]
    if len(live_both) >= min_n:
        head.append(f"\n🚪 <b>Варианты выхода</b> ({len(live_both)} сигналов)")
        for c, name in EXIT_NAMES.items():
            st = stats([r["_orig"][c] for r in live_both])
            mark = "  ← сейчас" if c == exit_col else ""
            head.append(f"{name}: {st['mean']:+.3f}% · в плюс {st['win']:.0f}%{mark}")
    findings = []
    sections = []
    for gname, key in GROUPS:
        buckets = {}
        for r in (all_rows if gname == "Тип сигнала" else rows):
            if r["r_pct"] is None:
                continue
            b = key(r)
            if b is not None:
                buckets.setdefault(b, []).append(r["r_pct"])
        items = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
        if gname == "Монета":
            items = items[:10]
        table = []
        for b, rs in items:
            st = stats(rs)
            if st["n"] < min_n:
                continue
            # сравниваем с остальными сигналами этой же группы (у которых признак тоже записан),
            # а не со всеми сигналами: иначе новые признаки, которые есть только у свежих сигналов,
            # сравнивались бы со старым периодом
            rest = [x for bb, xs in buckets.items() if bb != b for x in xs]
            other = stats(rest) if len(rest) >= min_n else None
            mark = "  "
            if other:
                diff = st["mean"] - other["mean"]
                se = (st["se"] ** 2 + other["se"] ** 2) ** 0.5
                if se and abs(diff) > 2 * se:
                    mark = " ✅" if diff > 0 else " ❌"
                    findings.append((abs(diff), diff, gname, b, st, other))
            table.append(html.escape(f"{str(b)[:18]:<18} {st['n']:>4} {st['win']:>4.0f}% {st['mean']:>+7.3f}%")
                         + mark)
        if table:
            hdr = f"{'':<18} {'N':>4} {'плюс':>5} {'среднее':>8}"
            sections.append(f"<b>{gname}</b>\n<pre>{hdr}\n" + "\n".join(table) + "</pre>")

    out = "\n".join(head)
    if findings:
        findings.sort(reverse=True)
        fl = ["", "<b>Главные выводы</b>"]
        for _, diff, g, b, st, other in findings[:8]:
            icon = "✅" if diff > 0 else "❌"
            fl.append(f"{icon} <b>{html.escape(g)}: {html.escape(str(b))}</b>\n"
                      f"    {st['mean']:+.3f}% на сделку ({st['n']} сигналов, в плюс {st['win']:.0f}%), "
                      f"у остальных {other['mean']:+.3f}%")
        fl.append("<i>Каждая группа сравнивается с остальными сигналами, где этот признак записан. "
                  "Показаны только отличия, которые вряд ли случайны.</i>")
        out += "\n" + "\n".join(fl)
    else:
        need = "" if base["n"] >= min_n * 4 else f" Нужно хотя бы ~{min_n * 4} сигналов."
        out += f"\n\nПока нет отличий, которые нельзя объяснить случайностью.{need}"

    msgs = [out]
    cur = "📋 <b>Подробно по группам</b>"
    for sec in sections:
        if len(cur) + len(sec) > 3500:
            msgs.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + sec
    if sections:
        msgs.append(cur)
    return msgs
