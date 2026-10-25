# -*- coding: utf-8 -*-
"""
Bonusy ELC (entry-level contract) – szansa, że młody zawodnik osiągnie progi bonusów "A" do końca sezonu.

Progi (Schedule A, statystyczne – te, które da się policzyć):
  napastnik: 20 G, 35 A, 60 PKT, 0,73 PKT/mecz (min. 42 mecze)
  obrońca:   10 G, 25 A, 40 PKT, 0,49 PKT/mecz (min. 42 mecze)
Dodatkowo pokazujemy bieżące miejsce w drużynie w czasie gry (top 6 F / top 4 D) i w +/- (top 3).
Nie modelujemy: All-Rookie Team, All-Star, nagród – to decyzje głosujących.

Metoda: symulacja Monte Carlo reszty sezonu (domyślnie 10 000 przebiegów):
  liczba meczów, które zagra ~ Binomial(pozostałe mecze drużyny, szansa gry),
  "prawdziwe" tempo zawodnika niepewne ~ Gamma (wspólny mnożnik dla G i A),
  gole / asysty ~ Poisson(tempo × mecze).
Kontrakty: API NHL ich nie zawiera. Plik elc.csv (z PuckPedii) potwierdza zawodników i podaje kwoty;
pozostali to "kandydaci" wybrani po wieku i stażu w NHL (przybliżenie – sprawdzaj na PuckPedii).
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

import nhl_model as core

THRESH = {
    "F": {"g": 20, "a": 35, "p": 60, "ppg": 0.73},
    "D": {"g": 10, "a": 25, "p": 40, "ppg": 0.49},
}
TEAM_RANK_LIMIT = {"F": 6, "D": 4}   # czas gry w drużynie
PM_RANK_LIMIT = 3                    # +/- w drużynie
MIN_GP = 42
MAX_AGE = 24                         # wiek na 15 września początku sezonu
MAX_PRIOR_SEASONS = 2                # ile wcześniejszych sezonów z min. 10 meczami NHL
N_SIMS = 10000
RATE_SHAPE = 12.0                    # niepewność tempa (większe = pewniejsze tempo)
PLAY_BASE = 0.88                     # typowy udział w meczach drużyny
PLAY_PRIOR_GAMES = 6

# "Goni bonus" (znaczek 🎯 w raporcie dziennym): próg jeszcze nieosiągnięty, w zasięgu i końcówka sezonu
CHASE_MAX_REMAINING = 30             # najwyżej tyle meczów drużyny do końca sezonu
CHASE_PROB = (0.10, 0.97)            # szansa na osiągnięcie progu w tym przedziale
CHASE_LABEL = {"g": "G", "a": "A", "p": "PKT"}

ALL_TEAMS = ["ANA", "BOS", "BUF", "CAR", "CBJ", "CGY", "CHI", "COL", "DAL", "DET", "EDM", "FLA", "LAK", "MIN",
             "MTL", "NJD", "NSH", "NYI", "NYR", "OTT", "PHI", "PIT", "SEA", "SJS", "STL", "TBL", "TOR", "UTA",
             "VAN", "VGK", "WPG", "WSH"]


# ---------------------------------------------------------------- plik z kontraktami
def parse_usd(s: str):
    """'$150,000' / '150000' / '150k' / '212.5k' / '212,5k' / '1.2M' -> int; puste lub błędne -> None."""
    s = (s or "").strip().lower().replace("$", "").replace("usd", "").replace(" ", "").replace(" ", "")
    if not s:
        return None
    mult = 1
    if s.endswith("k"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    if mult > 1:
        s = s.replace(",", ".")                    # 212,5k -> 212.5
    else:
        s = s.replace(",", "").replace(".", "")    # 150,000 / 150.000 -> 150000
    try:
        return int(round(float(s) * mult))
    except ValueError:
        return None


def load_contracts(path: Path):
    """elc.csv: zawodnik;bonus_za_prog_usd;uwagi  (separator ; lub ,). Zwraca {nazwa_znormalizowana: dict}."""
    out = {}
    if not path.exists():
        return out
    text = path.read_text(encoding="utf-8-sig")
    delim = ";" if text.count(";") >= text.count(",") else ","
    for row in csv.DictReader(text.splitlines(), delimiter=delim):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        name = row.get("zawodnik") or row.get("name") or ""
        if not name or name.startswith("#"):
            continue
        out[core.norm_name(name)] = {
            "name": name,
            "bonus": parse_usd(row.get("bonus_za_prog_usd") or row.get("bonus") or ""),
            "note": row.get("uwagi") or row.get("note") or "",
        }
    return out


# ---------------------------------------------------------------- dane drużyn
def team_list(http, day):
    data = http.get(f"{core.API}/standings/{day.isoformat()}", ttl_hours=12.0) or {}
    teams = sorted({core.nm(r.get("teamAbbrev")) for r in data.get("standings", []) or []} - {""})
    return teams if len(teams) >= 30 else ALL_TEAMS


def team_schedule(http, team, day):
    data = http.get(f"{core.API}/club-schedule-season/{team}/now", ttl_hours=6.0) or {}
    played = remaining = 0
    for g in data.get("games", []) or []:
        if g.get("gameType") != 2:
            continue
        done = g.get("gameState") in ("OFF", "FINAL")
        if done and g.get("gameDate", "") < day.isoformat():
            played += 1
        elif not done and g.get("gameDate", "") >= day.isoformat():
            remaining += 1
    return played, remaining


def team_ranks(http, team):
    """Miejsce w drużynie (w obrębie F lub D) w średnim czasie gry i +/-; z club-stats."""
    try:
        data = http.get(f"{core.API}/club-stats/{team}/now", ttl_hours=6.0) or {}
    except Exception:
        return {}
    groups = {"F": [], "D": []}
    for s in data.get("skaters", []) or []:
        posg = "D" if s.get("positionCode") == "D" else "F"
        groups[posg].append(s)
    out = {}
    for posg, lst in groups.items():
        by_toi = sorted(lst, key=lambda s: -(s.get("avgTimeOnIce") or 0))
        by_pm = sorted(lst, key=lambda s: -(s.get("plusMinus") or 0))
        for i, s in enumerate(by_toi):
            out.setdefault(s["playerId"], {})["toi_rank"] = i + 1
            out[s["playerId"]]["toi_n"] = len(lst)
            out[s["playerId"]]["toi"] = round((s.get("avgTimeOnIce") or 0) / 60, 1)
        for i, s in enumerate(by_pm):
            out[s["playerId"]]["pm_rank"] = i + 1
            out[s["playerId"]]["pm"] = s.get("plusMinus")
    return out


# ---------------------------------------------------------------- symulacja
def simulate(cur, lam_g, lam_a, remaining, p_play, posg, rng):
    th = THRESH[posg]
    mult = rng.gamma(RATE_SHAPE, 1.0 / RATE_SHAPE, N_SIMS)
    plays = rng.binomial(remaining, p_play, N_SIMS) if remaining > 0 else np.zeros(N_SIMS, dtype=int)
    G = cur["g"] + rng.poisson(lam_g * mult * plays)
    A = cur["a"] + rng.poisson(lam_a * mult * plays)
    P = G + A
    GP = cur["gp"] + plays
    hit = {
        "g": G >= th["g"],
        "a": A >= th["a"],
        "p": P >= th["p"],
        "ppg": (GP >= MIN_GP) & (P >= th["ppg"] * np.maximum(GP, 1)),
    }
    n_hit = sum(h.astype(int) for h in hit.values())
    return {
        "prob": {k: round(float(v.mean()), 4) for k, v in hit.items()},
        "proj": {"g": round(float(G.mean()), 1), "a": round(float(A.mean()), 1),
                 "p": round(float(P.mean()), 1), "gp": round(float(GP.mean()), 1)},
        "p10": {"g": int(np.percentile(G, 10)), "p": int(np.percentile(P, 10))},
        "p90": {"g": int(np.percentile(G, 90)), "p": int(np.percentile(P, 90))},
        "exp_hits": round(float(np.minimum(n_hit, 4).mean()), 3),
        "p_any": round(float((n_hit > 0).mean()), 4),
    }


def age_on(birth: str, day: dt.date):
    try:
        b = dt.date.fromisoformat(birth)
    except (TypeError, ValueError):
        return None
    return day.year - b.year - ((day.month, day.day) < (b.month, b.day))


# ---------------------------------------------------------------- przebieg
def run(http, day: dt.date, workers: int, contracts_path: Path, quiet=False):
    season = core.season_of(day)
    season_start = dt.date(season // 10000, 9, 15)
    seasons = [core.season_minus(season, k) for k in range(core.RATE_SEASONS)]
    contracts = load_contracts(contracts_path)
    teams = team_list(http, day)

    roster, sched, ranks = [], {}, {}
    for t in teams:
        try:
            roster += core.get_roster(http, t, None)
            sched[t] = team_schedule(http, t, day)
            ranks.update(team_ranks(http, t))
        except Exception as e:
            if not quiet:
                print(f"  ! drużyna {t}: {e}", file=sys.stderr)

    cands = []
    for p in roster:
        listed = contracts.get(core.norm_name(p["name"]))
        age = age_on(p.get("birth"), season_start)
        if listed or (age is not None and age <= MAX_AGE):
            cands.append((p, listed, age))

    live = core.season_of(dt.date.today())
    logs = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(core.get_player_games, http, p["id"], seasons, live): p["id"] for p, _, _ in cands}
        for f in as_completed(futs):
            try:
                logs[futs[f]] = f.result()
            except Exception:
                logs[futs[f]] = []

    rng = np.random.default_rng(int(day.strftime("%Y%m%d")))
    rows = []
    for p, listed, age in cands:
        games = [g for g in logs.get(p["id"], []) if g["date"] < day.isoformat()]
        prior_full = sum(1 for k in range(1, len(seasons))
                         if sum(1 for g in games if g["sidx"] == k) >= 10)
        if not listed and prior_full > MAX_PRIOR_SEASONS:
            continue
        cur_games = [g for g in games if g["sidx"] == 0]
        if not listed and not cur_games:
            continue  # kandydat bez meczu w NHL w tym sezonie – pomijamy (np. w AHL)
        cur = {"gp": len(cur_games), "g": sum(g["g"] for g in cur_games), "a": sum(g["a"] for g in cur_games)}
        cur["p"] = cur["g"] + cur["a"]

        proj, _, _, exp_toi = core.project(games, day, p["posg"], "", True, {})
        lam_g = proj["g"]["r60"] / 60 * exp_toi
        lam_a = proj["a"]["r60"] / 60 * exp_toi

        team_played, remaining = sched.get(p["team"], (0, 82))
        p_play = (cur["gp"] + PLAY_PRIOR_GAMES * PLAY_BASE) / (team_played + PLAY_PRIOR_GAMES) if team_played else PLAY_BASE
        p_play = float(min(0.98, max(0.05, p_play)))
        sim = simulate(cur, lam_g, lam_a, remaining, p_play, p["posg"], rng)

        th = THRESH[p["posg"]]
        need = {k: max(0, th[k] - cur[k]) for k in ("g", "a", "p")}
        chase = []
        if remaining <= CHASE_MAX_REMAINING:
            for k in ("g", "a", "p"):
                if need[k] > 0 and CHASE_PROB[0] <= sim["prob"][k] <= CHASE_PROB[1]:
                    chase.append({"k": k, "lab": CHASE_LABEL[k], "need": need[k], "th": th[k],
                                  "prob": sim["prob"][k], "rem": remaining})
            chase.sort(key=lambda c: -c["prob"])
        rk = ranks.get(p["id"], {})
        bonus = listed["bonus"] if listed else None
        rows.append({
            "id": p["id"], "name": p["name"], "num": p["num"], "pos": p["pos"], "posg": p["posg"],
            "team": p["team"], "age": age, "listed": bool(listed), "note": listed["note"] if listed else "",
            "prior": prior_full, "bonus": bonus,
            "cur": cur, "team_gp": team_played, "remaining": remaining, "p_play": round(p_play, 3),
            "lam": {"g": round(lam_g, 3), "a": round(lam_a, 3), "p": round(lam_g + lam_a, 3)},
            "toi_exp": round(exp_toi, 1),
            "need": need, "th": th, "chase": chase,
            "rank": rk,
            "toi_ok": bool(rk.get("toi_rank") and rk["toi_rank"] <= TEAM_RANK_LIMIT[p["posg"]]),
            "pm_ok": bool(rk.get("pm_rank") and rk["pm_rank"] <= PM_RANK_LIMIT),
            **sim,
            "exp_usd": round(bonus * sim["exp_hits"]) if bonus else None,
        })
    rows.sort(key=lambda r: -r["exp_hits"])
    return rows


def attach_to_daily(daily_rows, elc_rows):
    """Dokleja do wierszy raportu dziennego informację o ELC i ewentualnym 'gonieniu' bonusu."""
    by_id = {r["id"]: r for r in elc_rows}
    for r in daily_rows:
        e = by_id.get(r["id"])
        if e:
            r["elc"] = {"listed": e["listed"], "chase": e["chase"]}


def write_page(rows, day: dt.date, path: Path, back_url: str):
    tpl = Path(__file__).with_name("elc_template.html").read_text(encoding="utf-8")
    meta = {
        "date": day.isoformat(),
        "season": core.season_label(core.season_of(day)),
        "generated": dt.datetime.now(core.TZ_PL).strftime("%Y-%m-%d %H:%M"),
        "back": back_url, "sims": N_SIMS, "min_gp": MIN_GP,
        "thresh": THRESH, "rank_limit": TEAM_RANK_LIMIT, "pm_limit": PM_RANK_LIMIT,
        "listed": sum(1 for r in rows if r["listed"]),
    }
    payload = json.dumps({"meta": meta, "rows": rows}, ensure_ascii=False).replace("</", "<\\/")
    path.write_text(tpl.replace("__DATA__", payload), encoding="utf-8")
