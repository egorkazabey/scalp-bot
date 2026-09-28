"""Настройки бота: хранятся в data/settings.json и меняются прямо из Telegram."""
import json
import os
import threading
from copy import deepcopy

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")

# key: (значение по умолчанию, тип, описание для Telegram)
PARAMS = {
    # --- монеты ---
    "coin_mode":        ("manual", str,   "Режим монет: manual (свой список), auto (топ по обороту), "
                                          "movers (топ роста и падения) или mix (всё вместе)"),
    "auto_top_n":       (15,       int,   "Сколько монет брать из топа по обороту (auto и mix)"),
    "movers_n":         (5,        int,   "Сколько монет брать из топа роста и столько же из топа падения"),
    "auto_min_turnover": (20_000_000, float, "Мин. оборот за 24ч в $: монеты мельче не берём (auto, movers, mix)"),
    "max_coins":        (30,       int,   "Макс. монет одновременно"),
    "refresh_min":      (15,       int,   "Как часто обновлять список монет в авто-режимах, мин"),
    "ob_depth":         (1000,     int,   "Глубина стакана: 50, 200 или 1000 уровней"),
    "min_book_usd":     (150_000,  float, "Мин. сумма заявок в пределах 1% от цены (каждая сторона), $. "
                                          "Меньше: стакан тонкий, сигналы не даются"),

    # --- автоподстройка порогов под монету ---
    "auto_scale":       (True,     bool,  "Подстраивать пороги плотности, объёма и ликвидаций под каждую монету"),
    "wall_share_pct":   (1.0,      float, "Автопорог плотности: мин. % от всех заявок этой стороны в зоне поиска"),
    "liq_turnover_pct": (0.02,     float, "Автопорог ликвидаций: % от оборота монеты за 24ч"),

    # --- плотности ---
    "min_wall_usd":     (300_000,  float, "Мин. размер плотности в $ (при автоподстройке мин. $20K)"),
    "wall_mult":        (4.0,      float, "Во сколько раз плотность больше соседних уровней стакана (по 10 с каждой стороны)"),
    "wall_max_dist_pct": (1.5,     float, "Макс. расстояние плотности от цены, %"),
    "max_walls_side":   (3,        int,   "Сколько самых крупных плотностей отслеживать с каждой стороны"),
    "min_wall_age_sec": (30,       int,   "Мин. время жизни плотности до сигнала, сек"),
    "min_trust":        (55,       int,   "Мин. рейтинг доверия плотности (0-100)"),
    "approach_pct":     (0.15,     float, "На каком расстоянии до плотности давать сигнал отскока, %"),

    # --- объём ---
    "vol_mult":         (4.0,      float, "Всплеск объёма: во сколько раз минутный объём выше среднего"),
    "vol_min_move_pct": (0.4,      float, "Всплеск объёма: мин. движение цены за минуту, %"),
    "vol_min_usd":      (500_000,  float, "Всплеск объёма: мин. объём за минуту в $"),

    # --- ликвидации ---
    "liq_usd":          (250_000,  float, "Ликвидации: мин. сумма за 60 сек в $"),
    "liq_mode":         ("reversal", str, "Ликвидации: reversal (против каскада) или momentum (по каскаду)"),

    # --- риск и бумажная торговля ---
    "paper_enabled":    (True,     bool,  "Открывать бумажные сделки по сигналам"),
    "start_balance":    (1000.0,   float, "Стартовый виртуальный баланс, $"),
    "size_mode":        ("risk",   str,   "Размер позиции: risk (теряем risk_pct% на стопе) или "
                                          "margin (залог margin_pct% от баланса x плечо)"),
    "risk_pct":         (1.0,      float, "Режим risk: сколько % баланса теряем, если сработал стоп"),
    "margin_pct":       (1.0,      float, "Режим margin: сколько % баланса идёт в залог сделки"),
    "max_leverage":     (10.0,     float, "Плечо (в режиме risk это верхний предел)"),
    "rr":               (2.0,      float, "Соотношение прибыль/риск для тейка"),
    "sl_buffer_pct":    (0.1,      float, "Стоп за плотностью с запасом, %"),
    "default_sl_pct":   (0.35,     float, "Стоп для сигналов без плотности, %"),
    "max_hold_min":     (30,       int,   "Закрыть бумажную сделку через N минут"),
    "max_open":         (3,        int,   "Макс. одновременных бумажных сделок"),
    "daily_loss_pct":   (5.0,      float, "Стоп на день: при убытке больше N% новые сделки не открываются"),
    "fee_pct":          (0.055,    float, "Комиссия тейкера (вход по рынку, стоп), %"),
    "maker_fee_pct":    (0.02,     float, "Комиссия мейкера (тейк лимиткой), %"),
    "slippage_pct":     (0.02,     float, "Проскальзывание на вход/выход, %"),

    # --- общее ---
    "cooldown_sec":     (300,      int,   "Пауза между одинаковыми сигналами по монете, сек"),
    "btc_filter":       (False,    bool,  "Не давать лонги по альтам, когда BTC падает (и наоборот)"),
    "btc_filter_pct":   (0.3,      float, "BTC-фильтр: движение BTC за 5 мин, %"),
}

