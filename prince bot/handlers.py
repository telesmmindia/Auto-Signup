import asyncio
import random
from config import PROXIES
from tasks import PLATFORMS, Progress, Task, handler, PLATFORM_URLS

import requests

REFERERS = [
    "https://twitter.com/",
    "https://x.com/",
    "https://t.co/",
    "https://www.facebook.com/",
    "https://www.instagram.com/",
    "https://www.google.com/",
    "https://www.linkedin.com/",
    "https://news.ycombinator.com/",
    "https://t.me/",
    "https://discord.com/",
    "https://kick.com/",
    "https://www.twitch.tv/",
    "https://web.whatsapp.com/",
    "",
]

USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:115.0) Gecko/20100101 Firefox/115.0",
    
    # Firefox on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0",
    
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    
    # Mobile Chrome Android
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    
    # Mobile Safari iOS
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU iPad OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    
    # Samsung Internet
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/24.0 Chrome/127.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/23.0 Chrome/126.0.0.0 Mobile Safari/537.36",
    
    # Firefox Mobile
    "Mozilla/5.0 (Android 14; Mobile; rv:128.0) Gecko/128.0 Firefox/128.0",
    "Mozilla/5.0 (Android 13; Mobile; rv:127.0) Gecko/127.0 Firefox/127.0",
    
    # Opera
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 OPR/113.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 OPR/112.0.0.0",
]

ACCEPT_LANGS = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.8",
    "en-US,en;q=0.9,es;q=0.8",
    "en-US,en;q=0.9,fr;q=0.8",
    "en-US,en;q=0.9,de;q=0.8",
    "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "en-US,en;q=0.9,ja;q=0.8",
    "en-US,en;q=0.9,ko;q=0.8",
    "en-US,en;q=0.9,pt;q=0.8,pt-BR;q=0.7",
    "en-US,en;q=0.9,ru;q=0.8",
    "en-US,en;q=0.9,ar;q=0.8",
    "en-US,en;q=0.9,hi;q=0.8",
    "en-GB,en;q=0.9,en-US;q=0.8",
    "de-DE,de;q=0.9,en;q=0.8",
    "fr-FR,fr;q=0.9,en;q=0.8",
    "es-ES,es;q=0.9,en;q=0.8",
    "it-IT,it;q=0.9,en;q=0.8",
    "pt-BR,pt;q=0.9,en;q=0.8",
    "ja-JP,ja;q=0.9,en;q=0.8",
    "ko-KR,ko;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-TW,zh;q=0.9,en;q=0.8",
    "ru-RU,ru;q=0.9,en;q=0.8",
    "ar-SA,ar;q=0.9,en;q=0.8",
    "hi-IN,hi;q=0.9,en;q=0.8",
    "nl-NL,nl;q=0.9,en;q=0.8",
    "pl-PL,pl;q=0.9,en;q=0.8",
    "tr-TR,tr;q=0.9,en;q=0.8",
    "sv-SE,sv;q=0.9,en;q=0.8",
    "da-DK,da;q=0.9,en;q=0.8",
    "fi-FI,fi;q=0.9,en;q=0.8",
    "no-NO,no;q=0.9,en;q=0.8",
    "cs-CZ,cs;q=0.9,en;q=0.8",
    "hu-HU,hu;q=0.9,en;q=0.8",
    "ro-RO,ro;q=0.9,en;q=0.8",
    "sk-SK,sk;q=0.9,en;q=0.8",
    "bg-BG,bg;q=0.9,en;q=0.8",
    "hr-HR,hr;q=0.9,en;q=0.8",
    "sr-RS,sr;q=0.9,en;q=0.8",
    "sl-SI,sl;q=0.9,en;q=0.8",
    "et-EE,et;q=0.9,en;q=0.8",
    "lv-LV,lv;q=0.9,en;q=0.8",
    "lt-LT,lt;q=0.9,en;q=0.8",
    "el-GR,el;q=0.9,en;q=0.8",
    "he-IL,he;q=0.9,en;q=0.8",
    "th-TH,th;q=0.9,en;q=0.8",
    "vi-VN,vi;q=0.9,en;q=0.8",
    "id-ID,id;q=0.9,en;q=0.8",
    "ms-MY,ms;q=0.9,en;q=0.8",
]

ACCEPTS = [
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
]

ACCEPT_ENCODINGS = [
    "gzip, deflate, br, zstd",
    "gzip, deflate, br",
    "gzip, deflate",
    "br, gzip, deflate",
]

