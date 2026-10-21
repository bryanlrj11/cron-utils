"""Connectivity probe for stats.nba.com via nba_api's own headers (manual use)."""
import time

import requests
from nba_api.stats.endpoints import boxscoretraditionalv3, leaguegamefinder

print("runner ip:", requests.get("https://api.ipify.org", timeout=10).text)
t = time.time()
try:
    gf = leaguegamefinder.LeagueGameFinder(
        date_from_nullable="10/20/2026", date_to_nullable="10/20/2026",
        league_id_nullable="00", timeout=30).get_data_frames()[0]
    print(f"leaguegamefinder: {len(gf)} rows {time.time()-t:.1f}s", flush=True)
    t = time.time()
    box = boxscoretraditionalv3.BoxScoreTraditionalV3(
        game_id=gf["GAME_ID"].iloc[0], timeout=30).get_data_frames()[0]
    print(f"boxscoretraditionalv3: {len(box)} rows {time.time()-t:.1f}s", flush=True)
except Exception as exc:  # noqa: BLE001
    print(f"FAILED: {type(exc).__name__} {str(exc)[:150]} {time.time()-t:.1f}s", flush=True)
