"""Разбор результатов сигналов по обстановке: что работает, а что нет.

Результат каждого сигнала (r_pct) это виртуальная сделка до стопа, тейка или таймаута,
в % от позиции, уже с комиссиями. Вывод по группе делаем, только если в ней достаточно
сигналов и отличие от среднего больше двух стандартных ошибок (грубая проверка,
что это не случайность)."""
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


def analyze(rows, min_n=15, title="всё время"):
    """Возвращает список сообщений (Telegram ограничивает длину)."""
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
    findings = []
    sections = []
    for gname, key in GROUPS:
        buckets = {}
        for r in rows:
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
            diff = st["mean"] - base["mean"]
            mark = "  "
            if st["se"] and abs(diff) > 2 * st["se"]:
                mark = " ✅" if diff > 0 else " ❌"
                findings.append((abs(diff), diff, gname, b, st))
            table.append(f"{str(b)[:18]:<18} {st['n']:>4} {st['win']:>4.0f}% {st['mean']:>+7.3f}%{mark}")
        if table:
            hdr = f"{'':<18} {'N':>4} {'плюс':>5} {'среднее':>8}"
            sections.append(f"<b>{gname}</b>\n<pre>{hdr}\n" + "\n".join(table) + "</pre>")

    out = "\n".join(head)
    if findings:
        findings.sort(reverse=True)
        fl = ["", "<b>Главные выводы</b>"]
        for _, diff, g, b, st in findings[:8]:
            icon = "✅" if diff > 0 else "❌"
            verdict = "лучше среднего" if diff > 0 else "хуже среднего"
            fl.append(f"{icon} <b>{g}: {b}</b>\n    {verdict}, {st['mean']:+.3f}% на сделку "
                      f"({st['n']} сигналов, в плюс {st['win']:.0f}%)")
        fl.append("<i>Показаны только отличия, которые вряд ли случайны.</i>")
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
