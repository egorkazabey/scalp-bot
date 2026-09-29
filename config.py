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
    "breakout_mode":    ("fade",   str,   "Пробой: fade (против, ставка на ложный пробой) или follow (по пробою)"),

    # --- объём ---
    "vol_mult":         (4.0,      float, "Всплеск объёма: во сколько раз минутный объём выше среднего"),
    "vol_min_move_pct": (0.4,      float, "Всплеск объёма: мин. движение цены за минуту, %"),
    "vol_min_usd":      (500_000,  float, "Всплеск объёма: мин. объём за минуту в $"),

    "volume_mode":      ("reversal", str, "Всплеск объёма: reversal (против импульса) или momentum (по импульсу)"),

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
    "min_sl_pct":       (0.35,     float, "Стоп не ближе N% от входа: чтобы шум и комиссия не выбивали сделки"),
    "stop_pause_min":   (30,       int,   "После стопа не открывать бумажные сделки по этой монете N минут"),
    "max_hold_min":     (30,       int,   "Закрыть бумажную сделку через N минут"),
    "max_open":         (3,        int,   "Макс. одновременных бумажных сделок"),
    "max_same_side":    (2,        int,   "Макс. сделок в одну сторону сразу: альты ходят вместе, "
                                          "3 лонга по разным монетам это одна ставка"),
    "daily_loss_pct":   (5.0,      float, "Стоп на день: при убытке больше N% новые сделки не открываются"),
    "fee_pct":          (0.055,    float, "Комиссия тейкера (вход по рынку, стоп), %"),
    "maker_fee_pct":    (0.02,     float, "Комиссия мейкера (тейк лимиткой), %"),
    "slippage_pct":     (0.02,     float, "Проскальзывание на вход/выход, %"),
    "tp_through_pct":   (0.02,     float, "Тейк засчитывается, только если цена прошла за него на N% "
                                          "(лимитка в очереди может не исполниться от касания)"),

    # --- вход и выход ---
    "bounce_entry":     ("limit",  str,   "-"),
    "limit_offset_pct": (0.02,     float, "-"),
    "entry_wait_sec":   (90,       int,   "-"),
    "confirm_window_sec": (120,    int,   "-"),
    "confirm_move_pct": (0.1,      float, "-"),
    "confirm_eat_pct":  (3.0,      float, "-"),
    "breakeven":        (True,     bool,  "-"),
    "be_trigger":       (0.5,      float, "-"),
    "near_stop_exit":   (True,     bool,  "-"),
    "near_stop_zone":   (0.2,      float, "-"),
    "near_stop_reset":  (0.5,      float, "-"),

    # --- фильтры (сигналы всё равно проверяются виртуально) ---
    "max_depth_usd":    (10_000_000, float, "-"),
    "min_coin_move_pct": (3.0,     float, "-"),
    "min_confluence":   (2,        int,   "-"),
    "strong_conf":      (3,        int,   "-"),
    "strong_size_mult": (1.5,      float, "-"),
    "delta_block":      (True,     bool,  "-"),
    "delta_block_lvl":  (0.3,      float, "-"),
    "blocked_coins":    ("SOXL",   str,   "-"),

    # --- новости и режим рынка ---
    "news_pause":       (True,     bool,  "-"),
    "news_before_min":  (15,       int,   "-"),
    "news_after_min":   (15,       int,   "-"),
    "news_currencies":  ("USD",    str,   "-"),
    "news_impact":      ("High",   str,   "-"),
    "regime_block":     ("",       str,   "-"),

    # --- вынос стопов ---
    "sweep_min_pct":    (0.15,     float, "-"),
    "sweep_reclaim_pct": (0.05,    float, "-"),
    "sweep_vol_mult":   (2.0,      float, "-"),
    "sweep_max_pct":    (0.6,      float, "-"),
    "sweep_window_sec": (300,      int,   "-"),

    # --- обучение ---
    "auto_pause":       (True,     bool,  "Автопауза типов сигналов и монет, которые стабильно в минусе"),
    "pause_window":     (40,       int,   "Автопауза типа: по скольким последним сигналам судить"),
    "pause_coin_window": (15,      int,   "Автопауза монеты: по скольким последним сигналам судить"),
    "analyze_min":      (15,       int,   "/analyze: мин. сигналов в группе, чтобы делать вывод"),

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
    "sweep":    "Вынос стопов",
}

