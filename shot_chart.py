"""브런슨 vs 타운스 2025-26 정규시즌 슛 차트."""
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import Arc, Circle, Rectangle
from nba_api.stats.endpoints import shotchartdetail
from nba_api.stats.static import players

SEASON = "2025-26"
PLAYERS = ["Jalen Brunson", "Karl-Anthony Towns"]
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "brunson_vs_towns_shotchart_2025-26.png")

ZONES = {
    "페인트존": ["Restricted Area", "In The Paint (Non-RA)"],
    "미드레인지": ["Mid-Range"],
    "코너 3점": ["Left Corner 3", "Right Corner 3"],
    "탑 3점": ["Above the Break 3"],
}

plt.rcParams["font.family"] = "AppleGothic"
plt.rcParams["axes.unicode_minus"] = False


def fetch_shots(name):
    cache = os.path.join(HERE, f"shots_{name.replace(' ', '_')}_{SEASON}.csv")
    if os.path.exists(cache):
        return pd.read_csv(cache)
    pid = players.find_players_by_full_name(name)[0]["id"]
    df = shotchartdetail.ShotChartDetail(
        team_id=0,
        player_id=pid,
        season_nullable=SEASON,
        season_type_all_star="Regular Season",
        context_measure_simple="FGA",
        timeout=60,
    ).get_data_frames()[0]
    df.to_csv(cache, index=False)
    return df


def draw_court(ax, color="#333333", lw=1.5):
    # 단위: 0.1피트, 림 중심이 (0, 0)
    elements = [
        Circle((0, 0), radius=7.5, linewidth=lw, color=color, fill=False),
        Rectangle((-30, -7.5), 60, -1, linewidth=lw, color=color),
        Rectangle((-80, -47.5), 160, 190, linewidth=lw, color=color, fill=False),
        Rectangle((-60, -47.5), 120, 190, linewidth=lw, color=color, fill=False),
        Arc((0, 142.5), 120, 120, theta1=0, theta2=180, linewidth=lw, color=color),
        Arc((0, 142.5), 120, 120, theta1=180, theta2=0, linewidth=lw, color=color, linestyle="dashed"),
        Arc((0, 0), 80, 80, theta1=0, theta2=180, linewidth=lw, color=color),
        Rectangle((-220, -47.5), 0, 140, linewidth=lw, color=color),
        Rectangle((220, -47.5), 0, 140, linewidth=lw, color=color),
        Arc((0, 0), 475, 475, theta1=22, theta2=158, linewidth=lw, color=color),
        Arc((0, 422.5), 120, 120, theta1=180, theta2=0, linewidth=lw, color=color),
        Rectangle((-250, -47.5), 500, 470, linewidth=lw, color=color, fill=False),
    ]
    for e in elements:
        ax.add_patch(e)
    ax.set_xlim(-250, 250)
    ax.set_ylim(-47.5, 422.5)
    ax.set_aspect("equal")
    ax.axis("off")


def zone_stats(df):
    rows = []
    for label, zones in ZONES.items():
        z = df[df["SHOT_ZONE_BASIC"].isin(zones)]
        made, att = int(z["SHOT_MADE_FLAG"].sum()), len(z)
        rows.append((label, made, att, made / att * 100 if att else 0.0))
    return rows


def main():
    data = {}
    for name in PLAYERS:
        df = fetch_shots(name)
        if df.empty:
            sys.exit(f"{name}: {SEASON} 정규시즌 슛 데이터가 없습니다.")
        data[name] = df
        print(f"{name}: {len(df)}개 슛")

    fig, axes = plt.subplots(1, 2, figsize=(16, 9.2))
    fig.subplots_adjust(left=0.03, right=0.97, top=0.88, bottom=0.2, wspace=0.08)
    fig.patch.set_facecolor("white")
    names_ko = {"Jalen Brunson": "제일런 브런슨", "Karl-Anthony Towns": "칼-앤서니 타운스"}

    for ax, name in zip(axes, PLAYERS):
        df = data[name]
        made = df[df["SHOT_MADE_FLAG"] == 1]
        miss = df[df["SHOT_MADE_FLAG"] == 0]
        ax.scatter(miss["LOC_X"], miss["LOC_Y"], marker="x", c="#d62728",
                   s=14, linewidths=1, alpha=0.55, label="실패", zorder=2)
        ax.scatter(made["LOC_X"], made["LOC_Y"], marker="o", facecolors="none",
                   edgecolors="#2ca02c", s=16, linewidths=1, alpha=0.7, label="성공", zorder=3)
        draw_court(ax)

        fg = len(made) / len(df) * 100
        ax.set_title(f"{names_ko[name]}  —  FG {len(made)}/{len(df)} ({fg:.1f}%)",
                     fontsize=15, pad=10)

        stats = zone_stats(df)
        for i, (lbl, m, a, p) in enumerate(stats):
            x = (i + 0.5) / len(stats)
            ax.text(x, -0.04, lbl, transform=ax.transAxes, ha="center", va="top",
                    fontsize=11, color="#555555")
            ax.text(x, -0.10, f"{p:.1f}%", transform=ax.transAxes, ha="center",
                    va="top", fontsize=17, fontweight="bold", color="#222222")
            ax.text(x, -0.19, f"{m}/{a}", transform=ax.transAxes, ha="center",
                    va="top", fontsize=10, color="#777777")
        ax.legend(loc="upper right", fontsize=10, frameon=True, markerscale=1.8)

    fig.suptitle(f"뉴욕 닉스 {SEASON} 정규시즌 슛 차트", fontsize=18, y=0.97)
    fig.savefig(OUT, dpi=150, bbox_inches="tight")
    print(f"저장: {OUT}")


if __name__ == "__main__":
    main()
