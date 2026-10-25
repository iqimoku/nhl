#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NHL – model szans zawodników na: gol, asysta, punkt (G+A), strzały.

Dane: oficjalne API NHL (api-web.nhle.com + api.nhle.com/stats).
Model: rozkład Poissona. Oczekiwana liczba zdarzeń (lambda) =
    tempo zawodnika na 60 min (3 poprzednie sezony + bieżący, ważone)
  x oczekiwany czas na lodzie
  x siła defensywna rywala (gole / strzały tracone vs średnia ligi)
  x mecz u siebie / na wyjeździe
  x korekta H2H (historia zawodnika przeciwko temu rywalowi, mocno "ściągnięta" do 1.0)

Użycie:
  python nhl_model.py                         # mecze z dzisiejszej daty (czas polski)
  python nhl_model.py --date 2026-10-24       # konkretny dzień
  python nhl_model.py --backtest 2025-11-01 2026-03-31   # sprawdzenie trafności na historii
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import sys
import threading
import time
import unicodedata
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import calib

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Brak biblioteki 'requests'. Zainstaluj: pip install -r requirements.txt")

try:
    from zoneinfo import ZoneInfo
    TZ_PL = ZoneInfo("Europe/Warsaw")
except Exception:  # brak tzdata na Windows
    TZ_PL = dt.timezone(dt.timedelta(hours=1), "CET")

API = "https://api-web.nhle.com/v1"
STATS = "https://api.nhle.com/stats/rest/en"

# ---------------------------------------------------------------- parametry modelu
SEASON_WEIGHTS = [1.00, 0.70, 0.45, 0.30]   # tempo zawodnika: bieżący sezon, -1, -2, -3
H2H_SEASONS = 6                              # historia przeciw rywalowi: bieżący + 5 poprzednich sezonów
RATE_SEASONS = len(SEASON_WEIGHTS)
FORM_GAMES = 10                              # ostatnie N meczów dostaje dodatkową wagę
FORM_BOOST = 1.5
PRIOR_MINUTES = 250.0                        # "wirtualne" minuty średniego gracza (stabilizacja małych prób)
PRIOR_RATE_60 = {                            # średnie tempo na 60 min wg pozycji
    "F": {"g": 0.80, "a": 1.10, "p": 1.90, "s": 7.2},
    "D": {"g": 0.25, "a": 0.85, "p": 1.10, "s": 4.6},
}
PRIOR_TOI = {"F": 14.5, "D": 19.0}
HOME_FACTOR = {True: 1.03, False: 0.97}
OPP_SHRINK_GP = 15                           # ile meczów rywala potrzeba, by jego bieżąca forma "ważyła" 50%
H2H_PSEUDO = {"g": 1.5, "a": 1.5, "p": 2.5, "s": 8.0}
H2H_CLAMP = (0.80, 1.25)
MARKETS = ("g", "a", "p", "s")
SHOT_MODEL_WEIGHT = 0.70                     # gole: 70% ze strzałów × skuteczność, 30% z samych goli
SH_PRIOR = {"F": 0.105, "D": 0.050}          # typowa skuteczność strzałów wg pozycji
SH_PRIOR_SHOTS = 120                         # "wirtualne" strzały do wygładzenia skuteczności
LUCK_GAMES = 15                              # okno wskaźnika pech / fart
CALIB = None                                 # kalibracja z backtestu (calibration.json), ładowana w main()


