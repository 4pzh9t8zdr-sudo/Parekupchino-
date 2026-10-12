import json
import os
import re
import statistics
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

CONFIG_PATH = "config.json"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.8",
}
MAX_SEEN = 2500


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("filters", [])
    data.setdefault("seen", [])
    data.setdefault("last_update_id", 0)
    data.setdefault("scan_now", False)
    return data


def save_config(data):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def telegram(method, payload=None, timeout=20):
    if not API:
        return {}
    try:
        r = requests.post(f"{API}/{method}", json=payload or {}, timeout=timeout)
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            print("Telegram API error:", body)
        return body
    except requests.RequestException as e:
        print("Telegram request error:", e)
        return {}


def send(text):
    if not CHAT_ID:
        print("TELEGRAM_CHAT_ID is not set; message:", text)
        return
    telegram("sendMessage", {
        "chat_id": CHAT_ID,
        "text": text[:3900],
        "disable_web_page_preview": True,
    })


def money(value):
    return f"{int(value):,}".replace(",", " ") + " ₸"


def parse_price(text):
    if not text:
        return None
    normalized = text.replace("\xa0", " ").replace("тг", "").replace("₸", "")
    normalized = normalized.replace(" ", "")
    m = re.search(r"(\d[\d.,]*)", normalized)
    if not m:
        return None
    raw = m.group(1)
    if "," in raw and "." not in raw:
        raw = raw.replace(",", ".")
    try:
        value = float(raw)
        if value <= 0 or value > 1_000_000_000:
            return None
        return int(value)
    except ValueError:
        return None


def normalize_url(href, base):
    if not href:
        return ""
    from urllib.parse import urljoin
    url = urljoin(base, href).split("?")[0].rstrip("/")
    if not url.startswith("https://www.olx.kz/"):
        return ""
    return url


