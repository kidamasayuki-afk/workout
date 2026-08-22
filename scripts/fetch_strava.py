#!/usr/bin/env python3
"""Strava のライドとジムのセッションを取得して json を更新する。

環境変数:
  STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET / STRAVA_REFRESH_TOKEN

既に json にある activity は再取得しないので、通常の実行では
API 呼び出しは 1〜2 回で済む。
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://www.strava.com/api/v3"
RIDES_OUT = "rides.json"
SESSIONS_OUT = "sessions.json"

# 距離ゼロの室内バイクなどを除外するしきい値（メートル）
MIN_DISTANCE = 1000

RIDE_TYPES = {"Ride", "GravelRide", "MountainBikeRide", "VirtualRide"}

# 筋トレ側として拾う種目。距離の短いRideもここに落ちる（ジムのエアロバイクなど）
SESSION_LABEL = {
    "WeightTraining": "ウェイトトレーニング",
    "Workout": "ワークアウト",
    "Ride": "エアロバイク",
    "VirtualRide": "エアロバイク",
    "Elliptical": "クロストレーナー",
    "StairStepper": "ステアマスター",
    "Yoga": "ヨガ",
}

# パワーカーブとして出す区間（秒）
POWER_WINDOWS = [(5, "5秒"), (60, "1分"), (300, "5分"), (1200, "20分"), (3600, "1時間")]


def post(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def get(path, token, **params):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def get_token():
    for k in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN"):
        if not os.environ.get(k):
            sys.exit("環境変数 %s が設定されていません" % k)
    res = post(
        "https://www.strava.com/oauth/token",
        {
            "client_id": os.environ["STRAVA_CLIENT_ID"],
            "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
            "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        },
    )
    # Strava はリフレッシュトークンを更新して返すことがある。
    # その場合はログに出すので、Secrets を差し替える。
    new = res.get("refresh_token")
    if new and new != os.environ["STRAVA_REFRESH_TOKEN"]:
        print("::warning::リフレッシュトークンが更新されました。"
              "Secrets の STRAVA_REFRESH_TOKEN を次の値に差し替えてください: " + new)
    return res["access_token"]


def best_power(watts, seconds):
    """watts ストリーム（1秒間隔想定）から指定秒数の最大平均パワーを返す。"""
    n = len(watts)
    if n < seconds:
        return None
    total = sum(watts[:seconds])
    best = total
    for i in range(seconds, n):
        total += watts[i] - watts[i - seconds]
        if total > best:
            best = total
    return round(best / seconds)


def power_curve(token, activity_id):
    try:
        s = get("/activities/%s/streams" % activity_id, token,
                keys="watts", key_by_type="true")
    except Exception as e:
        print("  パワーストリーム取得に失敗: %s" % e)
        return None
    data = (s.get("watts") or {}).get("data")
    if not data:
        return None
    watts = [w if isinstance(w, (int, float)) else 0 for w in data]
    curve = {}
    for sec, label in POWER_WINDOWS:
        v = best_power(watts, sec)
        if v:
            curve[label] = v
    return curve or None


def build_ride(token, summary):
    """一覧の1件を詳細取得して、ライドの形に整える。"""
    d = get("/activities/%s" % summary["id"], token, include_all_efforts="false")
    start = d.get("start_date_local") or d["start_date"]
    o = {
        "id": d["id"],
        "date": start[:10],
        "km": round(d["distance"] / 1000, 1),
        "moving": d["moving_time"],
        "elapsed": d["elapsed_time"],
        "elev": round(d.get("total_elevation_gain") or 0),
        "spd": round((d["distance"] / d["moving_time"]) * 3.6, 1) if d["moving_time"] else 0,
    }
    if d.get("max_speed"):
        o["maxSpd"] = round(d["max_speed"] * 3.6, 1)
    if d.get("average_watts"):
        o["watts"] = round(d["average_watts"])
    if d.get("average_heartrate"):
        o["hr"] = round(d["average_heartrate"])
    if d.get("max_heartrate"):
        o["maxHr"] = round(d["max_heartrate"])
    if d.get("average_cadence"):
        o["cad"] = round(d["average_cadence"])
    if d.get("calories"):
        o["cal"] = round(d["calories"])
    if d.get("suffer_score"):
        o["re"] = round(d["suffer_score"])
    if d.get("pr_count"):
        o["prs"] = d["pr_count"]
    if d.get("device_watts"):
        curve = power_curve(token, d["id"])
        if curve:
            o["power"] = curve
    return o


def build_session(token, summary):
    """ジムのセッション。種目・重量はStravaに無いので、時間・カロリー・心拍だけ。"""
    d = get("/activities/%s" % summary["id"], token, include_all_efforts="false")
    start = d.get("start_date_local") or d["start_date"]
    stype = d.get("sport_type") or d.get("type") or ""
    o = {
        "id": d["id"],
        "date": start[:10],
        "start": start[11:16],
        "type": stype,
        "label": SESSION_LABEL.get(stype, stype),
        "min": round((d.get("elapsed_time") or 0) / 60),
    }
    if d.get("calories"):
        o["cal"] = round(d["calories"])
    if d.get("average_heartrate"):
        o["hr"] = round(d["average_heartrate"])
    return o


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")


def main():
    rides = load(RIDES_OUT)
    sessions = load(SESSIONS_OUT)
    known = {r.get("id") for r in rides + sessions if r.get("id")}

    token = get_token()

    # 直近90日ぶんだけ見る。それ以前は既に json に入っている前提。
    after = int((datetime.now(timezone.utc) - timedelta(days=90)).timestamp())
    listed = get("/athlete/activities", token, per_page=100, after=after)

    new_rides = new_sessions = 0
    for a in listed:
        if a["id"] in known:
            continue
        stype = a.get("sport_type") or a.get("type") or ""
        dist = a.get("distance") or 0
        when = a["start_date_local"][:16].replace("T", " ")

        if stype in RIDE_TYPES and dist >= MIN_DISTANCE:
            print("ライド: %s %s (%.1fkm)" % (when, a["name"], dist / 1000))
            rides.append(build_ride(token, a))
            new_rides += 1
        elif stype in SESSION_LABEL:
            print("セッション: %s %s" % (when, a["name"]))
            sessions.append(build_session(token, a))
            new_sessions += 1
        else:
            print("対象外のためスキップ: %s %s (%s)" % (when, a["name"], stype))

    if new_rides:
        rides.sort(key=lambda r: (r["date"], r.get("id") or 0))
        save(RIDES_OUT, rides)
    if new_sessions:
        sessions.sort(key=lambda r: (r["date"], r.get("start") or ""))
        save(SESSIONS_OUT, sessions)

    print("ライド %d件追加（合計 %d）／セッション %d件追加（合計 %d）"
          % (new_rides, len(rides), new_sessions, len(sessions)))


if __name__ == "__main__":
    main()