# Когда меняется значение по умолчанию: если у пользователя стоит старое значение по умолчанию
# (он его не трогал), переводим на новое. Своё значение пользователя не трогаем.
SETTINGS_VERSION = 4
MIGRATIONS = {
    2: [("ob_depth", 200, 1000), ("wall_mult", 6.0, 4.0), ("auto_min_turnover", 50_000_000, 20_000_000)],
    3: [("sweep_min_pct", 0.05, 0.15)],
    4: [("min_confluence", 0, 2)],
}

# Человеческие названия параметров для Telegram (ключ нужен только для /set)
LABELS = {
    "coin_mode": "Режим монет", "auto_top_n": "Топ по обороту, шт", "movers_n": "Топ роста и падения, шт",
    "auto_min_turnover": "Мин. оборот монеты за сутки", "max_coins": "Макс. монет",
    "refresh_min": "Обновление списка, мин", "ob_depth": "Глубина стакана, уровней",
    "min_book_usd": "Мин. глубина стакана",
    "auto_scale": "Автоподстройка порогов", "wall_share_pct": "Доля плотности в стакане, %",
    "liq_turnover_pct": "Порог ликвидаций, % оборота",
    "breakout_mode": "Режим пробоя", "min_wall_usd": "Мин. размер плотности", "wall_mult": "Больше соседей, в x раз",
    "wall_max_dist_pct": "Зона поиска от цены, %", "max_walls_side": "Плотностей на сторону",
    "min_wall_age_sec": "Мин. возраст плотности, сек", "min_trust": "Мин. доверие к плотности",
    "approach_pct": "Подход к плотности, %",
    "volume_mode": "Режим всплеска объёма", "vol_mult": "Всплеск: больше среднего, в x раз",
    "vol_min_move_pct": "Всплеск: мин. движение цены, %", "vol_min_usd": "Всплеск: мин. объём за минуту",
    "liq_usd": "Мин. ликвидаций за минуту", "liq_mode": "Режим ликвидаций",
    "paper_enabled": "Бумажная торговля", "start_balance": "Стартовый баланс", "size_mode": "Как считать размер",
    "risk_pct": "Риск на сделку, %", "margin_pct": "Залог на сделку, %", "max_leverage": "Плечо",
    "rr": "Тейк дальше стопа, в x раз", "sl_buffer_pct": "Запас стопа за плотностью, %",
    "default_sl_pct": "Стоп без плотности, %", "min_sl_pct": "Стоп не ближе, %",
    "stop_pause_min": "Пауза по монете после стопа, мин", "max_hold_min": "Макс. длительность сделки, мин",
    "max_open": "Макс. открытых сделок", "max_same_side": "Макс. сделок в одну сторону",
    "daily_loss_pct": "Дневной лимит убытка, %", "fee_pct": "Комиссия тейкера, %",
    "maker_fee_pct": "Комиссия мейкера, %", "slippage_pct": "Проскальзывание, %",
    "tp_through_pct": "Тейк: цена прошла за уровень, %",
    "auto_pause": "Автопауза", "pause_window": "Автопауза типа: окно, сигналов",
    "pause_coin_window": "Автопауза монеты: окно, сигналов", "analyze_min": "Анализ: мин. сигналов в группе",
    "cooldown_sec": "Пауза между одинаковыми сигналами, сек", "btc_filter": "BTC-фильтр",
    "btc_filter_pct": "BTC-фильтр: движение за 5 мин, %",
    "bounce_entry": "Вход на отскоке", "limit_offset_pct": "Лимитка перед плотностью, %",
    "entry_wait_sec": "Ждать исполнения лимитки, сек", "confirm_window_sec": "Ждать подтверждения, сек",
    "confirm_move_pct": "Подтверждение: цена отошла, %", "confirm_eat_pct": "Подтверждение: съели, % плотности",
    "breakeven": "Безубыток", "be_trigger": "Безубыток после, доля пути к тейку",
    "max_depth_usd": "Не торговать монеты с глубиной больше", "min_coin_move_pct": "Мин. движение монеты за сутки, %",
    "min_confluence": "Мин. совпавших факторов",
    "strong_conf": "Сильный сигнал: факторов от", "strong_size_mult": "Сильный сигнал: размер, в x раз",
    "delta_block": "Не входить против дельты", "delta_block_lvl": "Дельта против сделки от",
    "blocked_coins": "Не торговать монеты",
    "sweep_min_pct": "Прокол уровня не меньше, %", "sweep_max_pct": "Прокол уровня не больше, %",
    "sweep_window_sec": "Вернуться за уровень за, сек",
    "sweep_reclaim_pct": "Вернуться за уровень хотя бы на, %", "sweep_vol_mult": "Объём на проколе, в x раз",
    "near_stop_exit": "Выход на втором подходе к стопу", "near_stop_zone": "Близко к стопу, доля пути",
    "near_stop_reset": "Отошла от стопа, доля пути",
    "news_pause": "Пауза на важных новостях", "news_before_min": "Пауза до новости, мин",
    "news_after_min": "Пауза после новости, мин", "news_currencies": "Новости каких стран",
    "news_impact": "Какие новости", "regime_block": "Не торговать в режимах",
}

