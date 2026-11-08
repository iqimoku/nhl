# -*- coding: utf-8 -*-
"""
Bramkarze: siła (skuteczność obron) i przewidywany starter.

API NHL nie publikuje potwierdzonego bramkarza przed meczem. Dlatego:
  - na dziś model PRZEWIDUJE startera: kto bronił najczęściej w ostatnich 10 meczach drużyny,
    a przy meczu dzień po dniu (back-to-back), gdy pierwszy bramkarz bronił wczoraj – zmiennik;
  - w backteście (mecze z przeszłości) bierzemy FAKTYCZNEGO startera z dziennika meczów;
  - w raporcie można ręcznie wybrać bramkarza – procenty przeliczają się w przeglądarce.

Wpływ na model: współczynnik obrony rywala (gole tracone przez drużynę) zawiera „średniego” bramkarza
tej drużyny. Podmieniamy go na dzisiejszego:  rel = siła_dzisiejszego / średnia_siła_bramkarzy_drużyny,
gdzie siła = (1 − skuteczność) / (1 − średnia ligi), czyli ile goli puszcza względem przeciętnego.
"""
from __future__ import annotations

import datetime as dt

GOALIE_SEASON_WEIGHTS = [1.00, 0.70, 0.45]   # bieżący sezon, -1, -2
LEAGUE_SV = 0.901                            # średnia skuteczność obron w lidze
SV_PRIOR = 0.897                             # do czego ściągamy bramkarzy z małą próbą
SV_PRIOR_SHOTS = 1200                        # "wirtualne" strzały wygładzania (skuteczność stabilizuje się wolno)
REL_CLAMP = (0.80, 1.25)
LAST_N = 10


def _nm(o):
    if isinstance(o, dict):
        return o.get("default") or next(iter(o.values()), "")
    return o or ""


def _toi(s):
    if not s or ":" not in str(s):
        return 0.0
    m, sec = str(s).split(":")[:2]
    try:
        return int(m) + int(sec) / 60
    except ValueError:
        return 0.0


def roster(http, api, team, season):
    path = "current" if season is None else str(season)
    data = http.get(f"{api}/roster/{team}/{path}", ttl_hours=6.0) or {}
    return [{"id": g["id"], "name": f"{_nm(g.get('firstName'))} {_nm(g.get('lastName'))}".strip()}
            for g in data.get("goalies", []) or []]


def game_log(http, api, pid, seasons, live_season):
    out = []
    for sidx, s in enumerate(seasons):
        ttl = 6.0 if s >= live_season else None
        data = http.get(f"{api}/player/{pid}/game-log/{s}/2", ttl_hours=ttl) or {}
        for g in data.get("gameLog", []) or []:
            toi = _toi(g.get("toi"))
            started = g.get("gamesStarted")
            started = bool(started) if started is not None else toi >= 30
            out.append({
                "date": g.get("gameDate", ""), "sidx": sidx, "team": g.get("teamAbbrev"),
                "started": started, "sa": int(g.get("shotsAgainst") or 0),
                "ga": int(g.get("goalsAgainst") or 0), "toi": toi,
            })
    out.sort(key=lambda x: x["date"])
    return out


def strength(log, cutoff):
    """Skuteczność obron ważona sezonami, wygładzona do SV_PRIOR."""
    sa = ga = 0.0
    raw_sa = raw_ga = 0
    for g in log:
        if g["date"] >= cutoff or g["sidx"] >= len(GOALIE_SEASON_WEIGHTS):
            continue
        w = GOALIE_SEASON_WEIGHTS[g["sidx"]]
        sa += w * g["sa"]
        ga += w * g["ga"]
        if g["sidx"] == 0:
            raw_sa += g["sa"]
            raw_ga += g["ga"]
    saves = sa - ga
    sv = (saves + SV_PRIOR * SV_PRIOR_SHOTS) / (sa + SV_PRIOR_SHOTS)
    f = (1 - sv) / (1 - LEAGUE_SV)
    sv_cur = (raw_sa - raw_ga) / raw_sa if raw_sa else None
    return sv, f, sv_cur, int(raw_sa)


