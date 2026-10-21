"""Manual probe: which egress route gets a runner through to stats.nba.com?

stats.nba.com drops connections from cloud IPs, so the runner tries Cloudflare
WARP and several Tor circuits (distinct SOCKS credentials = distinct circuits).
"""
import time

import requests
from nba_api.stats.endpoints import boxscoretraditionalv3, leaguegamefinder

ROUTES = {
    "direct": None,
    "warp": "socks5h://127.0.0.1:40000",
    **{f"tor-any#{i}": f"socks5h://c{i}:x@127.0.0.1:9150" for i in range(1, 4)},
    **{f"tor-us#{i}": f"socks5h://c{i}:x@127.0.0.1:9152" for i in range(1, 4)},
}
CDN = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")


def egress(proxy: str | None) -> str:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    deadline = time.time() + 120  # Tor may still be bootstrapping
    while True:
        try:
            info = requests.get("https://ipinfo.io/json", proxies=proxies, timeout=20).json()
            return f"{info.get('ip')} {info.get('country')} {info.get('org')}"
        except Exception as exc:  # noqa: BLE001
            if time.time() > deadline:
                return f"unreachable ({type(exc).__name__})"
            time.sleep(5)


def timed(fn):
    t = time.time()
    try:
        return f"OK {fn()} {time.time() - t:.1f}s", True
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__} {time.time() - t:.1f}s", False


for name, proxy in ROUTES.items():
    print(f"\n[{name}] egress: {egress(proxy)}", flush=True)
    msg, ok = timed(lambda: len(leaguegamefinder.LeagueGameFinder(
        date_from_nullable="10/20/2026", date_to_nullable="10/20/2026", league_id_nullable="00",
        proxy=proxy or "", timeout=25).get_data_frames()[0]))
    print(f"  stats leaguegamefinder: {msg}", flush=True)
    if ok:
        msg, _ = timed(lambda: len(boxscoretraditionalv3.BoxScoreTraditionalV3(
            game_id="0022600003", proxy=proxy or "", timeout=25).get_data_frames()[0]))
        print(f"  stats boxscoretraditionalv3: {msg}", flush=True)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    msg, _ = timed(lambda: requests.get(CDN, headers={"User-Agent": UA}, proxies=proxies,
                                        timeout=20).status_code)
    print(f"  cdn todaysScoreboard: {msg}", flush=True)