# Пояснения простым языком: что меняет параметр (показываются в настройках под названием)
DESCS = {
    "coin_mode": "Откуда брать монеты: твой список, топ по обороту, сильнее всего выросшие и упавшие или всё сразу",
    "auto_top_n": "Сколько самых оборотных монет брать",
    "movers_n": "Сколько самых выросших брать, и столько же самых упавших",
    "auto_min_turnover": "Монеты с меньшим суточным оборотом не берём: там пустой стакан",
    "max_coins": "Больше монет сразу бот не отслеживает",
    "refresh_min": "Как часто пересобирать список монет в авто-режимах",
    "ob_depth": "Сколько уровней стакана получать с Bybit. 1000 это максимум",
    "min_book_usd": "Если заявок в пределах 1% от цены меньше, стакан тонкий и сигналов по монете нет",
    "auto_scale": "Пороги ликвидаций и объёма считаются от оборота каждой монеты",
    "wall_share_pct": "Плотность должна быть не меньше этой доли от всех заявок своей стороны",
    "liq_turnover_pct": "Сколько ликвидаций за минуту считать каскадом, от суточного оборота монеты",
    "breakout_mode": "Когда плотность съели: входить против (ставка на возврат) или по направлению пробоя",
    "min_wall_usd": "Абсолютный минимум плотности, если автоподстройка выключена",
    "wall_mult": "Заявка считается плотностью, если она во столько раз больше 10 соседних уровней",
    "wall_max_dist_pct": "Дальше этого расстояния от цены плотности не ищем",
    "max_walls_side": "Сколько самых крупных плотностей следить сверху и снизу",
    "min_wall_age_sec": "Свежие заявки часто снимают, поэтому ждём столько секунд",
    "min_trust": "Отскок только от плотности с таким доверием. Доверие растёт, если она долго стоит "
                 "и её едят, а она не уходит",
    "approach_pct": "Насколько близко цена должна подойти к плотности, чтобы дать отскок",
    "volume_mode": "После резкого всплеска объёма: входить против (ставка на откат) или по импульсу",
    "vol_mult": "Во сколько раз объём за минуту должен быть больше обычного",
    "vol_min_move_pct": "Насколько цена должна сдвинуться за эту минуту",
    "vol_min_usd": "Минимальный объём за минуту в долларах",
    "liq_usd": "Сколько ликвидаций за минуту считать каскадом, если автоподстройка выключена",
    "liq_mode": "После каскада ликвидаций: входить против (ставка на откат) или по направлению",
    "paper_enabled": "Открывать виртуальные сделки по сигналам и считать баланс",
    "start_balance": "С какой суммы начинается бумажный счёт",
    "size_mode": "По риску: на стопе теряем заданный % баланса. По залогу: в сделку идёт заданный % баланса",
    "risk_pct": "Сколько % баланса теряем на стопе вместе с комиссиями (режим «по риску»)",
    "margin_pct": "Сколько % баланса идёт в залог сделки (режим «по залогу»)",
    "max_leverage": "Плечо. В режиме «по риску» это верхний предел",
    "rr": "Тейк ставится во столько раз дальше от входа, чем стоп",
    "sl_buffer_pct": "Стоп ставится чуть дальше плотности, на столько процентов",
    "default_sl_pct": "Стоп для сигналов по объёму и ликвидациям",
    "min_sl_pct": "Ближе стоп не ставим: иначе выбивает шумом и съедает комиссией",
    "stop_pause_min": "После стопа по монете столько минут не открываем по ней сделки",
    "max_hold_min": "Если за это время нет ни стопа, ни тейка, закрываем по рынку",
    "max_open": "Больше сделок одновременно не открываем",
    "max_same_side": "Альты ходят вместе, поэтому больше стольких лонгов или шортов сразу не открываем",
    "daily_loss_pct": "Если за день потеряли больше, до конца дня новых сделок нет",
    "fee_pct": "Комиссия за рыночный ордер: вход и стоп",
    "maker_fee_pct": "Комиссия за лимитный ордер: тейк",
    "slippage_pct": "Насколько хуже исполняется рыночный ордер",
    "tp_through_pct": "Тейк засчитываем, только если цена прошла за него: лимитка стоит в очереди",
    "auto_pause": "Ставить на паузу тип сигнала или монету, если они стабильно в минусе",
    "pause_window": "По скольким последним сигналам судить о типе сигнала",
    "pause_coin_window": "По скольким последним сигналам судить о монете",
    "analyze_min": "Меньше сигналов в группе: вывод в /analyze не делаем, мало данных",
    "cooldown_sec": "Одинаковый сигнал по монете не чаще, чем раз в столько секунд",
    "btc_filter": "Не давать лонги по альтам, когда BTC падает, и шорты, когда растёт",
    "btc_filter_pct": "Какое движение BTC за 5 минут считать падением или ростом",
    "bounce_entry": "Как входить на отскоке. По касанию: по рынку, как только цена подошла. Лимиткой: ордер "
                    "прямо перед плотностью, комиссия ниже. После подтверждения: плотность выдержала удар "
                    "и цена пошла назад. Остальные два способа бот проверяет виртуально, сравни в /analyze",
    "limit_offset_pct": "Лимитка ставится чуть перед плотностью, чтобы исполниться раньше неё",
    "entry_wait_sec": "Если за это время лимитка не исполнилась, она отменяется",
    "confirm_window_sec": "Сколько ждать, пока плотность докажет, что держит цену",
    "confirm_move_pct": "На сколько цена должна отойти от плотности после удара",
    "confirm_eat_pct": "Сколько объёма плотности должны съесть, а она устоять",
    "breakeven": "Когда цена прошла часть пути к тейку, стоп переносится в ноль (с учётом комиссий). "
                 "Виртуально бот считает оба варианта",
    "be_trigger": "0.5 значит перенос стопа, когда цена прошла половину пути к тейку",
    "max_depth_usd": "Крупные монеты (BTC, ETH) с таким стаканом по анализу хуже. 0 значит без фильтра",
    "min_coin_move_pct": "Монеты, которые за сутки почти не двигались, по анализу хуже. 0 значит без фильтра",
    "min_confluence": "Сколько факторов должно совпасть: BTC по пути, всплеск объёма, ликвидации, "
                      "сильная плотность. 0 значит без фильтра",
    "strong_conf": "Сигнал с таким числом совпавших факторов помечается как сильный. По анализу "
                   "3+ фактора заметно лучше остальных",
    "strong_size_mult": "Во сколько раз больше позиция у сильного сигнала на бумажном счёте. "
                        "1 значит как у обычного",
    "delta_block": "Не открывать сделку, если за последнюю минуту рынок давит против неё "
                   "(продают в лонг, покупают в шорт). Сигнал всё равно проверяется виртуально",
    "delta_block_lvl": "Дельта от -1 до 1: (покупки - продажи) / весь объём, в сторону сделки. "
                       "0.3 значит против сделки на 30% объёма больше",
    "blocked_coins": "Монеты через запятую, по которым не открывать сделки, например SOXL,ALGO. "
                     "Сигналы по ним всё равно проверяются виртуально. Пусто значит без запрета",
    "sweep_min_pct": "Цена должна выйти за максимум или минимум хотя бы на столько, чтобы собрать стопы. "
                     "Для волатильных монет порог сам растёт до половины средней 15-минутной свечи",
    "sweep_reclaim_pct": "Возврат засчитывается, только если цена ушла обратно за уровень на столько, "
                         "а не просто колеблется вокруг него",
    "sweep_vol_mult": "На проколе объём в сторону прокола должен быть во столько раз выше обычного: "
                      "это и есть сработавшие стопы",
    "sweep_max_pct": "Если ушла дальше, это уже настоящий пробой, а не вынос стопов",
    "sweep_window_sec": "Сколько времени у цены, чтобы вернуться за уровень. Не успела: сигнала нет",
    "near_stop_exit": "Если цена подошла к стопу, отошла и снова подходит, закрываем по рынку, не дожидаясь "
                      "стопа. Виртуально бот считает все варианты выхода, сравни в /analyze",
    "near_stop_zone": "0.2 значит цена в последних 20% пути от входа до стопа",
    "near_stop_reset": "Подход считается новым, если между ними цена отошла хотя бы на эту долю пути "
                       "(0.5 это половина пути до стопа)",
    "news_pause": "Вокруг важных экономических новостей (ставка ФРС, инфляция, занятость) рынок дёргается "
                  "непредсказуемо. Сделки не открываются, сигналы проверяются виртуально",
    "news_before_min": "За сколько минут до новости перестать открывать сделки",
    "news_after_min": "Сколько минут после новости ждать",
    "news_currencies": "Валюты через запятую. USD это США, для крипты важнее всего. Например: USD,EUR",
    "news_impact": "Только самые важные или ещё и средние",
    "regime_block": "Режимы рынка через запятую, в которых не открывать сделки: тренд вверх, тренд вниз, "
                    "боковик, тихо, паника. Пусто значит торговать везде. Сравни режимы в /analyze",
}
for _k, _d in DESCS.items():
    PARAMS[_k] = (PARAMS[_k][0], PARAMS[_k][1], _d)

