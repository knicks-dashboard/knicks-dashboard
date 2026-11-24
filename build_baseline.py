"""닉스 2025-26 정규시즌 기준값(baseline) 파일 생성."""
import json
import os
import time
from datetime import date

import pandas as pd
from nba_api.stats.endpoints import leaguedashplayerstats, leaguedashteamstats, playergamelogs, teamgamelogs
from nba_api.stats.static import teams

SEASON = "2025-26"
SEASON_TYPE = "Regular Season"
KNICKS_ID = teams.find_team_by_abbreviation("NYK")["id"]
QUARTERS = [1, 2, 3, 4]
# 주요 선수 기준: 출전 경기 수와 경기당 출전 시간
MIN_GP, MIN_MPG = 20, 15.0

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "baseline")

PLAYER_COLS = ["PLAYER_ID", "PLAYER_NAME", "GP", "MIN", "PTS", "REB", "OREB", "DREB", "AST",
               "STL", "BLK", "TOV", "PF", "FGM", "FGA", "FG_PCT", "FG3M", "FG3A", "FG3_PCT",
               "FTM", "FTA", "FT_PCT", "PLUS_MINUS"]


def call(endpoint, **kwargs):
    time.sleep(0.6)  # stats.nba.com 요청 간격
    return endpoint(season=SEASON, season_type_all_star=SEASON_TYPE, timeout=60,
                    **kwargs).get_data_frames()[0]


def team_row(df):
    return df[df["TEAM_ID"] == KNICKS_ID].iloc[0]


def game_log():
    time.sleep(0.6)
    df = teamgamelogs.TeamGameLogs(team_id_nullable=KNICKS_ID, season_nullable=SEASON,
                                   season_type_nullable=SEASON_TYPE,
                                   timeout=60).get_data_frames()[0]
    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"]).dt.date
    df = df.sort_values("GAME_DATE").reset_index(drop=True)
    df["HOME"] = df["MATCHUP"].str.contains("vs.")
    df["OPPONENT"] = df["MATCHUP"].str.split().str[-1]
    df["OPP_PTS"] = df["PTS"] - df["PLUS_MINUS"]
    df["MARGIN"] = df["PLUS_MINUS"]
    return df


def quarter_scoring():
    rows = []
    for q in QUARTERS:
        base = team_row(call(leaguedashteamstats.LeagueDashTeamStats, period=q,
                             measure_type_detailed_defense="Base", per_mode_detailed="PerGame"))
        opp = team_row(call(leaguedashteamstats.LeagueDashTeamStats, period=q,
                            measure_type_detailed_defense="Opponent", per_mode_detailed="PerGame"))
        rows.append({"QUARTER": f"Q{q}", "GP": int(base["GP"]), "PTS": base["PTS"],
                     "OPP_PTS": opp["OPP_PTS"], "DIFF": round(base["PTS"] - opp["OPP_PTS"], 1)})
    return pd.DataFrame(rows)


def four_factors():
    r = team_row(call(leaguedashteamstats.LeagueDashTeamStats,
                      measure_type_detailed_defense="Four Factors", per_mode_detailed="PerGame"))
    keys = ["EFG_PCT", "TM_TOV_PCT", "OREB_PCT", "FTA_RATE",
            "OPP_EFG_PCT", "OPP_TOV_PCT", "OPP_OREB_PCT", "OPP_FTA_RATE"]
    return {"GP": int(r["GP"]), **{k: float(r[k]) for k in keys}}


def players():
    season = call(leaguedashplayerstats.LeagueDashPlayerStats, team_id_nullable=KNICKS_ID,
                  measure_type_detailed_defense="Base", per_mode_detailed="PerGame")
    key = season[(season["GP"] >= MIN_GP) & (season["MIN"] >= MIN_MPG)]
    key = key.sort_values("MIN", ascending=False)[PLAYER_COLS].reset_index(drop=True)

    frames = []
    for q in QUARTERS:
        df = call(leaguedashplayerstats.LeagueDashPlayerStats, team_id_nullable=KNICKS_ID,
                  period=q, measure_type_detailed_defense="Base", per_mode_detailed="PerGame")
        df = df[df["PLAYER_ID"].isin(key["PLAYER_ID"])][PLAYER_COLS].copy()
        df.insert(2, "QUARTER", f"Q{q}")
        frames.append(df)
    by_q = pd.concat(frames).sort_values(["PLAYER_NAME", "QUARTER"]).reset_index(drop=True)
    return key, by_q


def head_to_head(log):
    g = log.groupby("OPPONENT")
    h2h = pd.DataFrame({
        "GP": g.size(),
        "W": g["WL"].apply(lambda s: (s == "W").sum()),
        "L": g["WL"].apply(lambda s: (s == "L").sum()),
        "PTS": g["PTS"].mean().round(1),
        "OPP_PTS": g["OPP_PTS"].mean().round(1),
        "MARGIN": g["MARGIN"].mean().round(1),
        "FG_PCT": g["FG_PCT"].mean().round(3),
        "FG3_PCT": g["FG3_PCT"].mean().round(3),
        "REB": g["REB"].mean().round(1),
        "AST": g["AST"].mean().round(1),
        "TOV": g["TOV"].mean().round(1),
    }).reset_index()
    return h2h.sort_values(["MARGIN"], ascending=False).reset_index(drop=True)


