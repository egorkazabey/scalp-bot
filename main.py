"""Точка входа: python main.py"""
import logging
import os
import sys

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
    from config import DATA_DIR, Settings
    from storage import Storage
    from bot import TgBot

    os.makedirs(DATA_DIR, exist_ok=True)
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