# Параметры с вариантами: в Telegram переключаются нажатием по кругу
ENUMS = {
    "coin_mode": {"manual": "свой список", "auto": "топ по обороту", "movers": "рост и падение",
                  "mix": "всё вместе"},
    "breakout_mode": {"fade": "против (ложный пробой)", "follow": "по пробою"},
    "volume_mode": {"reversal": "против (откат)", "momentum": "по импульсу"},
    "liq_mode": {"reversal": "против каскада", "momentum": "по каскаду"},
    "size_mode": {"risk": "по риску", "margin": "по залогу"},
    "bounce_entry": {"touch": "по касанию", "limit": "лимиткой", "confirm": "после подтверждения"},
    "news_impact": {"High": "только важные", "Medium": "важные и средние"},
    "ob_depth": {50: "50", 200: "200", 1000: "1000"},
}

# Короткие названия типов для таблиц
SHORT_NAMES = {
    "bounce": "Отскок", "breakout": "Пробой", "breakout_fade": "Ложн.пробой",
    "volume": "Импульс", "volume_rev": "Откат", "liq": "Ликвидации",
    "bounce_limit": "Отскок лимит", "bounce_confirm": "Отскок подтв", "sweep": "Вынос стопов",
}

REGIMES = ["тренд вверх", "тренд вниз", "боковик", "тихо", "паника"]

