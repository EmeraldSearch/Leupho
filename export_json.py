"""
export_json.py  –  Exporte tracker.db en fichiers JSON statiques pour GitHub Pages
====================================================================================
Usage :
    python export_json.py              # export une fois
    python export_json.py --watch      # export toutes les 30s en boucle

Les fichiers sont écrits dans ./data/ (commité sur GitHub Pages).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time

from db import Database

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH   = os.path.join(BASE_DIR, "tracker.db")
DATA_DIR  = os.path.join(BASE_DIR, "data")
INTERVAL_SUMMARY = 10   # secondes — summary.json (Run Summary)
INTERVAL_CHARTS  = 30   # secondes — graphes

FARLANDS    = 12_550_821.0
MILLI_SIZE  = 125.508_21
NUM_MILLIS  = 100_000


# ==============================================================
# Helpers
# ==============================================================

def write_json(name: str, data) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, name)
    tmp  = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)   # atomic


LAST_HOUR_IGT  = 72_000   # 1h = 3600s × 20 ticks
NO_DOWNSAMPLE  = False    # mis à True par --full

def downsample(rows: list, max_pts: int, igt_key: str = "igt") -> list:
    """
    Downsample intelligent : garde tous les points de la dernière heure IGT,
    downsample uniformément le reste sur le budget restant.
    """
    if not rows or NO_DOWNSAMPLE:
        return rows

    # Séparer dernière heure / historique
    try:
        last_igt = float(rows[-1][igt_key])
        cutoff   = last_igt - LAST_HOUR_IGT
        recent   = [r for r in rows if float(r[igt_key]) >= cutoff]
        historic = [r for r in rows if float(r[igt_key]) <  cutoff]
    except (KeyError, TypeError, IndexError):
        # Pas de champ igt (ex. heatmap) → downsample classique
        if len(rows) <= max_pts:
            return rows
        step = len(rows) / max_pts
        return [rows[int(i * step)] for i in range(max_pts)]

    # Budget pour l'historique = max_pts - points récents (min 1000)
    budget = max(max_pts - len(recent), 1000)

    if len(historic) <= budget:
        return historic + recent

    step = len(historic) / budget
    return [historic[int(i * step)] for i in range(budget)] + recent


# ==============================================================
# Exports (mirror exact des endpoints Flask)
# ==============================================================

def export_summary(db: Database) -> None:
    s = db.fetch_summary()
    d = dict(s)
    # Ajouter l'inventaire du dernier tick
    row = db.execute(
        "SELECT inventory FROM ticks ORDER BY real_ts DESC LIMIT 1"
    ).fetchone()
    d["inventory"] = row["inventory"] if row else ""
    write_json("summary.json", d)


def export_hp(db: Database) -> None:
    rows = db.execute(
        "SELECT igt, CAST(health AS REAL) AS health FROM ticks ORDER BY real_ts"
    ).fetchall()
    rows   = downsample(rows, 10000)
    deaths = [r["igt"] for r in rows if float(r["health"]) <= 0]
    write_json("hp.json", {
        "data":   [{"igt": r["igt"], "health": float(r["health"])} for r in rows],
        "deaths": deaths,
    })


def export_speed(db: Database) -> None:
    rows = db.execute(
        """SELECT s.world_time, t.igt, s.speed_horiz, s.speed_avg, s.speed_x
           FROM speed s JOIN ticks t ON s.world_time = t.world_time
           ORDER BY t.igt"""
    ).fetchall()
    rows = downsample(rows, 10000)
    write_json("speed.json", [
        {
            "igt":         r["igt"],
            "speed_horiz": r["speed_horiz"],
            "speed_avg":   r["speed_avg"],
            "speed_x":     -float(r["speed_x"]) if r["speed_x"] is not None else 0.0,
        }
        for r in rows
    ])


def export_distance(db: Database) -> None:
    rows = db.execute(
        """SELECT igt,
                  CAST(dist_total AS REAL) AS dist_total,
                  CAST(x_total    AS REAL) AS x_total
           FROM ticks ORDER BY real_ts"""
    ).fetchall()
    if not rows:
        write_json("distance.json", [])
        return
    x_start = float(rows[0]["x_total"] or 0)
    rows = downsample(rows, 10000)
    write_json("distance.json", [
        {
            "igt":        r["igt"],
            "dist_total": float(r["dist_total"] or 0),
            "x_total":    x_start - float(r["x_total"] or 0),
        }
        for r in rows
    ])


def export_heatmap(db: Database) -> None:
    rows = db.fetch_positions(None, None)
    rows = downsample(rows, 10000)
    write_json("heatmap.json", [{"x": r["x"], "z": r["z"]} for r in rows])


def export_elevation(db: Database) -> None:
    rows = db.execute(
        "SELECT igt, CAST(elevation AS REAL) AS elevation "
        "FROM ticks WHERE elevation IS NOT NULL ORDER BY real_ts"
    ).fetchall()
    rows = downsample(rows, 10000)
    write_json("elevation.json", [
        {"igt": r["igt"], "elevation": float(r["elevation"])} for r in rows
    ])


def export_item_flow(db: Database) -> None:
    rows = db.fetch_item_flow()
    write_json("item_flow.json", [
        {"item": r["item"], "gained": r["gained"], "lost": r["lost"]}
        for r in rows[:10]
    ])


def export_deaths(db: Database) -> None:
    rows = db.fetch_deaths()
    write_json("deaths.json", [
        {"igt": r["igt"], "x": r["x"], "z": r["z"], "death_number": r["death_number"]}
        for r in rows
    ])


def export_pace(db: Database) -> None:
    rows = db.execute(
        "SELECT igt, CAST(x_total AS REAL) AS x FROM ticks WHERE x_total IS NOT NULL ORDER BY igt ASC"
    ).fetchall()
    if not rows:
        write_json("pace.json", [])
        return

    milestones = []
    prev_igt   = 0

    for i in range(1, NUM_MILLIS + 1):
        target_x = -(i * MILLI_SIZE)
        hit = next((r for r in rows if float(r["x"]) <= target_x), None)
        if hit is None:
            break

        igt_total   = int(hit["igt"])
        igt_segment = igt_total - prev_igt
        secs_total  = igt_total   / 20.0
        secs_seg    = igt_segment / 20.0
        dist        = round(i * MILLI_SIZE, 1)

        pace_seg     = round((MILLI_SIZE / secs_seg)   if secs_seg   > 0 else 0, 4)
        pace_overall = round((dist       / secs_total) if secs_total > 0 else 0, 4)
        eta_ovr_s    = (FARLANDS / pace_overall) if pace_overall > 0 else None

        milestones.append({
            "i":            i,
            "pct":          round(i / 1000, 3),
            "distance":     dist,
            "igt_total_s":  round(secs_total, 1),
            "igt_seg_s":    round(secs_seg,   1),
            "pace_seg":     pace_seg,
            "pace_overall": pace_overall,
            "eta_ovr_s":    round(eta_ovr_s, 0) if eta_ovr_s else None,
        })

        prev_igt = igt_total

    write_json("pace.json", milestones)


# ==============================================================
# Git push
# ==============================================================

def git_push(label: str = "all") -> None:
    try:
        subprocess.run(
            ["git", "-C", BASE_DIR, "add", "data/"],
            check=True, capture_output=True
        )
        result = subprocess.run(
            ["git", "-C", BASE_DIR, "diff", "--cached", "--quiet"],
            capture_output=True
        )
        if result.returncode != 0:
            subprocess.run(
                ["git", "-C", BASE_DIR, "commit", "-m", f"data: auto-update ({label})"],
                check=True, capture_output=True
            )
            subprocess.run(
                ["git", "-C", BASE_DIR, "push"],
                check=True, capture_output=True
            )
            print(f"[{time.strftime('%H:%M:%S')}] Pushed ({label}).")
        else:
            print(f"[{time.strftime('%H:%M:%S')}] No changes ({label}).")
    except subprocess.CalledProcessError as e:
        print(f"[{time.strftime('%H:%M:%S')}] Git error: {e.stderr.decode()}")


# ==============================================================
# Export all
# ==============================================================

def export_summary_only() -> None:
    db = Database(DB_PATH, readonly=True)
    try:
        export_summary(db)
        export_pace(db)
    finally:
        db.close()
    git_push(label="summary")


def export_charts() -> None:
    db = Database(DB_PATH, readonly=True)
    try:
        export_hp(db)
        export_speed(db)
        export_distance(db)
        export_elevation(db)
        export_item_flow(db)
        export_deaths(db)
    finally:
        db.close()
    git_push(label="charts")


def export_all() -> None:
    db = Database(DB_PATH, readonly=True)
    try:
        export_summary(db)
        export_hp(db)
        export_speed(db)
        export_distance(db)
        export_elevation(db)
        export_item_flow(db)
        export_deaths(db)
        export_pace(db)
    finally:
        db.close()
    git_push()


# ==============================================================
# Entry point
# ==============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="Boucle toutes les 30s")
    parser.add_argument("--full",  action="store_true", help="Export sans downsample (fin de run)")
    args = parser.parse_args()

    if args.full:
        print("Mode --full : export sans downsample.")
        NO_DOWNSAMPLE = True

    if args.watch:
        import threading
        print(f"Summary every {INTERVAL_SUMMARY}s, charts every {INTERVAL_CHARTS}s. Ctrl+C to stop.")

        def loop_summary():
            while True:
                try:
                    export_summary_only()
                except Exception as e:
                    print(f"[{time.strftime('%H:%M:%S')}] Summary error: {e}")
                time.sleep(INTERVAL_SUMMARY)

        def loop_charts():
            while True:
                try:
                    export_charts()
                except Exception as e:
                    print(f"[{time.strftime('%H:%M:%S')}] Charts error: {e}")
                time.sleep(INTERVAL_CHARTS)

        threading.Thread(target=loop_summary, daemon=True).start()
        threading.Thread(target=loop_charts,  daemon=True).start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("Stopped.")
    else:
        export_all()