def extract_items(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    items, urls = [], set()

    # Prefer JSON-LD when present.
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            obj = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        stack = obj if isinstance(obj, list) else [obj]
        for node in stack:
            if not isinstance(node, dict):
                continue
            if node.get("@type") == "ItemList":
                stack.extend(node.get("itemListElement", []))
            item = node.get("item", node)
            if isinstance(item, dict) and item.get("@type") in ("Product", "Offer", "Thing"):
                title = item.get("name") or ""
                url = normalize_url(item.get("url"), base_url)
                offers = item.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = parse_price(str(offers.get("price", ""))) if isinstance(offers, dict) else None
                if url and title and url not in urls:
                    items.append({"title": title.strip(), "url": url, "price": price, "raw": title})
                    urls.add(url)

    # DOM fallback for common OLX card layouts.
    selectors = [
        '[data-cy="l-card"]',
        'div[data-testid="listing-grid"] > div',
        'div[data-testid="listing-item"]',
    ]
    cards = []
    for selector in selectors:
        cards = soup.select(selector)
        if cards:
            break
    for card in cards:
        link = card.select_one('a[href*="/d/"], a[href*="/obyavlenie/"]')
        if not link:
            link = card.select_one("a[href]")
        url = normalize_url(link.get("href") if link else "", base_url)
        title_node = card.select_one("h4, h3, [data-cy='ad-card-title'], [aria-label]")
        title = (title_node.get_text(" ", strip=True) if title_node else
                 (link.get_text(" ", strip=True) if link else ""))
        if not url or not title or url in urls:
            continue
        text = card.get_text(" ", strip=True)
        price_node = card.select_one('[data-testid*="ad-price"], [data-testid*="price"], p')
        price = parse_price(price_node.get_text(" ", strip=True) if price_node else text)
        items.append({"title": title, "url": url, "price": price, "raw": text})
        urls.add(url)
    return items


def fetch_search(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc not in ("www.olx.kz", "olx.kz"):
        raise ValueError("Разрешены только ссылки https://www.olx.kz/...")
    r = requests.get(url, headers=HEADERS, timeout=25)
    if r.status_code in (403, 429):
        raise RuntimeError(f"OLX вернул HTTP {r.status_code}; проверка остановлена, не обходите защиту.")
    r.raise_for_status()
    if "captcha" in r.url.lower() or "robot" in r.text[:5000].lower():
        raise RuntimeError("Похоже, OLX показывает проверку/защиту. Автоматический разбор остановлен.")
    return extract_items(r.text, url)


def market_median(items):
    prices = [x["price"] for x in items if x.get("price") and x["price"] > 0]
    return statistics.median(prices) if len(prices) >= 5 else None


def filter_match(item, f):
    price = item.get("price")
    if price is None:
        return False
    if f.get("min_price") is not None and price < f["min_price"]:
        return False
    if f.get("max_price") is not None and price > f["max_price"]:
        return False
    city = (f.get("city") or "").strip().casefold()
    if city and city not in (item.get("raw", "") + " " + item.get("title", "")).casefold():
        return False
    kws = [x.strip().casefold() for x in f.get("keywords", []) if x.strip()]
    if kws and not any(k in item["title"].casefold() for k in kws):
        return False
    return True


def add_filter(data, url, keywords):
    data["filters"].append({
        "id": max([f.get("id", 0) for f in data["filters"]] + [0]) + 1,
        "url": url,
        "label": " ".join(keywords).strip() or f"Поиск {len(data['filters']) + 1}",
        "keywords": keywords,
        "city": "",
        "min_price": None,
        "max_price": None,
    })
    return data["filters"][-1]


def command_help():
    return (
        "OLX Radar KZ — команды\n\n"
        "/add URL ключевые_слова — добавить поиск (URL сначала, затем слова)\n"
        "/list — список фильтров\n"
        "/del ID — удалить фильтр\n"
        "/max ID СУММА — максимальная цена в ₸\n"
        "/min ID СУММА — минимальная цена в ₸\n"
        "/city ID ГОРОД — фильтр по тексту города\n"
        "/keywords ID слово1, слово2 — ключевые слова в названии\n"
        "/scan — проверить при следующем запуске workflow\n"
        "/help — эта справка\n\n"
        "Добавляй ссылку на результаты поиска OLX.kz с выбранной моделью и сортировкой «Сначала новые»."
    )


def handle_updates(data):
    result = telegram("getUpdates", {
        "offset": data.get("last_update_id", 0) + 1,
        "timeout": 1,
        "allowed_updates": ["message"],
    }, timeout=10).get("result", [])
    for upd in result:
        data["last_update_id"] = max(data.get("last_update_id", 0), upd.get("update_id", 0))
        msg = upd.get("message", {})
        chat = str(msg.get("chat", {}).get("id", ""))
        if CHAT_ID and chat != CHAT_ID:
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        try:
            if cmd in ("/start", "/help"):
                send(command_help())
            elif cmd == "/add":
                if len(parts) < 2:
                    send("Формат: /add URL ключевые_слова")
                    continue
                url = parts[1]
                parsed = urlparse(url)
                if parsed.scheme != "https" or parsed.netloc not in ("www.olx.kz", "olx.kz"):
                    send("Нужна полная HTTPS-ссылка на поиск с домена olx.kz.")
                    continue
                keywords = " ".join(parts[2:]).split(",") if len(parts) > 2 else []
                f = add_filter(data, url, [k.strip() for k in keywords if k.strip()])
                save_config(data)
                send(f"Добавлен фильтр №{f['id']}: {f['label']}\nПервые найденные объявления будут сохранены без уведомлений, чтобы не завалить чат старыми лотами.")
            elif cmd == "/list":
                if not data["filters"]:
                    send("Фильтров пока нет. Используй /add URL ключевые_слова")
                else:
                    lines = []
                    for f in data["filters"]:
                        lines.append(f"№{f['id']} — {f['label']}\nЦена: {f.get('min_price') or 0}–{f.get('max_price') or 'без лимита'} ₸; город: {f.get('city') or 'любой'}\n{f['url']}")
                    send("\n\n".join(lines))
            elif cmd == "/del" and len(parts) >= 2:
                fid = int(parts[1])
                before = len(data["filters"])
                data["filters"] = [f for f in data["filters"] if f["id"] != fid]
                save_config(data)
                send("Фильтр удалён." if len(data["filters"]) < before else "Фильтр с таким ID не найден.")
            elif cmd in ("/max", "/min") and len(parts) >= 3:
                fid, amount = int(parts[1]), int(parts[2])
                f = next((x for x in data["filters"] if x["id"] == fid), None)
                if not f:
                    send("Не найден ID. Посмотри /list")
                else:
                    f["max_price" if cmd == "/max" else "min_price"] = amount
                    save_config(data)
                    send(f"Обновлено: фильтр №{fid}, {'максимум' if cmd == '/max' else 'минимум'} {money(amount)}")
            elif cmd == "/city" and len(parts) >= 3:
                fid, city = int(parts[1]), " ".join(parts[2:])
                f = next((x for x in data["filters"] if x["id"] == fid), None)
                if not f:
                    send("Не найден ID. Посмотри /list")
                else:
                    f["city"] = city
                    save_config(data)
                    send(f"Для фильтра №{fid} установлен город: {city}")
            elif cmd == "/keywords" and len(parts) >= 3:
                fid = int(parts[1])
                kws = " ".join(parts[2:]).split(",")
                f = next((x for x in data["filters"] if x["id"] == fid), None)
                if not f:
                    send("Не найден ID. Посмотри /list")
                else:
                    f["keywords"] = [x.strip() for x in kws if x.strip() and x.strip() != "-"]
                    save_config(data)
                    send(f"Ключевые слова обновлены для фильтра №{fid}.")
            elif cmd == "/scan":
                data["scan_now"] = True
                save_config(data)
                send("Запрос проверки сохранён. Нажми Run workflow в GitHub → Actions → OLX Radar KZ.")
            else:
                send("Не понял команду. Напиши /help")
        except (ValueError, IndexError) as e:
            send(f"Ошибка формата команды: {e}\nНапиши /help")
    return data


def run_scan(data):
    seen = set(data.get("seen", []))
    new_seen = list(data.get("seen", []))
    for f in data["filters"]:
        try:
            items = fetch_search(f["url"])
            median = market_median(items)
            # OLX pages may sort newest first. Store initial snapshot without sending old listings.
            key = f"initialized_{f['id']}"
            if not data.get(key):
                for item in items:
                    if item["url"] not in seen:
                        new_seen.append(item["url"])
                        seen.add(item["url"])
                data[key] = True
                send(f"Фильтр №{f['id']} инициализирован: найдено {len(items)} объявлений. Новые объявления будут приходить в следующих проверках.")
                continue
            count = 0
            for item in items:
                url = item["url"]
                if url in seen:
                    continue
                new_seen.append(url)
                seen.add(url)
                if not filter_match(item, f):
                    continue
                price = item["price"]
                msg = f"🆕 {item['title']}\n💰 {money(price)}\n"
                if median:
                    diff = (median - price) / median * 100
                    msg += f"📊 Медиана поиска: {money(median)} ({diff:+.0f}% разницы)\n"
                    if diff >= 15:
                        msg = "🔥 НИЖЕ МЕДИАНЫ НА " + f"{diff:.0f}%\n" + msg
                else:
                    msg += "📊 Для оценки рынка пока мало цен (нужно минимум 5).\n"
                msg += f"🔗 {url}\nФильтр: {f['label']}"
                send(msg)
                count += 1
            print(f"Filter {f['id']}: parsed {len(items)}, sent {count}, median={median}")
        except Exception as e:
            print(f"Filter {f.get('id')} failed: {type(e).__name__}: {e}")
    data["seen"] = new_seen[-MAX_SEEN:]
    data["scan_now"] = False
    save_config(data)


def main():
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in GitHub Secrets.")
    data = load_config()
    data = handle_updates(data)
    run_scan(data)


if __name__ == "__main__":
    main()