# ---------------------------------------------------------------- HTTP + cache
class Http:
    def __init__(self, cache_dir: Path):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()

    def _session(self):
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = "nhl-player-model/1.0"
            self._local.s = s
        return s

    def get(self, url: str, ttl_hours: float | None = 3.0):
        """ttl_hours=None -> cache na zawsze (zakończone sezony)."""
        p = self.cache / (hashlib.sha1(url.encode()).hexdigest() + ".json")
        if p.exists():
            age_h = (time.time() - p.stat().st_mtime) / 3600
            if ttl_hours is None or age_h < ttl_hours:
                try:
                    return json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
        last_err = None
        for attempt in range(5):
            try:
                r = self._session().get(url, timeout=25)
            except requests.RequestException as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 404:
                data = None
                break
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {r.status_code}"
                time.sleep(2.0 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            break
        else:
            raise RuntimeError(f"Nie udało się pobrać {url}: {last_err}")
        p.write_text(json.dumps(data), encoding="utf-8")
        return data


# ---------------------------------------------------------------- pomocnicze
def season_of(d: dt.date) -> int:
    y = d.year if d.month >= 8 else d.year - 1
    return y * 10000 + (y + 1)


def season_minus(season: int, k: int) -> int:
    y = season // 10000 - k
    return y * 10000 + (y + 1)


def season_label(season: int) -> str:
    return f"{season // 10000}/{str(season % 10000)[2:]}"


def parse_toi(s) -> float:
    if not s or not isinstance(s, str) or ":" not in s:
        return 0.0
    m, sec = s.split(":")[:2]
    try:
        return int(m) + int(sec) / 60.0
    except ValueError:
        return 0.0


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return " ".join(s.lower().replace(".", "").split())


def nm(obj, key="default"):
    if isinstance(obj, dict):
        return obj.get(key) or next(iter(obj.values()), "")
    return obj or ""


def pois_ge(lam: float, k: int) -> float:
    """P(X >= k) dla Poissona."""
    if lam <= 0:
        return 0.0
    term, cdf = math.exp(-lam), 0.0
    for i in range(k):
        if i > 0:
            term *= lam / i
        cdf += term
    return max(0.0, min(1.0, 1.0 - cdf))


# ---------------------------------------------------------------- pobieranie danych
def get_schedule(http: Http, day: dt.date):
    data = http.get(f"{API}/schedule/{day.isoformat()}", ttl_hours=1.0) or {}
    for wk in data.get("gameWeek", []) or []:
        if wk.get("date") == day.isoformat():
            return [g for g in wk.get("games", []) if g.get("gameType") in (2, 3)]
    return []


def get_roster(http: Http, team: str, season: int | None):
    path = "current" if season is None else str(season)
    data = http.get(f"{API}/roster/{team}/{path}", ttl_hours=6.0) or {}
    out = []
    for grp, posg in (("forwards", "F"), ("defensemen", "D")):
        for p in data.get(grp, []) or []:
            out.append({
                "id": p["id"],
                "name": f"{nm(p.get('firstName'))} {nm(p.get('lastName'))}".strip(),
                "num": p.get("sweaterNumber"),
                "pos": p.get("positionCode", posg),
                "posg": posg,
                "team": team,
                "birth": p.get("birthDate", ""),
            })
    return out


def get_player_games(http: Http, pid: int, seasons: list[int], live_season: int):
    games = []
    for sidx, s in enumerate(seasons):
        ttl = 6.0 if s >= live_season else None
        data = http.get(f"{API}/player/{pid}/game-log/{s}/2", ttl_hours=ttl) or {}
        for g in data.get("gameLog", []) or []:
            toi = parse_toi(g.get("toi"))
            if toi <= 0:
                continue
            goals, assists = int(g.get("goals") or 0), int(g.get("assists") or 0)
            games.append({
                "date": g.get("gameDate", ""),
                "sidx": sidx,
                "team": g.get("teamAbbrev"),
                "opp": g.get("opponentAbbrev"),
                "home": g.get("homeRoadFlag") == "H",
                "g": goals, "a": assists, "p": goals + assists,
                "s": int(g.get("shots") or 0),
                "toi": toi,
            })
    games.sort(key=lambda x: x["date"])
    return games


def get_team_factors(http: Http, as_of: dt.date, season: int):
    """Współczynniki defensywne rywali: ga = gole tracone, sa = strzały tracone (1.0 = średnia ligi)."""
    live = season >= season_of(dt.date.today())

    def standings_ratio(day: dt.date, ttl):
        data = http.get(f"{API}/standings/{day.isoformat()}", ttl_hours=ttl) or {}
        rows = data.get("standings", []) or []
        tot_ga = sum(r.get("goalAgainst", 0) or 0 for r in rows)
        tot_gp = sum(r.get("gamesPlayed", 0) or 0 for r in rows)
        res, names = {}, {}
        for r in rows:
            ab = nm(r.get("teamAbbrev"))
            gp = r.get("gamesPlayed", 0) or 0
            names[norm_name(nm(r.get("teamName")))] = ab
            if gp and tot_gp and tot_ga:
                res[ab] = ((r.get("goalAgainst", 0) / gp) / (tot_ga / tot_gp), gp)
        return res, names

    def shots_ratio(s: int, names: dict, ttl):
        url = f"{STATS}/team/summary?cayenneExp=seasonId={s}%20and%20gameTypeId=2"
        try:
            data = http.get(url, ttl_hours=ttl) or {}
        except Exception:
            return {}
        rows = data.get("data", []) or []
        vals = [r.get("shotsAgainstPerGame") for r in rows if r.get("shotsAgainstPerGame")]
        if not vals:
            return {}
        avg = sum(vals) / len(vals)
        res = {}
        for r in rows:
            ab = names.get(norm_name(r.get("teamFullName", "")))
            if ab and r.get("shotsAgainstPerGame"):
                res[ab] = (r["shotsAgainstPerGame"] / avg, r.get("gamesPlayed", 0) or 0)
        return res

    prev = season_minus(season, 1)
    prev_end = dt.date(prev % 10000, 5, 1)
    cur_ga, names = standings_ratio(as_of - dt.timedelta(days=1), 3.0 if live else None)
    prev_ga, prev_names = standings_ratio(prev_end, None)
    names = {**prev_names, **names}
    cur_sa = shots_ratio(season, names, 6.0 if live else None)
    prev_sa = shots_ratio(prev, names, None)

    teams = set(cur_ga) | set(prev_ga) | set(names.values())
    out = {}
    for t in teams:
        f = {}
        for key, cur, prv in (("ga", cur_ga, prev_ga), ("sa", cur_sa, prev_sa)):
            prior = 0.5 + 0.5 * prv.get(t, (1.0, 0))[0]          # zeszły sezon, ściągnięty do średniej
            ratio, gp = cur.get(t, (prior, 0))
            f[key] = (gp * ratio + OPP_SHRINK_GP * prior) / (gp + OPP_SHRINK_GP)
        out[t] = f
    return out


# ---------------------------------------------------------------- model
def project(games, as_of: dt.date, posg: str, opp: str, is_home: bool, opp_f: dict):
    cutoff = as_of.isoformat()
    hist_all = [g for g in games if g["date"] < cutoff]            # do H2H (do 6 sezonów)
    hist = [g for g in hist_all if g["sidx"] < RATE_SEASONS]       # do tempa i czasu gry
    prior = PRIOR_RATE_60[posg]
    recent = hist[-FORM_GAMES:]
    recent_ids = {id(g) for g in recent}

    sums = {k: 0.0 for k in MARKETS}
    mins = wsum = 0.0
    for g in hist:
        w = SEASON_WEIGHTS[g["sidx"]] * (FORM_BOOST if id(g) in recent_ids else 1.0)
        for k in MARKETS:
            sums[k] += w * g[k]
        mins += w * g["toi"]
        wsum += w
    r60 = {k: (sums[k] + prior[k] * PRIOR_MINUTES / 60) / ((mins + PRIOR_MINUTES) / 60) for k in MARKETS}

    # Gole ze strzałów: tempo strzałów × skuteczność z całej dostępnej kariery (wygładzona do średniej pozycji).
    # Zawodnik, który strzela dużo, a ostatnio "nie wpada", dostaje uczciwie wyższą szansę – i odwrotnie.
    sh = shooting_pct(hist_all, posg)
    r60_g_raw = r60["g"]
    r60["g"] = SHOT_MODEL_WEIGHT * r60["s"] * sh + (1 - SHOT_MODEL_WEIGHT) * r60_g_raw
    r60["p"] = r60["g"] + r60["a"]

    if hist:
        toi_recent = sum(g["toi"] for g in recent) / len(recent)
        toi_weighted = mins / wsum
        n = len(hist)
        exp_toi = 0.6 * toi_recent + 0.4 * toi_weighted
        if n < 5:  # bardzo mała próba – miksuj z typowym czasem
            exp_toi = (n * exp_toi + (5 - n) * PRIOR_TOI[posg]) / 5
    else:
        exp_toi = PRIOR_TOI[posg]

    vs = [g for g in hist_all if g["opp"] == opp]
    h2h = {}
    for k in MARKETS:
        act = sum(g[k] for g in vs)
        exp = sum(r60[k] / 60 * g["toi"] for g in vs)
        c = H2H_PSEUDO[k]
        h2h[k] = min(H2H_CLAMP[1], max(H2H_CLAMP[0], (act + c) / (exp + c)))

    ha = HOME_FACTOR[is_home]
    res = {}
    for k in MARKETS:
        of = opp_f.get("sa" if k == "s" else "ga", 1.0)
        lam = r60[k] / 60 * exp_toi * of * ha * h2h[k]
        res[k] = {"lam": lam, "r60": r60[k], "h2h_f": h2h[k], "opp_f": of}
    res["g"]["sh"] = sh
    res["g"]["r60_raw"] = r60_g_raw
    return res, hist, vs, exp_toi


def shooting_pct(games, posg):
    g = sum(x["g"] for x in games)
    s = sum(x["s"] for x in games)
    return (g + SH_PRIOR[posg] * SH_PRIOR_SHOTS) / (s + SH_PRIOR_SHOTS)


def streaks(games):
    """Serie bez zdarzenia: bieżąca (od ostatniego meczu wstecz) i najdłuższa w dostępnej historii."""
    out = {}
    for k in ("g", "a", "p"):
        cur = 0
        for x in reversed(games):
            if x[k] > 0:
                break
            cur += 1
        best = run = 0
        for x in games:
            run = run + 1 if x[k] == 0 else 0
            best = max(best, run)
        out[k] = cur
        out[k + "_max"] = best
    s3 = 0
    for x in reversed(games):
        if x["s"] >= 3:
            break
        s3 += 1
    out["s"] = s3
    return out


def luck(games, sh, n=LUCK_GAMES):
    """Gole w ostatnich n meczach vs oczekiwane z liczby strzałów (strzały × skuteczność z kariery)."""
    last = games[-n:]
    shots = sum(x["s"] for x in last)
    goals = sum(x["g"] for x in last)
    xg = shots * sh
    return {"n": len(last), "g": goals, "s": shots, "xg": round(xg, 2), "diff": round(goals - xg, 2)}


def monthly(games):
    """[sezon, miesiąc, mecze, G, A] dla każdego miesiąca w dostępnej historii."""
    agg = {}
    for x in games:
        y, mth = int(x["date"][:4]), int(x["date"][5:7])
        s = season_label(season_of(dt.date(y, mth, 1)))
        a = agg.setdefault((s, mth), [0, 0, 0])
        a[0] += 1
        a[1] += x["g"]
        a[2] += x["a"]
    return [[s, mth, *v] for (s, mth), v in sorted(agg.items())]


def form_stats(games):
    """Sumy + w ilu meczach było zdarzenie (hg/ha/hp = min. 1, hs = min. 3 strzały)."""
    out = {"gp": len(games)}
    for k in MARKETS:
        out[k] = sum(g[k] for g in games)
        out["h" + k] = sum(1 for g in games if g[k] >= (3 if k == "s" else 1))
    return out


def build_row(pl, game, games, as_of, team_dates, factors, season):
    team, home_team, away_team = pl["team"], game["homeTeam"]["abbrev"], game["awayTeam"]["abbrev"]
    is_home = team == home_team
    opp = away_team if is_home else home_team
    proj, hist, vs, exp_toi = project(games, as_of, pl["posg"], opp, is_home, factors.get(opp, {}))

    cur = [g for g in hist if g["sidx"] == 0]
    last = cur[-1]["date"] if cur else ""
    missed = sum(1 for d in team_dates.get(team, ()) if d > last)
    if not cur and team_dates.get(team):
        status = "brak meczów w sezonie"
    elif missed >= 2:
        status = f"nie grał w {missed} ost. meczach"
    else:
        status = "OK"

    m = {}
    for k in MARKETS:
        lam_raw = proj[k]["lam"]
        lam = calib.apply(CALIB, k, lam_raw)
        e = {"lam": round(lam, 3), "lam_raw": round(lam_raw, 4), "r60": round(proj[k]["r60"], 2),
             "h2h_f": round(proj[k]["h2h_f"], 3), "opp_f": round(proj[k]["opp_f"], 3)}
        if k == "s":
            r_nb = CALIB["s"].get("r") if CALIB and "s" in CALIB else None
            for t in (1, 2, 3, 4, 5):
                e[f"p{t}"] = round(calib.nb_ge(lam, r_nb, t) if r_nb else pois_ge(lam, t), 4)
        else:
            e["p1"] = round(pois_ge(lam, 1), 4)
            e["p2"] = round(pois_ge(lam, 2), 4)
        m[k] = e

    all_hist = [g for g in games if g["date"] < as_of.isoformat()]
    sh = proj["g"]["sh"]

    start = game.get("startTimeUTC", "")
    try:
        t_pl = dt.datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(TZ_PL).strftime("%H:%M")
    except ValueError:
        t_pl = ""
    return {
        "id": pl["id"], "name": pl["name"], "num": pl["num"], "pos": pl["pos"], "posg": pl["posg"],
        "team": team, "opp": opp, "home": is_home,
        "gameId": game["id"], "game": f"{away_team} @ {home_team}", "time": t_pl,
        "status": status, "inactive": status != "OK",
        "gp": len(hist), "gp_cur": len(cur), "toi": round(exp_toi, 1),
        "f10": form_stats(hist[-FORM_GAMES:]), "h2h": form_stats(vs),
        # szczegóły do rozwijanej linii: [data, dom 1/0, drużyna, rywal, G, A, SOG, TOI], od najnowszych
        "h2h_log": [[g["date"], int(g["home"]), g["team"], g["opp"], g["g"], g["a"], g["s"], round(g["toi"], 1)]
                    for g in reversed(vs)],
        "f10_log": [[g["date"], int(g["home"]), g["team"], g["opp"], g["g"], g["a"], g["s"], round(g["toi"], 1)]
                    for g in reversed(hist[-FORM_GAMES:])],
        "sh": round(sh, 4), "streak": streaks(all_hist), "luck": luck(all_hist, sh),
        "months": monthly(all_hist),
        "m": m, "season": season,
    }


# ---------------------------------------------------------------- przebieg dzienny
def analyse_day(http: Http, day: dt.date, workers: int, historical_rosters=False, quiet=False):
    season = season_of(day)
    seasons = [season_minus(season, k) for k in range(max(H2H_SEASONS, RATE_SEASONS))]
    games = get_schedule(http, day)
    if not games:
        return [], [], {}
    factors = get_team_factors(http, day, season)

    players = []
    for g in games:
        for side in ("awayTeam", "homeTeam"):
            team = g[side]["abbrev"]
            for p in get_roster(http, team, season if historical_rosters else None):
                players.append((p, g))

    live = season_of(dt.date.today())
    logs, done = {}, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(get_player_games, http, p["id"], seasons, live): p["id"] for p, _ in players}
        for f in as_completed(futs):
            pid = futs[f]
            try:
                logs[pid] = f.result()
            except Exception as e:
                logs[pid] = []
                if not quiet:
                    print(f"  ! zawodnik {pid}: {e}", file=sys.stderr)
            done += 1
            if not quiet and (done % 25 == 0 or done == len(futs)):
                print(f"  pobrano dane zawodników: {done}/{len(futs)}", end="\r")
    if not quiet:
        print()

    cutoff = day.isoformat()
    team_dates: dict[str, set] = {}
    for p, _ in players:
        for gm in logs.get(p["id"], []):
            if gm["sidx"] == 0 and gm["team"] == p["team"] and gm["date"] < cutoff:
                team_dates.setdefault(p["team"], set()).add(gm["date"])

    rows = [build_row(p, g, logs.get(p["id"], []), day, team_dates, factors, season) for p, g in players]
    return rows, games, logs


