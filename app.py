import os
import sqlite3
import time

from flask import Flask, g, jsonify, redirect, render_template, request, url_for

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "scores.db")

# GEM QUEST match: 0:20 auto + 1:40 teleop + 0:30 endgame = 2:30
DEFAULT_AUTO = 20
DEFAULT_DRIVER = 100
DEFAULT_ENDGAME = 30

TEAMS = ("yellow", "green")

# Camp round-robin schedule (yellow side, green side) + who sits out,
# straight from the official match schedule sheet.
SCHEDULE = [
    {"rotation": 1, "bye": "Ruby", "matches": [
        ("Emerald", "Obsidian"), ("Lapis", "Quartz"), ("Topaz", "Amethyst")]},
    {"rotation": 2, "bye": "Quartz", "matches": [
        ("Topaz", "Lapis"), ("Ruby", "Obsidian"), ("Amethyst", "Emerald")]},
    {"rotation": 3, "bye": "Topaz", "matches": [
        ("Obsidian", "Amethyst"), ("Lapis", "Emerald"), ("Quartz", "Ruby")]},
    {"rotation": 4, "bye": "Emerald", "matches": [
        ("Quartz", "Obsidian"), ("Ruby", "Topaz"), ("Amethyst", "Lapis")]},
    {"rotation": 5, "bye": "Amethyst", "matches": [
        ("Ruby", "Emerald"), ("Quartz", "Topaz"), ("Lapis", "Obsidian")]},
    {"rotation": 6, "bye": "Lapis", "matches": [
        ("Quartz", "Emerald"), ("Obsidian", "Topaz"), ("Ruby", "Amethyst")]},
    {"rotation": 7, "bye": "Obsidian", "matches": [
        ("Lapis", "Ruby"), ("Amethyst", "Quartz"), ("Topaz", "Emerald")]},
]

# Flattened: MATCHES[i] = {"rotation", "yellow", "green"}; match number = i + 1.
MATCHES = [
    {"rotation": rot["rotation"], "yellow": y, "green": g}
    for rot in SCHEDULE
    for y, g in rot["matches"]
]

app = Flask(__name__)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS match_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            match_number INTEGER NOT NULL DEFAULT 1,
            yellow_team TEXT NOT NULL DEFAULT '---',
            green_team TEXT NOT NULL DEFAULT '---',
            yellow_score INTEGER NOT NULL DEFAULT 0,
            green_score INTEGER NOT NULL DEFAULT 0,
            yellow_fouls INTEGER NOT NULL DEFAULT 0,
            yellow_majors INTEGER NOT NULL DEFAULT 0,
            green_fouls INTEGER NOT NULL DEFAULT 0,
            green_majors INTEGER NOT NULL DEFAULT 0,
            auto_duration INTEGER NOT NULL DEFAULT {auto},
            driver_duration INTEGER NOT NULL DEFAULT {driver},
            endgame_duration INTEGER NOT NULL DEFAULT {endgame},
            schedule_index INTEGER NOT NULL DEFAULT 0,
            timer_duration INTEGER NOT NULL DEFAULT {total},
            timer_remaining REAL NOT NULL DEFAULT {total},
            timer_end REAL,
            timer_running INTEGER NOT NULL DEFAULT 0
        )
        """.format(
            auto=DEFAULT_AUTO,
            driver=DEFAULT_DRIVER,
            endgame=DEFAULT_ENDGAME,
            total=DEFAULT_AUTO + DEFAULT_DRIVER + DEFAULT_ENDGAME,
        )
    )
    db.execute("INSERT OR IGNORE INTO match_state (id) VALUES (1)")

    # Migrate databases from when the teams were red/blue.
    existing = {row[1] for row in db.execute("PRAGMA table_info(match_state)")}
    renames = {
        "red_team": "yellow_team",
        "blue_team": "green_team",
        "red_score": "yellow_score",
        "blue_score": "green_score",
        "red_fouls": "yellow_fouls",
        "red_majors": "yellow_majors",
        "blue_fouls": "green_fouls",
        "blue_majors": "green_majors",
    }
    for old, new in renames.items():
        if old in existing and new not in existing:
            db.execute(
                "ALTER TABLE match_state RENAME COLUMN {0} TO {1}".format(old, new)
            )
            existing.discard(old)
            existing.add(new)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS results (
            match_index INTEGER PRIMARY KEY,
            yellow_score INTEGER NOT NULL,
            green_score INTEGER NOT NULL
        )
        """
    )
    for col, default in (
        ("yellow_fouls", 0),
        ("yellow_majors", 0),
        ("green_fouls", 0),
        ("green_majors", 0),
        ("auto_duration", DEFAULT_AUTO),
        ("driver_duration", DEFAULT_DRIVER),
        ("endgame_duration", DEFAULT_ENDGAME),
        ("schedule_index", 0),
    ):
        if col not in existing:
            db.execute(
                "ALTER TABLE match_state ADD COLUMN {0} INTEGER NOT NULL"
                " DEFAULT {1}".format(col, default)
            )
            if col == "endgame_duration":
                # Old databases stored only a flat match length; line the
                # stored total up with the new per-phase durations.
                db.execute(
                    "UPDATE match_state SET timer_duration = ?,"
                    " timer_remaining = ?, timer_running = 0 WHERE id = 1",
                    (
                        DEFAULT_AUTO + DEFAULT_DRIVER + DEFAULT_ENDGAME,
                        DEFAULT_AUTO + DEFAULT_DRIVER + DEFAULT_ENDGAME,
                    ),
                )
    db.commit()
    db.close()