# 경기별 분포(평균·표준편차): '평소와 얼마나 다른가'를 판단하는 기준
TEAM_DIST_STATS = ["PTS", "OPP_PTS", "EFG_PCT", "FG3_PCT", "FG3A", "FTA_RATE", "TOV_PCT", "AST", "REB", "OREB", "TOV"]
PLAYER_DIST_STATS = ["MIN", "PTS", "REB", "AST", "FG3M", "FGA", "TOV", "PF", "STL", "BLK"]


def team_game_dist(log):
    g = log.copy()
    g["EFG_PCT"] = (g["FGM"] + 0.5 * g["FG3M"]) / g["FGA"]
    g["FTA_RATE"] = g["FTA"] / g["FGA"]
    g["TOV_PCT"] = g["TOV"] / (g["FGA"] + 0.44 * g["FTA"] + g["TOV"])
    return pd.DataFrame([{"STAT": k, "MEAN": round(g[k].mean(), 4), "STD": round(g[k].std(), 4)}
                         for k in TEAM_DIST_STATS])


def player_game_dist(key):
    time.sleep(0.6)
    logs = playergamelogs.PlayerGameLogs(team_id_nullable=KNICKS_ID, season_nullable=SEASON,
                                         season_type_nullable=SEASON_TYPE,
                                         timeout=60).get_data_frames()[0]
    logs = logs[logs["PLAYER_ID"].isin(key["PLAYER_ID"])]
    rows = []
    for (pid, name), g in logs.groupby(["PLAYER_ID", "PLAYER_NAME"]):
        for k in PLAYER_DIST_STATS:
            rows.append({"PLAYER_ID": pid, "PLAYER_NAME": name, "STAT": k, "GP": len(g),
                         "MEAN": round(g[k].mean(), 3), "STD": round(g[k].std(), 3)})
    return pd.DataFrame(rows)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    log = game_log()
    quarters = quarter_scoring()
    ff = four_factors()
    key, by_q = players()
    h2h = head_to_head(log)
    team_dist = team_game_dist(log)
    player_dist = player_game_dist(key)

    log_cols = ["GAME_DATE", "GAME_ID", "OPPONENT", "HOME", "WL", "PTS", "OPP_PTS", "MARGIN",
                "FGM", "FGA", "FG_PCT", "FG3M", "FG3A", "FG3_PCT", "FTM", "FTA", "FT_PCT",
                "OREB", "DREB", "REB", "AST", "STL", "BLK", "TOV", "PF"]
    log[log_cols].to_csv(os.path.join(OUT_DIR, "game_log.csv"), index=False)
    quarters.to_csv(os.path.join(OUT_DIR, "team_quarter_scoring.csv"), index=False)
    pd.DataFrame([ff]).to_csv(os.path.join(OUT_DIR, "team_four_factors.csv"), index=False)
    key.to_csv(os.path.join(OUT_DIR, "players_per_game.csv"), index=False)
    by_q.to_csv(os.path.join(OUT_DIR, "players_per_quarter.csv"), index=False)
    h2h.to_csv(os.path.join(OUT_DIR, "head_to_head.csv"), index=False)
    team_dist.to_csv(os.path.join(OUT_DIR, "team_game_distribution.csv"), index=False)
    player_dist.to_csv(os.path.join(OUT_DIR, "players_game_distribution.csv"), index=False)

    w = int((log["WL"] == "W").sum())
    baseline = {
        "meta": {
            "team": "New York Knicks", "team_id": KNICKS_ID, "season": SEASON,
            "season_type": SEASON_TYPE, "generated": date.today().isoformat(),
            "key_player_rule": f"GP >= {MIN_GP} and MIN/G >= {MIN_MPG}",
        },
        "team": {
            "record": {"W": w, "L": len(log) - w},
            "per_game": {"PTS": round(log["PTS"].mean(), 1),
                         "OPP_PTS": round(log["OPP_PTS"].mean(), 1),
                         "MARGIN": round(log["MARGIN"].mean(), 1)},
            "quarters": quarters.to_dict(orient="records"),
            "four_factors": ff,
            "game_dist": team_dist.to_dict(orient="records"),
        },
        "players": {
            "per_game": key.to_dict(orient="records"),
            "per_quarter": by_q.to_dict(orient="records"),
            "game_dist": player_dist.to_dict(orient="records"),
        },
        "head_to_head": h2h.to_dict(orient="records"),
    }
    with open(os.path.join(OUT_DIR, "baseline.json"), "w") as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2, default=float)

    print(f"기록 {w}-{len(log) - w}, 주요 선수 {len(key)}명, 상대팀 {len(h2h)}개")
    print(f"저장: {OUT_DIR}")


if __name__ == "__main__":
    main()