# ---------------------------------------------------------------- wyjście
CSV_COLS = [
    ("Mecz", lambda r: r["game"]), ("Godzina PL", lambda r: r["time"]), ("Zawodnik", lambda r: r["name"]),
    ("Drużyna", lambda r: r["team"]), ("Rywal", lambda r: r["opp"]), ("Dom", lambda r: "tak" if r["home"] else "nie"),
    ("Poz", lambda r: r["pos"]), ("Status", lambda r: r["status"]), ("Mecze w próbie", lambda r: r["gp"]),
    ("Oczek. TOI", lambda r: r["toi"]),
    ("Gol %", lambda r: pct(r["m"]["g"]["p1"])), ("Gol kurs fair", lambda r: fair(r["m"]["g"]["p1"])),
    ("Gol oczek.", lambda r: r["m"]["g"]["lam"]),
    ("Asysta %", lambda r: pct(r["m"]["a"]["p1"])), ("Asysta kurs fair", lambda r: fair(r["m"]["a"]["p1"])),
    ("Asysta oczek.", lambda r: r["m"]["a"]["lam"]),
    ("Punkt %", lambda r: pct(r["m"]["p"]["p1"])), ("Punkt kurs fair", lambda r: fair(r["m"]["p"]["p1"])),
    ("2+ pkt %", lambda r: pct(r["m"]["p"]["p2"])),
    ("Strzały oczek.", lambda r: r["m"]["s"]["lam"]),
    ("Strzały 2+ %", lambda r: pct(r["m"]["s"]["p2"])), ("Strzały 3+ %", lambda r: pct(r["m"]["s"]["p3"])),
    ("Strzały 4+ %", lambda r: pct(r["m"]["s"]["p4"])),
    ("Forma10 G", lambda r: r["f10"]["g"]), ("Forma10 A", lambda r: r["f10"]["a"]),
    ("Forma10 SOG", lambda r: r["f10"]["s"]), ("Forma10 mecze", lambda r: r["f10"]["gp"]),
    ("Skuteczność kariera %", lambda r: pct(r["sh"])),
    ("Seria bez G", lambda r: r["streak"]["g"]), ("Seria bez A", lambda r: r["streak"]["a"]),
    ("Seria bez PKT", lambda r: r["streak"]["p"]), ("Najdłuższa seria bez G", lambda r: r["streak"]["g_max"]),
    ("Najdłuższa seria bez PKT", lambda r: r["streak"]["p_max"]),
    (f"Gole ost. {LUCK_GAMES}m", lambda r: r["luck"]["g"]), (f"Oczek. gole ost. {LUCK_GAMES}m", lambda r: r["luck"]["xg"]),
    ("Pech/fart G", lambda r: r["luck"]["diff"]),
    ("Forma10 mecze z golem",lambda r: r["f10"]["hg"]), ("Forma10 mecze z punktem", lambda r: r["f10"]["hp"]),
    ("H2H mecze", lambda r: r["h2h"]["gp"]), ("H2H G", lambda r: r["h2h"]["g"]),
    ("H2H mecze z golem", lambda r: r["h2h"]["hg"]),
    ("H2H A", lambda r: r["h2h"]["a"]), ("H2H mecze z asystą", lambda r: r["h2h"]["ha"]),
    ("H2H PKT", lambda r: r["h2h"]["p"]), ("H2H mecze z punktem", lambda r: r["h2h"]["hp"]),
    ("H2H SOG", lambda r: r["h2h"]["s"]), ("H2H mecze z 3+ SOG", lambda r: r["h2h"]["hs"]),
    ("Wsp. obrony rywala (gole)", lambda r: r["m"]["g"]["opp_f"]),
    ("Wsp. obrony rywala (strzały)", lambda r: r["m"]["s"]["opp_f"]),
]


