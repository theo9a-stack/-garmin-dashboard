#!/usr/bin/env python3
"""
Pulls today's Garmin Connect data, recalculates recovery and training load
using our own formulas (not just Garmin's built-in scores), writes a
morning or evening brief, and updates the JSON files the dashboard reads.

Run with:
    python3 scripts/fetch_and_brief.py --mode morning
    python3 scripts/fetch_and_brief.py --mode evening

Environment variables expected:
    GARMIN_TOKENS_B64   base64 tar.gz of a ~/.garminconnect token dir
                        (created by generate_login_token.py)
    GARMIN_EMAIL        fallback credential if tokens have expired
    GARMIN_PASSWORD     fallback credential if tokens have expired
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import statistics
import tarfile
from datetime import date, datetime, timedelta
from pathlib import Path

from garminconnect import Garmin

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "docs" / "data"
HISTORY_FILE = DATA_DIR / "history.json"
LATEST_FILE = DATA_DIR / "latest.json"
TOKEN_DIR = Path.home() / ".garminconnect"

CONFIG_FILE = REPO_ROOT / "config.json"
DEFAULT_CONFIG = {
    "resting_hr_fallback": 55,
    "max_hr": 190,
    "sex": "male",
    "timezone": "Europe/Berlin",
}


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text())
        return {**DEFAULT_CONFIG, **cfg}
    return DEFAULT_CONFIG


def restore_tokens_from_env() -> None:
    """Decode GARMIN_TOKENS_B64 into ~/.garminconnect if not already present."""
    if TOKEN_DIR.exists():
        return
    encoded = os.environ.get("GARMIN_TOKENS_B64")
    if not encoded:
        return
    raw = base64.b64decode(encoded)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        tar.extractall(Path.home(), filter="data")


def login() -> Garmin:
    restore_tokens_from_env()
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    client = Garmin(email, password)
    try:
        client.login(str(TOKEN_DIR))
    except Exception as exc:  # noqa: BLE001 - token load failed, try fresh login
        if not (email and password):
            raise RuntimeError(
                "Cached tokens failed to load and no GARMIN_EMAIL/GARMIN_PASSWORD "
                "fallback is set. Re-run generate_login_token.py and update the "
                "GARMIN_TOKENS_B64 secret."
            ) from exc
        client.login(str(TOKEN_DIR))
    return client


# --------------------------------------------------------------------------
# Data fetching
# --------------------------------------------------------------------------

def safe(fn, *args, default=None):
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - many Garmin endpoints 404 if no data yet
        return default


def fetch_today(client: Garmin, today: str) -> dict:
    stats = safe(client.get_stats, today, default={}) or {}
    hrv = safe(client.get_hrv_data, today, default={}) or {}
    sleep = safe(client.get_sleep_data, today, default={}) or {}
    battery = safe(client.get_body_battery, today, today, default=[]) or []
    activities = safe(client.get_activities_by_date, today, today, default=[]) or []

    hrv_summary = (hrv or {}).get("hrvSummary", {}) or {}
    sleep_dto = (sleep or {}).get("dailySleepDTO", {}) or {}

    battery_values = []
    if battery and isinstance(battery, list) and battery[0].get("bodyBatteryValuesArray"):
        battery_values = [v[1] for v in battery[0]["bodyBatteryValuesArray"] if v[1] is not None]

    parsed_activities = []
    for act in activities:
        duration_min = (act.get("duration") or 0) / 60
        avg_hr = act.get("averageHR")
        parsed_activities.append(
            {
                "name": act.get("activityName"),
                "type": (act.get("activityType") or {}).get("typeKey"),
                "duration_min": round(duration_min, 1),
                "avg_hr": avg_hr,
            }
        )

    return {
        "date": today,
        "resting_hr": stats.get("restingHeartRate"),
        "hrv_avg_ms": hrv_summary.get("lastNightAvg"),
        "hrv_status": hrv_summary.get("status"),
        "sleep_score": ((sleep_dto.get("sleepScores") or {}).get("overall") or {}).get("value"),
        "sleep_duration_h": round((sleep_dto.get("sleepTimeSeconds") or 0) / 3600, 2),
        "sleep_deep_min": round((sleep_dto.get("deepSleepSeconds") or 0) / 60),
        "sleep_light_min": round((sleep_dto.get("lightSleepSeconds") or 0) / 60),
        "sleep_rem_min": round((sleep_dto.get("remSleepSeconds") or 0) / 60),
        "sleep_awake_min": round((sleep_dto.get("awakeSleepSeconds") or 0) / 60),
        "body_battery_wake": battery_values[0] if battery_values else None,
        "body_battery_high": max(battery_values) if battery_values else None,
        "body_battery_low": min(battery_values) if battery_values else None,
        "body_battery_current": battery_values[-1] if battery_values else None,
        "steps": stats.get("totalSteps"),
        "stress_avg": stats.get("averageStressLevel"),
        "calories_total": stats.get("totalKilocalories"),
        "calories_active": stats.get("activeKilocalories"),
        "intensity_minutes_moderate": stats.get("moderateIntensityMinutes"),
        "intensity_minutes_vigorous": stats.get("vigorousIntensityMinutes"),
        "activities": parsed_activities,
    }


# --------------------------------------------------------------------------
# History storage
# --------------------------------------------------------------------------

def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return []


def save_history(history: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    history.sort(key=lambda r: r["date"])
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def upsert_today(history: list[dict], record: dict) -> list[dict]:
    history = [r for r in history if r["date"] != record["date"]]
    history.append(record)
    return history


# --------------------------------------------------------------------------
# Custom recovery score
# --------------------------------------------------------------------------

def zscore(value, series: list[float]) -> float:
    series = [s for s in series if s is not None]
    if value is None or len(series) < 5:
        return 0.0
    mean = statistics.mean(series)
    stdev = statistics.pstdev(series) or 1.0
    return (value - mean) / stdev


def compute_recovery_score(today: dict, history: list[dict]) -> dict:
    prior = [r for r in history if r["date"] < today["date"]][-30:]
    hrv_series = [r.get("hrv_avg_ms") for r in prior]
    rhr_series = [r.get("resting_hr") for r in prior]

    # Base weights when every metric is available (sums to 50, giving a
    # possible +/-50 swing around the 50 midpoint -> full 0-100 range).
    weights_base = {"hrv": 15, "rhr": 15, "sleep": 10, "battery": 10}
    contributions: dict[str, float] = {}

    if today.get("hrv_avg_ms") is not None:
        z = max(-2.0, min(2.0, zscore(today.get("hrv_avg_ms"), hrv_series)))
        contributions["hrv"] = z / 2.0  # normalize to -1..1

    if today.get("resting_hr") is not None:
        z = max(-2.0, min(2.0, -zscore(today.get("resting_hr"), rhr_series)))
        contributions["rhr"] = z / 2.0

    if today.get("sleep_score") is not None:
        contributions["sleep"] = max(-1.0, min(1.0, (today["sleep_score"] - 50) / 50))

    if today.get("body_battery_high") is not None:
        contributions["battery"] = max(-1.0, min(1.0, (today["body_battery_high"] - 50) / 50))

    if not contributions:
        return {"score": None, "band": "no data", "components_used": []}

    # Redistribute weight: whatever's missing doesn't silently count as
    # neutral, its share gets reallocated across whatever data IS present.
    total_available_weight = sum(weights_base[k] for k in contributions)
    score = 50.0
    for key, contribution in contributions.items():
        adjusted_weight = weights_base[key] / total_available_weight * 50
        score += adjusted_weight * contribution
    score = max(0, min(100, round(score)))

    if score >= 75:
        band = "primed"
    elif score >= 55:
        band = "ready"
    elif score >= 35:
        band = "moderate"
    else:
        band = "compromised"

    return {
        "score": score,
        "band": band,
        "components_used": sorted(contributions.keys()),
    }


# --------------------------------------------------------------------------
# Custom training load (Banister TRIMP + ACWR)
# --------------------------------------------------------------------------

def trimp_for_activity(duration_min: float, avg_hr, resting_hr: float, max_hr: float, sex: str) -> float:
    if not avg_hr or max_hr <= resting_hr:
        return 0.0
    hrr = (avg_hr - resting_hr) / (max_hr - resting_hr)
    hrr = max(0.0, min(1.0, hrr))
    k = 1.92 if sex == "male" else 1.67
    return round(duration_min * hrr * 0.64 * math.exp(k * hrr), 1)


def compute_daily_trimp(record: dict, config: dict) -> float:
    resting_hr = record.get("resting_hr") or config["resting_hr_fallback"]
    max_hr = config["max_hr"]
    total = 0.0
    for act in record.get("activities", []):
        total += trimp_for_activity(act["duration_min"], act.get("avg_hr"), resting_hr, max_hr, config["sex"])
    return round(total, 1)


def compute_load(history: list[dict], config: dict) -> dict:
    by_date = {r["date"]: compute_daily_trimp(r, config) for r in history}
    today = max(by_date) if by_date else None
    if not today:
        return {"daily_trimp": 0, "acute_7d": 0, "chronic_28d": 0, "acwr": None, "band": "no data"}

    ordered_dates = sorted(by_date)
    last7 = ordered_dates[-7:]
    last28 = ordered_dates[-28:]
    acute = round(sum(by_date[d] for d in last7) / len(last7), 1)
    chronic = round(sum(by_date[d] for d in last28) / len(last28), 1)
    acwr = round(acute / chronic, 2) if chronic > 0 else None

    if acwr is None:
        band = "building baseline"
    elif acwr < 0.8:
        band = "undertraining"
    elif acwr <= 1.3:
        band = "sweet spot"
    elif acwr <= 1.5:
        band = "caution"
    else:
        band = "high injury risk"

    return {
        "daily_trimp": by_date[today],
        "acute_7d": acute,
        "chronic_28d": chronic,
        "acwr": acwr,
        "band": band,
    }


# --------------------------------------------------------------------------
# Brief text
# --------------------------------------------------------------------------

RECOVERY_TEXT = {
    "primed": "Your body is well recovered — HRV and resting HR are both trending favorably.",
    "ready": "Recovery looks solid. Nothing is holding you back today.",
    "moderate": "Recovery is middling. Not a red flag, but don't be surprised if things feel average.",
    "compromised": "Recovery markers are down versus your recent baseline. Your body is asking for an easier day.",
    "no data": "No recovery data available yet today.",
}

LOAD_TEXT = {
    "undertraining": "Training load has dropped well below your recent normal — fine for a rest block, but fitness will start to slide if it continues.",
    "sweet spot": "Training load is right in your typical productive range.",
    "caution": "Training load has climbed faster than your body has adapted to. Worth being deliberate about intensity the next few days.",
    "high injury risk": "Load has spiked sharply versus your 4-week average. This is the range where overuse injuries and burnout become more likely.",
    "building baseline": "Not enough history yet to judge load trend — this improves after ~4 weeks of data.",
    "no data": "No activity data yet.",
}


def build_morning_brief(today: dict, recovery: dict, load: dict) -> str:
    lines = [
        f"Recovery: {recovery['score']}/100 ({recovery['band']}). {RECOVERY_TEXT[recovery['band']]}",
        f"Training load (ACWR {load['acwr']}): {load['band']}. {LOAD_TEXT[load['band']]}",
    ]
    if today.get("sleep_score") is not None:
        lines.append(f"Sleep score last night: {today['sleep_score']} ({today.get('sleep_duration_h')}h).")
    if today.get("body_battery_wake") is not None:
        lines.append(f"Woke up at {today['body_battery_wake']} body battery.")
    return " ".join(lines)


def build_evening_brief(today: dict, recovery: dict, load: dict) -> str:
    acts = today.get("activities", [])
    if acts:
        act_desc = "; ".join(
            f"{a['name'] or a['type']} — {a['duration_min']} min" + (f" @ {a['avg_hr']} bpm avg" if a.get("avg_hr") else "")
            for a in acts
        )
    else:
        act_desc = "no logged activity today"

    lines = [
        f"Today: {act_desc}. Added {load['daily_trimp']} TRIMP to your load.",
        f"Current ACWR {load['acwr']} ({load['band']}). {LOAD_TEXT[load['band']]}",
        f"Morning recovery was {recovery['score']}/100 ({recovery['band']}).",
    ]
    if today.get("body_battery_low") is not None:
        lines.append(f"Body battery dipped to {today['body_battery_low']} today.")
    return " ".join(lines)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["morning", "evening"], required=True)
    args = parser.parse_args()

    config = load_config()
    client = login()

    today_str = date.today().isoformat()
    today_record = fetch_today(client, today_str)

    history = load_history()
    history = upsert_today(history, today_record)

    recovery = compute_recovery_score(today_record, history)
    load = compute_load(history, config)

    today_record["recovery"] = recovery
    today_record["load"] = load
    history = upsert_today(history, today_record)
    save_history(history)

    brief_text = (
        build_morning_brief(today_record, recovery, load)
        if args.mode == "morning"
        else build_evening_brief(today_record, recovery, load)
    )

    latest = json.loads(LATEST_FILE.read_text()) if LATEST_FILE.exists() else {}
    latest["date"] = today_str
    latest["today"] = today_record
    latest[f"{args.mode}_brief"] = brief_text
    latest["last_mode"] = args.mode
    latest["last_brief"] = brief_text
    latest["updated_at"] = datetime.now().isoformat(timespec="minutes")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_FILE.write_text(json.dumps(latest, indent=2))

    print(f"[{args.mode}] {brief_text}")


if __name__ == "__main__":
    main()