# названия для статистики: у пробоя и объёма есть вариант «против сигнала»,
# его статистика считается отдельно
TYPE_NAMES = dict(SIGNAL_TYPES, **{
    "breakout_fade": "Ложный пробой (против)",
    "volume_rev": "Откат после импульса (против)",
    "bounce_limit": "Отскок: вход лимиткой",
    "bounce_confirm": "Отскок: вход после подтверждения",
})

DEFAULT_STATE = {
    "version": SETTINGS_VERSION,
    "owner_id": None,
    "paused": False,
    "coins": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"],
    "signals_on": {k: True for k in SIGNAL_TYPES},
    "notify": {k: True for k in SIGNAL_TYPES},
    "params": {k: v[0] for k, v in PARAMS.items()},
    "overrides": {},  # {"BTCUSDT": {"min_wall_usd": 3000000}}
    "auto_paused": {},  # {"type:volume_rev": {"since": ts, "why": "..."}, "coin:ALGOUSDT": {...}}
    "pause_reset": {},  # ключ -> когда пауза была снята: после этого судим только по новым сигналам
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
    s = str(raw).strip()
    if key in ("regime_block", "blocked_coins") and s.lower() in ("-", "нет", "0", "пусто", "off"):
        return ""
    if key == "blocked_coins":
        coins = [c.strip().upper().replace("USDT", "") for c in s.replace(" ", ",").split(",")]
        return ",".join(dict.fromkeys(c for c in coins if c))
    return s


def _validate(key, val):
    if key == "coin_mode" and val not in ("manual", "auto", "movers", "mix"):
        raise ValueError("manual, auto, movers или mix")
    if key == "size_mode" and val not in ("risk", "margin"):
        raise ValueError("risk или margin")
    if key == "breakout_mode" and val not in ("fade", "follow"):
        raise ValueError("fade или follow")
    if key == "volume_mode" and val not in ("reversal", "momentum"):
        raise ValueError("reversal или momentum")
    if key == "liq_mode" and val not in ("reversal", "momentum"):
        raise ValueError("reversal или momentum")
    if key in ENUMS and val not in ENUMS[key]:
        raise ValueError("варианты: " + ", ".join(map(str, ENUMS[key])))
    if key == "regime_block" and val:
        bad = [r for r in (x.strip() for x in val.split(",")) if r and r not in REGIMES]
        if bad:
            raise ValueError("неизвестные режимы: " + ", ".join(bad) + ". Есть: " + ", ".join(REGIMES))
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