def pct(p):
    return round(100 * p, 1)


def fair(p):
    return round(1 / p, 2) if p > 0.001 else ""


def write_csv(rows, path: Path):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow([c for c, _ in CSV_COLS])
        for r in sorted(rows, key=lambda r: (r["game"], -r["m"]["p"]["p1"])):
            w.writerow([str(fn(r)).replace(".", ",") if isinstance(fn(r), float) else fn(r) for _, fn in CSV_COLS])


def write_html(rows, games, day: dt.date, path: Path, root: str | None = None, csv_url: str | None = None,
               elc_url: str | None = None):
    """root – ścieżka do katalogu głównego strony (tryb GitHub Pages), None w trybie lokalnym."""
    tpl = (Path(__file__).with_name("report_template.html")).read_text(encoding="utf-8")
    season = season_of(day)
    meta = {
        "root": root,
        "csv": csv_url,
        "elc": elc_url,
        "date": day.isoformat(),
        "generated": dt.datetime.now(TZ_PL).strftime("%Y-%m-%d %H:%M"),
        "seasons": [season_label(season_minus(season, k)) for k in range(len(SEASON_WEIGHTS))],
        "weights": SEASON_WEIGHTS,
        "calib": (CALIB or {}).get("_info"),
        "h2h_from": season_label(season_minus(season, H2H_SEASONS - 1)),
        "games": [{
            "id": g["id"], "label": f'{g["awayTeam"]["abbrev"]} @ {g["homeTeam"]["abbrev"]}',
            "away": f'{nm(g["awayTeam"].get("placeName"))} {nm(g["awayTeam"].get("commonName"))}'.strip(),
            "home": f'{nm(g["homeTeam"].get("placeName"))} {nm(g["homeTeam"].get("commonName"))}'.strip(),
            "time": next((r["time"] for r in rows if r["gameId"] == g["id"]), ""),
        } for g in games],
    }
    payload = json.dumps({"meta": meta, "rows": rows}, ensure_ascii=False).replace("</", "<\\/")
    path.write_text(tpl.replace("__DATA__", payload), encoding="utf-8")