def read_state():
    """Return (row, remaining, running), stopping the timer in the DB once it hits zero."""
    db = get_db()
    row = db.execute("SELECT * FROM match_state WHERE id = 1").fetchone()
    if row["timer_running"]:
        remaining = max(0.0, row["timer_end"] - time.time())
    else:
        remaining = row["timer_remaining"]
    running = bool(row["timer_running"]) and remaining > 0
    if row["timer_running"] and not running:
        db.execute(
            "UPDATE match_state SET timer_running = 0, timer_remaining = 0 WHERE id = 1"
        )
        db.commit()
    return row, remaining, running


def state_json():
    row, remaining, running = read_state()
    return jsonify(
        {
            "match_number": row["match_number"],
            "yellow_team": row["yellow_team"],
            "green_team": row["green_team"],
            "yellow_score": row["yellow_score"],
            "green_score": row["green_score"],
            "yellow_fouls": row["yellow_fouls"],
            "yellow_majors": row["yellow_majors"],
            "green_fouls": row["green_fouls"],
            "green_majors": row["green_majors"],
            "auto_duration": row["auto_duration"],
            "driver_duration": row["driver_duration"],
            "endgame_duration": row["endgame_duration"],
            "timer_duration": row["timer_duration"],
            "timer_remaining": remaining,
            "timer_running": running,
        }
    )


@app.route("/")
def referee():
    return render_template("referee.html")


@app.route("/panel/<team>")
def panel(team):
    legacy = {"red": "yellow", "blue": "green"}
    if team in legacy:
        return redirect(url_for("panel", team=legacy[team]))
    if team not in TEAMS:
        return redirect(url_for("referee"))
    return render_template("panel.html", team=team)


@app.route("/display")
def display():
    return render_template("display.html")


@app.route("/schedule")
def schedule():
    return render_template("schedule.html")


@app.route("/admin")
def admin():
    return render_template("admin.html")


@app.route("/api/state")
def api_state():
    return state_json()


@app.route("/api/schedule")
def api_schedule():
    db = get_db()
    row = db.execute("SELECT schedule_index FROM match_state WHERE id = 1").fetchone()
    results = {
        r["match_index"]: {"yellow": r["yellow_score"], "green": r["green_score"]}
        for r in db.execute("SELECT * FROM results")
    }
    matches = [
        {
            "n": i + 1,
            "rotation": m["rotation"],
            "yellow": m["yellow"],
            "green": m["green"],
            "result": results.get(i),
        }
        for i, m in enumerate(MATCHES)
    ]
    return jsonify(
        {
            "current": row["schedule_index"],
            "matches": matches,
            "byes": {rot["rotation"]: rot["bye"] for rot in SCHEDULE},
        }
    )


@app.route("/api/next_match", methods=["POST"])
def api_next_match():
    """Advance to the next scheduled match (dir=1) or go back one (dir=-1).
    Advancing saves the current scores as that match's final result; going
    back restores whatever result was saved so it can be corrected."""
    data = request.get_json(silent=True) or {}
    direction = data.get("dir")
    if direction not in (1, -1):
        return jsonify({"error": "bad request"}), 400
    db = get_db()
    row = db.execute("SELECT * FROM match_state WHERE id = 1").fetchone()
    idx = min(max(row["schedule_index"], 0), len(MATCHES) - 1)

    if direction == 1:
        db.execute(
            "INSERT OR REPLACE INTO results (match_index, yellow_score, green_score)"
            " VALUES (?, ?, ?)",
            (idx, row["yellow_score"], row["green_score"]),
        )
        new_idx = min(idx + 1, len(MATCHES) - 1)
    else:
        new_idx = max(idx - 1, 0)

    saved = db.execute(
        "SELECT * FROM results WHERE match_index = ?", (new_idx,)
    ).fetchone()
    yellow_score = saved["yellow_score"] if (direction == -1 and saved) else 0
    green_score = saved["green_score"] if (direction == -1 and saved) else 0

    match = MATCHES[new_idx]
    db.execute(
        "UPDATE match_state SET schedule_index = ?, match_number = ?,"
        " yellow_team = ?, green_team = ?, yellow_score = ?, green_score = ?,"
        " yellow_fouls = 0, yellow_majors = 0, green_fouls = 0, green_majors = 0,"
        " timer_running = 0, timer_remaining = timer_duration WHERE id = 1",
        (new_idx, new_idx + 1, match["yellow"], match["green"], yellow_score, green_score),
    )
    db.commit()
    return state_json()