SIGNAL_TYPES = {
    "bounce":   "Отскок от плотности",
    "breakout": "Пробой (плотность съели)",
    "volume":   "Всплеск объёма",
    "liq":      "Каскад ликвидаций",
}

# Когда меняется значение по умолчанию: если у пользователя стоит старое значение по умолчанию
# (он его не трогал), переводим на новое. Своё значение пользователя не трогаем.
SETTINGS_VERSION = 2
MIGRATIONS = {
    2: [("ob_depth", 200, 1000), ("wall_mult", 6.0, 4.0), ("auto_min_turnover", 50_000_000, 20_000_000)],
}

DEFAULT_STATE = {
    "version": SETTINGS_VERSION,
    "owner_id": None,
    "paused": False,
    "coins": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"],
    "signals_on": {k: True for k in SIGNAL_TYPES},
    "notify": {k: True for k in SIGNAL_TYPES},
    "params": {k: v[0] for k, v in PARAMS.items()},
    "overrides": {},  # {"BTCUSDT": {"min_wall_usd": 3000000}}
}


def _cast(key, raw):
    typ = PARAMS[key][1]
    if typ is bool:
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "on", "yes", "да", "вкл"):
            return True
        if s in ("0", "false", "off", "no", "нет", "выкл"):
            return False
        raise ValueError("нужно on/off")
    if typ in (int, float):
        s = str(raw).strip().lower().replace(" ", "").replace("_", "").replace(",", ".")
        mult = 1
        if s.endswith("k"):
            mult, s = 1_000, s[:-1]
        elif s.endswith("m"):
            mult, s = 1_000_000, s[:-1]
        try:
            val = float(s) * mult
        except ValueError:
            raise ValueError("нужно число, например 500k, 2.5m или 0.3") from None
        return int(val) if typ is int else val
    return str(raw).strip()


def _validate(key, val):
    if key == "coin_mode" and val not in ("manual", "auto", "movers", "mix"):
        raise ValueError("manual, auto, movers или mix")
    if key == "size_mode" and val not in ("risk", "margin"):
        raise ValueError("risk или margin")
    if key == "liq_mode" and val not in ("reversal", "momentum"):
        raise ValueError("reversal или momentum")
    if key == "ob_depth" and val not in (50, 200, 1000):
        raise ValueError("50, 200 или 1000")
    if isinstance(val, (int, float)) and not isinstance(val, bool) and val < 0:
        raise ValueError("не может быть отрицательным")


class Settings:
    def __init__(self, path=SETTINGS_PATH):
        self.path = path
        self._lock = threading.Lock()
        self.state = deepcopy(DEFAULT_STATE)
        self.load()

    def load(self):
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(self.state.get(k), dict):
                    self.state[k].update(v)
                else:
                    self.state[k] = v
            ver = saved.get("version", 1)
            for v in range(ver + 1, SETTINGS_VERSION + 1):
                for key, old, new in MIGRATIONS.get(v, []):
                    if self.state["params"].get(key) == old:
                        self.state["params"][key] = new
            self.state["version"] = SETTINGS_VERSION
            # параметры, которых больше нет
            for k in [k for k in self.state["params"] if k not in PARAMS]:
                del self.state["params"][k]
        self.save()

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    # параметры
    def get(self, key, symbol=None):
        if symbol and key in self.state["overrides"].get(symbol, {}):
            return self.state["overrides"][symbol][key]
        return self.state["params"].get(key, PARAMS[key][0])

    def set(self, key, raw, symbol=None):
        if key not in PARAMS:
            raise KeyError(key)
        val = _cast(key, raw)
        _validate(key, val)
        if symbol:
            self.state["overrides"].setdefault(symbol, {})[key] = val
        else:
            self.state["params"][key] = val
        self.save()
        return val

    def clear_override(self, symbol, key=None):
        ov = self.state["overrides"].get(symbol, {})
        if key:
            ov.pop(key, None)
        else:
            ov.clear()
        if not ov:
            self.state["overrides"].pop(symbol, None)
        self.save()

    def reset_params(self):
        self.state["params"] = {k: v[0] for k, v in PARAMS.items()}
        self.state["overrides"] = {}
        self.save()

    def __getitem__(self, k):
        return self.state[k]

    def __setitem__(self, k, v):
        self.state[k] = v
        self.save()
