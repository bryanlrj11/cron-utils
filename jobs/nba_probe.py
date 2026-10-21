"""Connectivity probe for NBA endpoints (manual use)."""
import time
import requests

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
     "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.5",
     "Referer": "https://www.nba.com/", "Origin": "https://www.nba.com",
     "x-nba-stats-origin": "stats", "x-nba-stats-token": "true"}
TESTS = {
    "stats scoreboardv2": "https://stats.nba.com/stats/scoreboardv2?GameDate=2026-10-20&LeagueID=00&DayOffset=0",
    "stats leaguegamelog": "https://stats.nba.com/stats/leaguegamelog?Counter=0&Direction=ASC&LeagueID=00&PlayerOrTeam=P&Season=2025-26&SeasonType=Regular+Season&Sorter=DATE",
    "cdn scoreboard": "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json",
    "cdn schedule": "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json",
    "www.nba.com": "https://www.nba.com/",
}
print("runner ip:", requests.get("https://api.ipify.org", timeout=10).text)
for name, url in TESTS.items():
    t = time.time()
    try:
        r = requests.get(url, headers=H, timeout=30)
        print(f"{name}: {r.status_code} {time.time()-t:.1f}s {len(r.content)}B", flush=True)
    except Exception as exc:
        print(f"{name}: {type(exc).__name__} {time.time()-t:.1f}s", flush=True)
