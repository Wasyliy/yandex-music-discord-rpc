"""
Discord Rich Presence для Яндекс Музыки (только Windows).

Показывает в Discord трек, который сейчас играет, а когда музыки нет —
вращает «обычные» экраны (статистика дня, настроение, нагрузка ПК и т.д.).
Все личные настройки лежат в config.json (см. config.example.json).
"""
import asyncio
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

if sys.platform != "win32":
    sys.exit("Скрипт работает только в Windows (нужен системный медиа-интерфейс SMTC).")

import requests
from pypresence import AioPresence, ActivityType, StatusDisplayType
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
)
from winrt.windows.storage.streams import DataReader

try:
    import psutil
    psutil.cpu_percent(None)   # «прогрев» счётчика
except ImportError:
    psutil = None

BASE_DIR = Path(__file__).resolve().parent

if sys.stdout is None:   # запуск через pythonw.exe — пишем вывод в файл
    sys.stdout = sys.stderr = open(BASE_DIR / "rpc.log", "a", encoding="utf-8", buffering=1)


# ---------- конфиг ----------
DEFAULTS = {
    "client_id": "",
    "webhook_url": "",
    "github_url": "",
    "button_label": "GitHub",
    "app_name": "My Status",
    "source_hints": ["yandex"],
    "fallback_cover": "cover",
    "idle_decor": "༓ ♡ ༓",
    "idle_large_text": "♡",
    "timer_label": "В онлайне на протяжении:",
    "cute_lines": [
        "не забудь попить воды ✧",
        "ты молодец ♡",
        "пора размяться (っ◔◡◔)っ",
        "улыбнись :3",
        "розовое настроение ✿",
        "⊂(・ω・⊂)",
    ],
    "moods": {
        "morning": ["(｡•ᴗ•｡)", "доброе утро ✿"],
        "day": ["٩(◕‿◕｡)۶", "день в разгаре ✧"],
        "evening": ["(´｡• ω •｡`)", "вечерний режим ♡"],
        "night": ["(｡-ω-)zzZ", "ночная сова ✧"],
    },
    "rotate_every": 60,   # сек: как часто менять экран
    "grace": 8,           # сек: пауза/переключение трека до возврата в обычный режим
    "min_interval": 5,    # сек: минимум между обновлениями в Discord
    "poll": 2,            # сек: опрос плеера
}

CONFIG_FILE = BASE_DIR / "config.json"


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        print(f"Не найден {CONFIG_FILE}. Скопируйте config.example.json в config.json и заполните его.")
        sys.exit(1)
    except Exception as e:
        print("Не удалось прочитать config.json:", e)
        sys.exit(1)
    if not str(cfg["client_id"]).strip():
        print("В config.json не указан client_id (Application ID из Discord Developer Portal).")
        sys.exit(1)
    return cfg


CFG = load_config()

CLIENT_ID = str(CFG["client_id"]).strip()
WEBHOOK_URL = (CFG["webhook_url"] or "").strip()
FALLBACK_COVER = CFG["fallback_cover"]
SOURCE_HINTS = tuple(h.lower() for h in CFG["source_hints"])
ROTATE_EVERY = CFG["rotate_every"]
GRACE = CFG["grace"]
MIN_INTERVAL = CFG["min_interval"]
POLL = CFG["poll"]

SCRIPT_START = int(time.time())
STATS_FILE = BASE_DIR / "rpc_stats.json"

_cache = {}


def base_buttons():
    if CFG["github_url"]:
        return [{"label": CFG["button_label"], "url": CFG["github_url"]}]
    return []


# ---------- обложки ----------
def _norm(s):
    return re.sub(r"[^\w]+", " ", s.lower()).strip()


def _same_title(a, b):
    a, b = _norm(a), _norm(b)
    return bool(a) and bool(b) and (a in b or b in a)