def write_site(rows, games, day: dt.date, site: Path, elc: bool = False):
    """Strona pod GitHub Pages: index.html = najnowszy dzień, d/RRRR-MM-DD.html = archiwum."""
    (site / "d").mkdir(parents=True, exist_ok=True)
    (site / ".nojekyll").touch()
    d = day.isoformat()
    elc = elc or (site / "elc.html").exists()
    write_csv(rows, site / "d" / f"{d}.csv")
    write_html(rows, games, day, site / "d" / f"{d}.html", root="../", csv_url=f"{d}.csv",
               elc_url="../elc.html" if elc else None)

    days_path = site / "days.json"
    try:
        days = json.loads(days_path.read_text(encoding="utf-8"))
    except Exception:
        days = []
    days = [x for x in days if x.get("date") != d] + [{"date": d, "games": len(games)}]
    days.sort(key=lambda x: x["date"], reverse=True)
    days_path.write_text(json.dumps(days, indent=0), encoding="utf-8")

    if days[0]["date"] == d:
        write_html(rows, games, day, site / "index.html", root="", csv_url=f"d/{d}.csv",
                   elc_url="elc.html" if elc else None)
    return site / "d" / f"{d}.html"


# ---------------------------------------------------------------- backtest
def backtest(http: Http, start: dt.date, end: dt.date, workers: int, out_dir: Path, base: Path, save_calib=True):
    """Zbiera SUROWE prognozy (bez kalibracji), uczy kalibrację na starszych danych,
    sprawdza ją na najnowszych (których nie widziała), a na koniec zapisuje kalibrację z całości."""
    import numpy as np
    global CALIB
    saved, CALIB = CALIB, None
    cols = {k: [] for k in ("date", "lam_g", "lam_a", "lam_p", "lam_s", "g", "a", "p", "s", "sg", "sp", "luck")}
    names = []
    day = start
    try:
        while day <= end:
            rows, games, logs = analyse_day(http, day, workers, historical_rosters=True, quiet=True)
            n_day = 0
            for r in rows:
                actual = next((g for g in logs.get(r["id"], []) if g["date"] == day.isoformat()
                               and g["team"] == r["team"]), None)
                if not actual:
                    continue  # nie zagrał
                n_day += 1
                cols["date"].append(day.isoformat())
                for k in MARKETS:
                    cols["lam_" + k].append(r["m"][k]["lam_raw"])
                    cols[k].append(actual[k])
                cols["sg"].append(r["streak"]["g"])
                cols["sp"].append(r["streak"]["p"])
                cols["luck"].append(r["luck"]["diff"])
                names.append((r["name"], r["team"], r["opp"]))
            print(f"  {day}: {len(games)} meczów, {n_day} zawodników ocenionych", flush=True)
            day += dt.timedelta(days=1)
    finally:
        CALIB = saved

    n = len(cols["date"])
    if not n:
        print("Brak danych do backtestu w tym zakresie.")
        return
    D = {k: (np.array(v) if k == "date" else np.array(v, dtype=float)) for k, v in cols.items()}

    # podział w czasie: kalibracja uczy się na 75% najstarszych dni, test na 25% najnowszych
    dates = sorted(set(cols["date"]))
    cut = dates[int(len(dates) * 0.75)] if len(dates) >= 8 else None
    if cut:
        tr = D["date"] < cut
        Dtr = {k: v[tr] for k, v in D.items()}
        Dte = {k: v[~tr] for k, v in D.items()}
        print(f"\nUczę kalibrację na {int(tr.sum())} występach (do {cut}), test na {int((~tr).sum())} (od {cut})...")
        cal_tr = calib.fit_all(Dtr)
        print("\n=== TEST NA DANYCH, KTÓRYCH KALIBRACJA NIE WIDZIAŁA ===")
        print("(najnowsze 25% okresu – uczciwe porównanie: przed vs po kalibracji)")
        evaluate(Dte, None, "PRZED kalibracją (surowy model)")
        evaluate(Dte, cal_tr, "PO kalibracji")

    print(f"\nUczę ostateczną kalibrację na wszystkich {n} występach...")
    cal = calib.fit_all(D)
    cal["_info"] = {"od": str(start), "do": str(end), "n": n,
                    "utworzono": dt.datetime.now(TZ_PL).strftime("%Y-%m-%d %H:%M")}
    print("Parametry: " + ", ".join(
        f"{k}: a={v['a']}, b={v['b']}" + (f", r={v['r']}" if "r" in v else "") for k, v in cal.items() if k != "_info"))
    if save_calib:
        calib.save(cal, base)
        print(f"Zapisano {base / calib.FILE_NAME} – raport dzienny będzie z niej korzystał.")

    evaluate(D, cal, "CAŁY OKRES – PO KALIBRACJI", detail_buckets=True)

    P = calib.probs(D, cal)
    report_streaks(list(zip(D["sg"], D["sp"], D["luck"], P["Gol 1+"][0], P["Punkt 1+"][0],
                            P["Gol 1+"][1], P["Punkt 1+"][1])))

    path = out_dir / f"backtest_{start}_{end}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Data", "Zawodnik", "Drużyna", "Rywal", "P(gol)", "Gole", "P(asysta)", "Asysty",
                    "P(punkt)", "Punkty", "Oczek. strzały (surowe)", "P(3+ SOG)", "Strzały"])
        for i in range(n):
            w.writerow([D["date"][i], *names[i],
                        round(P["Gol 1+"][0][i], 4), int(D["g"][i]), round(P["Asysta 1+"][0][i], 4), int(D["a"][i]),
                        round(P["Punkt 1+"][0][i], 4), int(D["p"][i]), round(D["lam_s"][i], 3),
                        round(P["Strzały 3+"][0][i], 4), int(D["s"][i])])
    print(f"\nSzczegóły zapisane: {path}")
    print("Poprawa = o ile model jest lepszy od zgadywania średniej ligowej (Brier skill). >0% = model wnosi wartość.")


