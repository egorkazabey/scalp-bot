"""Точка входа: python main.py"""
import logging
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path=os.path.join(HERE, ".env")):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    load_env()
    # часовой пояс для «статистики за сегодня» и дневного лимита убытка
    os.environ.setdefault("TZ", "Europe/Prague")
    if hasattr(time, "tzset"):
        time.tzset()
    from config import DATA_DIR, Settings
    from storage import Storage
    from bot import TgBot

    os.makedirs(DATA_DIR, exist_ok=True)
    # только одна копия бота на папку data: вторая копия считала бы лимиты сделок отдельно
    import fcntl
    lock = open(os.path.join(DATA_DIR, "bot.lock"), "w")
    # при обновлении на хостинге новая копия может стартовать раньше, чем остановится старая:
    # ждём до 2 минут, пока старая освободит блокировку, и только потом сдаёмся
    for attempt in range(60):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if attempt == 0:
                print("Другая копия бота ещё работает, жду, пока она остановится...", flush=True)
            time.sleep(2)
    else:
        sys.exit("Бот уже запущен в другой копии, вторая не нужна. "
                 "Остановите старую копию (на VPS: sudo systemctl status scalpbot)")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(os.path.join(DATA_DIR, "bot.log"), encoding="utf-8")],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        sys.exit("Нет BOT_TOKEN. Создай файл .env по образцу .env.example")

    TgBot(token, Settings(), Storage()).run()


if __name__ == "__main__":
    main()