def deezer_cover(artist, title):
    main_artist = artist.split(",")[0].strip()
    for q in (f'artist:"{main_artist}" track:"{title}"', f"{main_artist} {title}"):
        try:
            r = requests.get(
                "https://api.deezer.com/search",
                params={"q": q, "limit": 5},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            js = r.json()
            if "error" in js:
                print("Deezer error:", js["error"])
            for d in js.get("data") or []:
                if _same_title(title, d["title"]):
                    return d["album"]["cover_xl"]
        except Exception as e:
            print("Deezer failed:", repr(e))
    return None


def itunes_cover(artist, title):
    try:
        r = requests.get(
            "https://itunes.apple.com/search",
            params={"term": f"{artist} {title}", "entity": "song", "limit": 5},
            timeout=8,
        ).json()
        for d in r.get("results", []):
            if _same_title(title, d.get("trackName", "")):
                return d["artworkUrl100"].replace("100x100bb", "600x600bb")
    except Exception as e:
        print("iTunes failed:", repr(e))
    return None


def webhook_upload(data: bytes):
    if not WEBHOOK_URL:
        return None
    try:
        ext = "png" if data[:4] == b"\x89PNG" else "jpg"
        r = requests.post(
            WEBHOOK_URL,
            params={"wait": "true"},
            files={"file": (f"c.{ext}", data)},
            timeout=15,
        )
        if r.ok:
            return r.json()["attachments"][0]["url"]
        print("Webhook upload failed:", r.status_code, r.text[:200])
    except Exception as e:
        print("Webhook upload failed:", e)
    return None


async def thumbnail_url(props):
    """Обложка прямо из плеера -> вебхук -> ссылка."""
    try:
        if not props.thumbnail or not WEBHOOK_URL:
            return None
        stream = await props.thumbnail.open_read_async()
        size = stream.size
        reader = DataReader(stream)
        await reader.load_async(size)
        buf = bytearray(size)
        reader.read_bytes(buf)
        return await asyncio.to_thread(webhook_upload, bytes(buf))
    except Exception as e:
        print("Thumbnail failed:", e)
        return None


async def resolve_cover(cur):
    key = (cur["artist"], cur["title"])
    if key not in _cache:
        print(f"lookup: artist={cur['artist']!r} title={cur['title']!r}")
        url = await asyncio.to_thread(deezer_cover, cur["artist"], cur["title"])
        print("  deezer:", url)
        if not url:
            url = await asyncio.to_thread(itunes_cover, cur["artist"], cur["title"])
            print("  itunes:", url)
        if not url:
            url = await thumbnail_url(cur["props"])
            print("  thumbnail:", url)
        _cache[key] = url or FALLBACK_COVER
        print("cover:", _cache[key])
    return _cache[key]


# ---------- текущий трек ----------
async def get_current():
    mgr = await SessionManager.request_async()
    session = next(
        (s for s in mgr.get_sessions()
         if any(h in s.source_app_user_model_id.lower() for h in SOURCE_HINTS)),
        None,
    )
    if not session:
        return None
    props = await session.try_get_media_properties_async()
    tl = session.get_timeline_properties()
    playing = session.get_playback_info().playback_status == Status.PLAYING
    duration = (tl.end_time - tl.start_time).total_seconds()

    # Плеер отдаёт «позицию на момент last_updated_time» — досчитываем до «сейчас»
    raw_pos = tl.position.total_seconds()
    lu = tl.last_updated_time
    if lu.tzinfo is None:
        lu = lu.replace(tzinfo=timezone.utc)

    pos = raw_pos
    age = (datetime.now(timezone.utc) - lu).total_seconds()
    if playing and 0 <= age < duration:
        pos += age
    pos = min(pos, duration) if duration > 0 else pos

    return {
        "props": props,
        "title": props.title,
        "artist": props.artist,
        "album": props.album_title,
        "position": pos,
        "raw": (raw_pos, lu.timestamp()),   # признак того, что плеер прислал новые данные
        "duration": duration,
        "playing": playing,
    }


async def show_music(rpc, cur, start):
    cover = await resolve_cover(cur)
    search = f"https://music.yandex.ru/search?text={quote(cur['artist'] + ' ' + cur['title'])}"
    await rpc.update(
        activity_type=ActivityType.LISTENING,
        name="Яндекс Музыка",
        details=cur["title"],
        state=cur["artist"],
        large_image=cover,
        large_text=cur["album"] or cur["title"],
        status_display_type=StatusDisplayType.DETAILS,
        buttons=[{"label": "Найти в Яндекс Музыке", "url": search}] + base_buttons(),
        start=int(start),
        end=int(start + cur["duration"]),
    )


# ---------- статистика ----------
def load_stats():
    try:
        return json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_stats(stats):
    try:
        for d in sorted(stats)[:-60]:       # храним последние 60 дней
            del stats[d]
        STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print("stats save failed:", e)


def today_stats(stats):
    return stats.setdefault(
        date.today().isoformat(),
        {"online": 0.0, "listen": 0.0, "tracks": {}, "artists": {}},
    )


def fmt(sec):
    h, m = divmod(int(sec // 60), 60)
    return f"{h}ч {m}м" if h else f"{m}м"


def week_top_artist(stats):
    total = {}
    for i in range(7):
        d = (date.today() - timedelta(days=i)).isoformat()
        for a, n in stats.get(d, {}).get("artists", {}).items():
            total[a] = total.get(a, 0) + n
    return max(total.items(), key=lambda kv: kv[1]) if total else None


def streak(stats):
    n, d = 0, date.today()
    while stats.get(d.isoformat(), {}).get("online", 0) > 0:
        n += 1
        d -= timedelta(days=1)
    return n


# ---------- экраны обычного статуса ----------
def hearts(frac, n=5):
    full = round(max(0.0, min(1.0, frac)) * n)
    return "♥" * full + "♡" * (n - full)


def mood(hour):
    if 6 <= hour < 11:
        key = "morning"
    elif 11 <= hour < 18:
        key = "day"
    elif 18 <= hour < 23:
        key = "evening"
    else:
        key = "night"
    face, text = CFG["moods"].get(key, ["", ""])
    return face, text


def build_pages(stats, now):
    """Список экранов: (id, details, state, показывать_таймер)."""
    day = today_stats(stats)
    n_tracks = sum(day["tracks"].values())

    pages = [
        ("classic", CFG["idle_decor"], CFG["timer_label"], True),
        ("stats", f"✧ сегодня в сети: {fmt(day['online'])}",
                  f"♫ треков: {n_tracks} · музыки: {fmt(day['listen'])}", False),
    ]

    if day["tracks"]:
        name, n = max(day["tracks"].items(), key=lambda kv: kv[1])
        artist, _, title = name.partition(" — ")
        pages.append(("toptrack", f"♡ трек дня: {title}", f"{artist} · ×{n}", False))

    top = week_top_artist(stats)
    if top:
        pages.append(("topartist", "✿ артист недели", f"{top[0]} · ×{top[1]}", False))

    s = streak(stats)
    if s >= 2:
        pages.append(("streak", f"♡ на связи {s} дн. подряд", "серия не прерывается ✧", False))

    face, phrase = mood(time.localtime().tm_hour)
    if face or phrase:
        pages.append(("mood", f"{face} {phrase}".strip(), "настроение сейчас", False))

    if psutil:
        cpu = psutil.cpu_percent(None)
        vibe = "отдыхает" if cpu < 20 else "работает" if cpu < 60 else "старается изо всех сил"
        pages.append(("pc", f"✿ мой пк {vibe}", f"{hearts(cpu / 100)} · CPU {cpu:.0f}%", False))

    lines = CFG["cute_lines"]
    if lines:
        line = lines[int(now // ROTATE_EVERY) % len(lines)]
        pages.append(("cute", "༓ мини-напоминание ༓", line, False))
    return pages


def idle_kwargs(page):
    _, details, state, timer = page
    kw = dict(
        activity_type=ActivityType.PLAYING,
        name=CFG["app_name"],
        details=details,
        state=state,
        large_image=FALLBACK_COVER,
        large_text=CFG["idle_large_text"],
        status_display_type=StatusDisplayType.NAME,
        buttons=base_buttons() or None,
    )
    if timer:
        kw["start"] = SCRIPT_START
    return kw


# ---------- подключение к Discord ----------
async def connect_rpc():
    """Ждём, пока Discord запустится, и подключаемся."""
    while True:
        rpc = AioPresence(CLIENT_ID)
        try:
            await rpc.connect()
            print("Discord connected")
            return rpc
        except Exception:
            await asyncio.sleep(5)


# ---------- главный цикл ----------
async def main():
    rpc = await connect_rpc()
    stats = load_stats()
    last_key, last_start, last_sent = None, 0.0, 0.0
    last_music_ts, sent_raw = 0.0, None
    last_tick, last_saved = time.time(), time.time()
    track_id, track_listened, track_counted = None, 0.0, False

    try:
        while True:
            try:
                now = time.time()
                dt = min(now - last_tick, 10.0)
                last_tick = now

                cur = await get_current()
                music = cur if (cur and cur["title"] and cur["playing"] and cur["duration"] > 0) else None

                # --- считаем статистику ---
                day = today_stats(stats)
                day["online"] += dt
                if music:
                    day["listen"] += dt
                    tid = (music["artist"], music["title"])
                    if tid != track_id:
                        track_id, track_listened, track_counted = tid, 0.0, False
                    track_listened += dt
                    if not track_counted and track_listened >= 30:
                        track_counted = True
                        name = f"{music['artist']} — {music['title']}"
                        day["tracks"][name] = day["tracks"].get(name, 0) + 1
                        a = music["artist"].split(",")[0].strip()
                        day["artists"][a] = day["artists"].get(a, 0) + 1
                if now - last_saved > 60:
                    save_stats(stats)
                    last_saved = now

                # --- что показывать ---
                wanted, seeked, start, key, page = True, False, 0.0, None, None
                if music:
                    last_music_ts = now
                    key = ("music", music["title"], music["artist"])
                    est_start = now - music["position"]
                    if key != last_key:
                        start = est_start
                    else:
                        start = last_start
                        # перемотка: только если плеер прислал новые данные и время разошлось
                        if music["raw"] != sent_raw and abs(est_start - last_start) > 3:
                            seeked, start = True, est_start
                elif now - last_music_ts < GRACE:
                    wanted = False      # переключение трека или пауза
                else:
                    pages = build_pages(stats, now)
                    page = pages[int(now // ROTATE_EVERY) % len(pages)]
                    key = ("idle", page[0])

                if wanted and (key != last_key or seeked) and now - last_sent >= MIN_INTERVAL:
                    if music:
                        await show_music(rpc, music, start)
                        sent_raw = music["raw"]
                    else:
                        await rpc.update(**idle_kwargs(page))
                    last_key, last_start, last_sent = key, start, time.time()

            except Exception as e:
                print("error:", e)
                try:
                    rpc.close()
                except Exception:
                    pass
                await asyncio.sleep(3)
                rpc = await connect_rpc()
                last_key = None

            await asyncio.sleep(POLL)
    finally:
        save_stats(stats)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