def evaluate(D, cal, title, detail_buckets=True):
    import numpy as np
    P = calib.probs(D, cal)
    print(f"\n=== {title} ===")
    print(f"{'Rynek':<12}{'N':>8}{'Śr. prognoza':>14}{'Wynik real.':>13}{'Brier':>9}{'Brier bazowy':>14}{'Poprawa':>9}")
    for label, (p, y) in P.items():
        n = len(p)
        rate = y.mean()
        brier = float(np.mean((p - y) ** 2))
        basev = float(np.mean((rate - y) ** 2))
        skill = 1 - brier / basev if basev else 0
        print(f"{label:<12}{n:>8}{p.mean()*100:>13.1f}%{rate*100:>12.1f}%{brier:>9.4f}{basev:>14.4f}{skill*100:>8.1f}%")
    if not detail_buckets:
        return
    print("\nKalibracja (prognoza → faktycznie):")
    edges = [0, .1, .2, .3, .4, .5, .6, .7, .8, 1.01]
    for label, (p, y) in P.items():
        parts = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            m = (p >= lo) & (p < hi)
            if m.sum() < 30:
                continue
            parts.append(f"{p[m].mean()*100:4.0f}→{y[m].mean()*100:4.0f}%")
        print(f"  {label:<11} " + "  ".join(parts))