def team_info(http, api, team, day: dt.date, seasons, live_season, historical: bool):
    """Bramkarze drużyny na dany dzień: siła, starty w ost. 10 meczach, przewidywany / faktyczny starter."""
    season = seasons[0]
    gl = roster(http, api, team, season if historical else None)
    cutoff = day.isoformat()
    yesterday = (day - dt.timedelta(days=1)).isoformat()
    logs = {}
    for g in gl:
        try:
            logs[g["id"]] = [x for x in game_log(http, api, g["id"], seasons[:len(GOALIE_SEASON_WEIGHTS)], live_season)]
        except Exception:
            logs[g["id"]] = []

    # mecze drużyny (wg dzienników bramkarzy), w których bronił ktoś z tej drużyny
    def team_games(sidx):
        return sorted({x["date"] for L in logs.values() for x in L
                       if x["team"] == team and x["sidx"] == sidx and x["date"] < cutoff})
    cur_dates = team_games(0)
    window = cur_dates[-LAST_N:] if len(cur_dates) >= 3 else (team_games(1)[-LAST_N:] + cur_dates)[-LAST_N:]
    wset = set(window)
    b2b = yesterday in cur_dates

    out, total_starts = [], 0
    for g in gl:
        L = logs[g["id"]]
        sv, f, sv_cur, sa_cur = strength(L, cutoff)
        mine = [x for x in L if x["team"] == team and x["date"] < cutoff]
        starts10 = sum(1 for x in mine if x["started"] and x["date"] in wset)
        starts_season = sum(1 for x in mine if x["started"] and x["sidx"] == 0)
        last_start = max((x["date"] for x in mine if x["started"]), default="")
        started_today = any(x["date"] == cutoff and x["started"] and x["team"] == team for x in L)
        total_starts += starts_season
        out.append({"id": g["id"], "name": g["name"], "sv": round(sv, 4), "f": round(f, 4),
                    "sv_cur": round(sv_cur, 4) if sv_cur is not None else None, "sa_cur": sa_cur,
                    "starts10": starts10, "starts": starts_season, "last": last_start,
                    "today": started_today, "yday": any(x["date"] == yesterday and x["started"] for x in mine)})

    if not out:
        return None

    # średnia siła bramkarzy drużyny = ważona startami w sezonie (lub w oknie, gdy sezon dopiero się zaczął)
    wkey = "starts" if total_starts >= 3 else "starts10"
    wsum = sum(g[wkey] for g in out)
    avg = sum(g["f"] * g[wkey] for g in out) / wsum if wsum else sum(g["f"] for g in out) / len(out)

    actual = next((g for g in out if g["today"]), None)
    if actual:
        starter, how = actual, "faktyczny"
    else:
        ranked = sorted(out, key=lambda g: (g["starts10"], g["last"]), reverse=True)
        starter, how = ranked[0], "przewidywany"
        if b2b and starter["yday"] and len(ranked) > 1 and (ranked[1]["starts"] + ranked[1]["starts10"]) > 0:
            starter, how = ranked[1], "przewidywany (mecz dzień po dniu – zmiennik)"

    for g in out:
        g["rel"] = round(min(REL_CLAMP[1], max(REL_CLAMP[0], g["f"] / avg)), 4)
        for k in ("today", "yday"):
            g.pop(k, None)
    out.sort(key=lambda g: (-g["starts10"], -g["starts"]))
    return {"avg": round(avg, 4), "starter": starter["id"], "how": how, "b2b": b2b, "list": out}


def rel_of(info, goalie_id=None):
    if not info:
        return 1.0, None
    gid = goalie_id or info["starter"]
    g = next((x for x in info["list"] if x["id"] == gid), None)
    return (g["rel"], g) if g else (1.0, None)