CACHE_CONTROLS = [
    "max-age=0",
    "no-cache",
    "no-store",
    "max-age=0, must-revalidate",
]

SEC_CH_UA = [
    '"Google Chrome";v="127", "Chromium";v="127", "Not=A?Brand";v="24"',
    '"Google Chrome";v="126", "Chromium";v="126", "Not/A?Brand";v="24"',
    '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
    '"Microsoft Edge";v="127", "Chromium";v="127", "Not=A?Brand";v="24"',
    '"Not=A?Brand";v="24", "Chromium";v="127"',
]


def realistic_headers(referer) -> dict:
    ua = random.choice(USER_AGENTS)
    is_chrome = "Chrome" in ua and "Edg" not in ua and "OPR" not in ua and "SamsungBrowser" not in ua
    is_edge = "Edg" in ua
    is_firefox = "Firefox" in ua
    is_safari = "Safari" in ua and "Chrome" not in ua
    is_mobile = "Mobile" in ua or "Android" in ua or "iPhone" in ua or "iPad" in ua
    
    headers = {
        "Referer": referer,
        "User-Agent": ua,
        "Accept-Language": random.choice(ACCEPT_LANGS),
        "Accept": random.choice(ACCEPTS),
        "Accept-Encoding": random.choice(ACCEPT_ENCODINGS),
        "Cache-Control": random.choice(CACHE_CONTROLS),
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site" if referer else "none",
        "Sec-Fetch-User": "?1",
        "X-Forwarded-For": f"{random.randint(1,255)}.{random.randint(1,255)}.{random.randint(1,255)}.{random.randint(1,255)}",
    }
    
    # Chrome/Edge specific headers
    if is_chrome or is_edge:
        headers["sec-ch-ua"] = random.choice(SEC_CH_UA)
        headers["sec-ch-ua-mobile"] = "?1" if is_mobile else "?0"
        headers["sec-ch-ua-platform"] = '"Android"' if "Android" in ua else ('"iOS"' if "iPhone" in ua or "iPad" in ua else ('"macOS"' if "Mac" in ua else '"Windows"'))
    
    # Firefox doesn't send sec-ch-ua
    if is_firefox:
        headers.pop("sec-ch-ua", None)
        headers.pop("sec-ch-ua-mobile", None)
        headers.pop("sec-ch-ua-platform", None)
    
    # Safari doesn't send sec-ch-ua either
    if is_safari:
        headers.pop("sec-ch-ua", None)
        headers.pop("sec-ch-ua-mobile", None)
        headers.pop("sec-ch-ua-platform", None)
    
    return headers


def send_clicks(headers: dict, BITLY_URL: str, proxy: str = None) -> int:
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        r = requests.get(BITLY_URL, headers=headers,
                         allow_redirects=True, timeout=10, proxies=proxies)
        print(f"  -> {r.status_code} -> {r.url}")
        return r.status_code
    except requests.RequestException as e:
        print(f"  -> ERROR: {e}")
        return -1

def get_proxy():
    return random.choice(PROXIES) if PROXIES else None

async def demo_job(task: Task, progress: Progress) -> str:
    total = task.count
    delay = task.delay
    mode = task.mode
    
    await progress(f"0/{total} on {task.platform} (delay: {delay}s, mode: {mode})")
    
    async def send_one():
        # Choose referer based on platform and mode
        if task.platform == "random":
            referer = random.choice(list(PLATFORM_URLS.values()))
        elif mode in ("random", "variety"):
            referer = random.choice(REFERERS)
        else:
            referer = PLATFORM_URLS[task.platform]
        
        headers = realistic_headers(referer)
        return await asyncio.to_thread(
             send_clicks, headers, task.link, get_proxy()
        )

    sem = asyncio.Semaphore(50)
    async def bounded_send():
        async with sem:
            return await send_one()

    done = 0
    for i in range(total):
        await bounded_send()
        done += 1
        
        update_interval = 1 if total <= 20 else (5 if total <= 100 else 50)
        if done % update_interval == 0 or done == total:
            pct = int(done / total * 100)
            await progress(f"{done}/{total} ({pct}%) on {task.platform} (delay: {delay}s, mode: {mode})")
        
        if i < total - 1 and delay > 0:
            await asyncio.sleep(delay)

    return f"demo finished: {total} items for {task.link} ({task.platform}) @ {delay}s delay, mode: {mode}"


for _platform in set(PLATFORMS.values()):
    handler(_platform)(demo_job)

# "random" uses the same handler - it picks random platform per request
handler("random")(demo_job)