def report_streaks(recs):
    """Czy po serii zer / 'pechu' zawodnik trafia częściej, niż przewidział model?
    Wskaźnik = faktycznie / prognoza. ~1,00 = model już to uwzględnia, seria nic nie dodaje.
    Wyraźnie > 1 przy dużym N = warto dodać korektę za serię."""
    if not recs:
        return

    def table(title, idx_key, buckets, idx_p, idx_y):
        print(f"\n{title}")
        print(f"  {'Grupa':<22}{'N':>7}{'Prognoza':>11}{'Faktycznie':>12}{'Wskaźnik':>10}{'±':>8}")
        for lab, lo, hi in buckets:
            b = [x for x in recs if lo <= x[idx_key] < hi]
            if len(b) < 30:
                print(f"  {lab:<22}{len(b):>7}   (za mało danych)")
                continue
            n = len(b)
            p = sum(x[idx_p] for x in b) / n
            y = sum(x[idx_y] for x in b) / n
            se = (p * (1 - p) / n) ** 0.5 / p if p > 0 else 0   # błąd standardowy wskaźnika
            print(f"  {lab:<22}{n:>7}{p*100:>10.1f}%{y*100:>11.1f}%{y/p if p else 0:>10.2f}{1.96*se:>8.2f}")

    print("\n=== SERIE I PECH – CZY WARTO GRAĆ NA 'PRZEŁAMANIE'? ===")
    print("Wskaźnik = faktycznie / prognoza modelu. Jeśli 1,00 mieści się w przedziale ±, seria nic nie dodaje.")
    streak_b = [("0 (strzelił ost.)", 0, 1), ("1–2 mecze", 1, 3), ("3–4 mecze", 3, 5),
                ("5–7 meczów", 5, 8), ("8–12 meczów", 8, 13), ("13+ meczów", 13, 10**6)]
    table("Gol w meczu – wg bieżącej serii bez gola", 0, streak_b, 3, 5)
    table("Punkt w meczu – wg bieżącej serii bez punktu", 1, streak_b, 4, 6)
    luck_b = [("pech ≤ −3 G", -99, -3), ("pech −3…−1,5", -3, -1.5), ("−1,5…−0,5", -1.5, -0.5),
              ("w normie ±0,5", -0.5, 0.5), ("+0,5…+1,5", 0.5, 1.5), ("fart +1,5…+3", 1.5, 3), ("fart ≥ +3", 3, 99)]
    table(f"Gol w meczu – wg pecha/fartu (gole − oczekiwane ze strzałów, ost. {LUCK_GAMES} m)", 2, luck_b, 3, 5)


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="NHL – szanse zawodników na gol / asystę / punkt / strzały")
    ap.add_argument("--date", help="dzień meczów RRRR-MM-DD (domyślnie dziś, czas polski)")
    ap.add_argument("--backtest", nargs=2, metavar=("OD", "DO"), help="test trafności na zakresie dat")
    ap.add_argument("--out", default="raporty", help="folder na raporty")
    ap.add_argument("--cache", default="cache", help="folder na pobrane dane")
    ap.add_argument("--workers", type=int, default=6, help="równoległe pobieranie (domyślnie 6)")
    ap.add_argument("--no-open", action="store_true", help="nie otwieraj raportu w przeglądarce")
    ap.add_argument("--no-calib", action="store_true", help="nie używaj calibration.json (surowy model)")
    ap.add_argument("--no-elc",action="store_true", help="pomiń stronę bonusów ELC")
    ap.add_argument("--site",help="zapisz jako stronę GitHub Pages do podanego folderu (np. docs)")
    a = ap.parse_args(argv)

    base = Path(__file__).resolve().parent
    out_dir = (base / a.out) if not Path(a.out).is_absolute() else Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    http = Http((base / a.cache) if not Path(a.cache).is_absolute() else Path(a.cache))
    global CALIB
    CALIB = None if a.no_calib else calib.load(base)
    if CALIB:
        info = CALIB.get("_info", {})
        print(f"Kalibracja z backtestu {info.get('od','?')} → {info.get('do','?')} (n={info.get('n','?')})")

    if a.backtest:
        s, e = (dt.date.fromisoformat(x) for x in a.backtest)
        print(f"Backtest {s} → {e} (pierwsze uruchomienie pobiera dużo danych, potem działa z cache)")
        backtest(http, s, e, a.workers, out_dir, base)
        return

    day = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(TZ_PL).date()
    site = None
    if a.site:
        site = (base / a.site) if not Path(a.site).is_absolute() else Path(a.site)
        site.mkdir(parents=True, exist_ok=True)

    elc_ok, elc_rows = False, []
    if not a.no_elc:
        print("Bonusy ELC – szukam młodych zawodników i symuluję resztę sezonu...")
        try:
            import elc_model
            elc_rows = elc_model.run(http, day, a.workers, base / "elc.csv")
            elc_path = (site / "elc.html") if site else (out_dir / f"elc_{day}.html")
            elc_model.write_page(elc_rows, day, elc_path, "index.html" if site else f"nhl_{day}.html")
            print(f"Bonusy ELC: {len(elc_rows)} zawodników → {elc_path}")
            elc_ok = True
        except Exception as e:  # ELC nie może zablokować raportu dziennego
            print(f"  ! Bonusy ELC pominięte: {e}", file=sys.stderr)

    print(f"NHL {day} – pobieram terminarz i dane...")
    rows, games, _ = analyse_day(http, day, a.workers)
    if not games:
        print("Brak meczów NHL (sezon zasadniczy / play-off) w tym dniu.")
        return
    print(f"Meczów: {len(games)}, zawodników: {len(rows)}")
    if elc_rows:
        import elc_model
        elc_model.attach_to_daily(rows, elc_rows)
        n_chase = sum(1 for r in rows if r.get("elc", {}).get("chase"))
        print(f"Goniący bonus ELC w dzisiejszych meczach: {n_chase}")

    if site:
        page = write_site(rows, games, day, site, elc=elc_ok)
        print(f"Strona zaktualizowana: {page}")
        return

    html_path = out_dir / f"nhl_{day}.html"
    csv_path = out_dir / f"nhl_{day}.csv"
    write_html(rows, games, day, html_path, csv_url=csv_path.name, elc_url=f"elc_{day}.html" if elc_ok else None)
    write_csv(rows, csv_path)
    print(f"Raport: {html_path}\nCSV:    {csv_path}")
    if not a.no_open:
        webbrowser.open(html_path.resolve().as_uri())


if __name__ == "__main__":
    main()