@app.route("/api/adjust", methods=["POST"])
def api_adjust():
    data = request.get_json(silent=True) or {}
    team = data.get("team")
    if team not in TEAMS:
        return jsonify({"error": "bad request"}), 400
    col = team + "_score"
    db = get_db()
    if data.get("clear"):
        db.execute("UPDATE match_state SET {0} = 0 WHERE id = 1".format(col))
    else:
        delta = data.get("delta")
        if delta not in (-1, 1, 2, 3):
            return jsonify({"error": "bad request"}), 400
        db.execute(
            "UPDATE match_state SET {0} = MAX(0, {0} + ?) WHERE id = 1".format(col),
            (delta,),
        )
    db.commit()
    return state_json()


FOUL_POINTS = {"foul": 1, "major": 5}


@app.route("/api/foul", methods=["POST"])
def api_foul():
    """Record a foul: `team` is the team AWARDED the points; the other team
    committed the foul and gets its foul count bumped. `undo` reverses one."""
    data = request.get_json(silent=True) or {}
    team = data.get("team")
    kind = data.get("kind")
    if team not in TEAMS or kind not in FOUL_POINTS:
        return jsonify({"error": "bad request"}), 400
    offender = "green" if team == "yellow" else "yellow"
    points = FOUL_POINTS[kind]
    count_col = offender + ("_fouls" if kind == "foul" else "_majors")
    score_col = team + "_score"
    db = get_db()
    if data.get("undo"):
        row = db.execute("SELECT * FROM match_state WHERE id = 1").fetchone()
        if row[count_col] > 0:
            db.execute(
                "UPDATE match_state SET {0} = {0} - 1, {1} = MAX(0, {1} - ?)"
                " WHERE id = 1".format(count_col, score_col),
                (points,),
            )
    else:
        db.execute(
            "UPDATE match_state SET {0} = {0} + 1, {1} = {1} + ? WHERE id = 1".format(
                count_col, score_col
            ),
            (points,),
        )
    db.commit()
    return state_json()


@app.route("/api/timer", methods=["POST"])
def api_timer():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in ("start", "pause", "reset"):
        return jsonify({"error": "bad request"}), 400
    db = get_db()
    row, remaining, running = read_state()
    if action == "start" and not running:
        if remaining <= 0:
            remaining = row["timer_duration"]
        db.execute(
            "UPDATE match_state SET timer_running = 1, timer_end = ? WHERE id = 1",
            (time.time() + remaining,),
        )
    elif action == "pause" and running:
        db.execute(
            "UPDATE match_state SET timer_running = 0, timer_remaining = ? WHERE id = 1",
            (remaining,),
        )
    elif action == "reset":
        db.execute(
            "UPDATE match_state SET timer_running = 0,"
            " timer_remaining = timer_duration WHERE id = 1"
        )
    db.commit()
    return state_json()


@app.route("/api/setup", methods=["POST"])
def api_setup():
    data = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute("SELECT * FROM match_state WHERE id = 1").fetchone()

    def clamped_int(value, lo, hi):
        return min(max(int(value), lo), hi)

    try:
        match_number = clamped_int(data.get("match_number", row["match_number"]), 1, 9999)
        auto = clamped_int(data.get("auto_duration", row["auto_duration"]), 0, 600)
        driver = clamped_int(data.get("driver_duration", row["driver_duration"]), 5, 3600)
        endgame = clamped_int(data.get("endgame_duration", row["endgame_duration"]), 0, 600)
    except (TypeError, ValueError):
        return jsonify({"error": "match number and phase times must be whole numbers"}), 400

    yellow_team = (str(data.get("yellow_team", row["yellow_team"])).strip() or "---")[:20]
    green_team = (str(data.get("green_team", row["green_team"])).strip() or "---")[:20]
    duration = auto + driver + endgame

    db.execute(
        "UPDATE match_state SET match_number = ?, yellow_team = ?, green_team = ?,"
        " auto_duration = ?, driver_duration = ?, endgame_duration = ?,"
        " timer_duration = ? WHERE id = 1",
        (match_number, yellow_team, green_team, auto, driver, endgame, duration),
    )
    if duration != row["timer_duration"]:
        db.execute(
            "UPDATE match_state SET timer_running = 0, timer_remaining = ? WHERE id = 1",
            (duration,),
        )
    if data.get("reset_scores"):
        db.execute(
            "UPDATE match_state SET yellow_score = 0, green_score = 0,"
            " yellow_fouls = 0, yellow_majors = 0, green_fouls = 0, green_majors = 0"
            " WHERE id = 1"
        )
    if data.get("reset_schedule"):
        db.execute("DELETE FROM results")
        first = MATCHES[0]
        db.execute(
            "UPDATE match_state SET schedule_index = 0, match_number = 1,"
            " yellow_team = ?, green_team = ?, yellow_score = 0, green_score = 0,"
            " yellow_fouls = 0, yellow_majors = 0, green_fouls = 0, green_majors = 0,"
            " timer_running = 0, timer_remaining = timer_duration WHERE id = 1",
            (first["yellow"], first["green"]),
        )
    db.commit()
    return state_json()


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
