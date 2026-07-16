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

ROSTER = ["Obsidian", "Ruby", "Amethyst", "Quartz", "Lapis", "Emerald", "Topaz"]

# Official camp round-robin (yellow side, green side) — seeds the editable
# quali_matches table on first run; pairings can be changed in admin later.
SCHEDULE = [
    {"rotation": 1, "matches": [
        ("Emerald", "Obsidian"), ("Lapis", "Quartz"), ("Topaz", "Amethyst")]},
    {"rotation": 2, "matches": [
        ("Topaz", "Lapis"), ("Ruby", "Obsidian"), ("Amethyst", "Emerald")]},
    {"rotation": 3, "matches": [
        ("Obsidian", "Amethyst"), ("Lapis", "Emerald"), ("Quartz", "Ruby")]},
    {"rotation": 4, "matches": [
        ("Quartz", "Obsidian"), ("Ruby", "Topaz"), ("Amethyst", "Lapis")]},
    {"rotation": 5, "matches": [
        ("Ruby", "Emerald"), ("Quartz", "Topaz"), ("Lapis", "Obsidian")]},
    {"rotation": 6, "matches": [
        ("Quartz", "Emerald"), ("Obsidian", "Topaz"), ("Ruby", "Amethyst")]},
    {"rotation": 7, "matches": [
        ("Lapis", "Ruby"), ("Amethyst", "Quartz"), ("Topaz", "Emerald")]},
]

QUALI_COUNT = sum(len(rot["matches"]) for rot in SCHEDULE)  # 21

# 4-team double elimination, one grand final (no bracket reset).
# Teams resolve as results come in; index = play order.
ELIM_LABELS = [
    "SEMIFINAL 1",       # E1: seed 1 vs seed 4
    "SEMIFINAL 2",       # E2: seed 2 vs seed 3
    "WINNERS FINAL",     # E3: winner E1 vs winner E2
    "ELIMINATION MATCH", # E4: loser E1 vs loser E2 (loser is out)
    "LOSERS FINAL",      # E5: loser E3 vs winner E4 (loser is out)
    "GRAND FINAL",       # E6: winner E3 vs winner E5 — decides the champion
]
ELIM_COUNT = len(ELIM_LABELS)

RESULT_FIELDS = (
    "yellow_score", "green_score",
    "yellow_fouls", "yellow_majors", "green_fouls", "green_majors",
)

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
    db.row_factory = sqlite3.Row
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
            phase TEXT NOT NULL DEFAULT 'quali',
            elim_index INTEGER NOT NULL DEFAULT 0,
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
            green_score INTEGER NOT NULL,
            yellow_fouls INTEGER NOT NULL DEFAULT 0,
            yellow_majors INTEGER NOT NULL DEFAULT 0,
            green_fouls INTEGER NOT NULL DEFAULT 0,
            green_majors INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Older databases saved only the final scores per match.
    existing_results = {row[1] for row in db.execute("PRAGMA table_info(results)")}
    for col in ("yellow_fouls", "yellow_majors", "green_fouls", "green_majors"):
        if col not in existing_results:
            db.execute(
                "ALTER TABLE results ADD COLUMN {0} INTEGER NOT NULL DEFAULT 0".format(col)
            )
    for col, decl in (
        ("yellow_fouls", "INTEGER NOT NULL DEFAULT 0"),
        ("yellow_majors", "INTEGER NOT NULL DEFAULT 0"),
        ("green_fouls", "INTEGER NOT NULL DEFAULT 0"),
        ("green_majors", "INTEGER NOT NULL DEFAULT 0"),
        ("auto_duration", "INTEGER NOT NULL DEFAULT {0}".format(DEFAULT_AUTO)),
        ("driver_duration", "INTEGER NOT NULL DEFAULT {0}".format(DEFAULT_DRIVER)),
        ("endgame_duration", "INTEGER NOT NULL DEFAULT {0}".format(DEFAULT_ENDGAME)),
        ("schedule_index", "INTEGER NOT NULL DEFAULT 0"),
        ("phase", "TEXT NOT NULL DEFAULT 'quali'"),
        ("elim_index", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if col.split()[0] not in existing:
            db.execute(
                "ALTER TABLE match_state ADD COLUMN {0} {1}".format(col, decl)
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
    # Editable quali pairings, seeded from the official sheet.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS quali_matches (
            match_index INTEGER PRIMARY KEY,
            rotation INTEGER NOT NULL,
            yellow_team TEXT NOT NULL,
            green_team TEXT NOT NULL
        )
        """
    )
    if db.execute("SELECT COUNT(*) FROM quali_matches").fetchone()[0] == 0:
        seed_official_pairings(db)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS elim_seeds (
            seed INTEGER PRIMARY KEY CHECK (seed BETWEEN 1 AND 4),
            team TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS elim_results (
            match_index INTEGER PRIMARY KEY,
            yellow_score INTEGER NOT NULL,
            green_score INTEGER NOT NULL,
            yellow_fouls INTEGER NOT NULL DEFAULT 0,
            yellow_majors INTEGER NOT NULL DEFAULT 0,
            green_fouls INTEGER NOT NULL DEFAULT 0,
            green_majors INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    db.commit()
    db.close()


def seed_official_pairings(db):
    db.execute("DELETE FROM quali_matches")
    i = 0
    for rot in SCHEDULE:
        for y, g_ in rot["matches"]:
            db.execute(
                "INSERT INTO quali_matches (match_index, rotation, yellow_team,"
                " green_team) VALUES (?, ?, ?, ?)",
                (i, rot["rotation"], y, g_),
            )
            i += 1


def get_quali_matches(db):
    return [
        dict(r) for r in db.execute(
            "SELECT * FROM quali_matches ORDER BY match_index"
        )
    ]


def compute_byes(quali):
    """The team of the 7 not playing in each rotation."""
    byes = {}
    for m in quali:
        byes.setdefault(m["rotation"], set(ROSTER))
        byes[m["rotation"]] -= {m["yellow_team"], m["green_team"]}
    return {rot: " / ".join(sorted(t)) if t else "—" for rot, t in byes.items()}


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


def match_label(row):
    if row["phase"] == "elims":
        idx = min(max(row["elim_index"], 0), ELIM_COUNT - 1)
        return ELIM_LABELS[idx]
    return "MATCH {0}".format(row["match_number"])


def state_json():
    row, remaining, running = read_state()
    return jsonify(
        {
            "match_number": row["match_number"],
            "match_label": match_label(row),
            "phase": row["phase"],
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


def compute_rankings(db, quali):
    """Standings from completed quali matches: Win = 2 ranking points, Tie = 1.
    Ties broken by total match points scored, then fewest penalty points
    given away (foul/major values from FOUL_POINTS), then team name."""
    stats = {
        name: {
            "team": name, "played": 0, "wins": 0, "losses": 0, "ties": 0,
            "rp": 0, "points": 0, "fouls": 0, "majors": 0, "penalty_points": 0,
        }
        for name in ROSTER
    }
    pairings = {m["match_index"]: m for m in quali}
    for r in db.execute("SELECT * FROM results"):
        m = pairings.get(r["match_index"])
        if m is None:
            continue
        for side, opp in (("yellow", "green"), ("green", "yellow")):
            name = m[side + "_team"]
            if name not in stats:
                continue
            s = stats[name]
            s["played"] += 1
            s["points"] += r[side + "_score"]
            s["fouls"] += r[side + "_fouls"]
            s["majors"] += r[side + "_majors"]
            if r[side + "_score"] > r[opp + "_score"]:
                s["wins"] += 1
            elif r[side + "_score"] < r[opp + "_score"]:
                s["losses"] += 1
            else:
                s["ties"] += 1
    for s in stats.values():
        s["rp"] = 2 * s["wins"] + s["ties"]
        s["penalty_points"] = (
            s["fouls"] * FOUL_POINTS["foul"] + s["majors"] * FOUL_POINTS["major"]
        )
    return sorted(
        stats.values(),
        key=lambda s: (-s["rp"], -s["points"], s["penalty_points"], s["team"]),
    )


def elim_bracket(db):
    """Resolve the 6-match double-elim bracket from seeds + results so far.
    Returns None until seeds are set. Each match: teams are real names once
    the feeder matches are decided, otherwise descriptive placeholders."""
    seeds = {r["seed"]: r["team"] for r in db.execute("SELECT * FROM elim_seeds")}
    if len(seeds) < 4:
        return None
    res = {r["match_index"]: dict(r) for r in db.execute("SELECT * FROM elim_results")}

    teams = [None] * ELIM_COUNT
    teams[0] = (seeds[1], seeds[4])
    teams[1] = (seeds[2], seeds[3])

    def winner(i):
        r, t = res.get(i), teams[i]
        if not r or not t or t[0] is None or t[1] is None:
            return None
        if r["yellow_score"] == r["green_score"]:
            return None
        return t[0] if r["yellow_score"] > r["green_score"] else t[1]

    def loser(i):
        r, t = res.get(i), teams[i]
        w = winner(i)
        if w is None:
            return None
        return t[1] if w == t[0] else t[0]

    teams[2] = (winner(0), winner(1))
    teams[3] = (loser(0), loser(1))
    teams[4] = (loser(2), winner(3))
    teams[5] = (winner(2), winner(4))

    placeholders = [
        ("Seed 1", "Seed 4"),
        ("Seed 2", "Seed 3"),
        ("Winner SF1", "Winner SF2"),
        ("Loser SF1", "Loser SF2"),
        ("Loser WF", "Winner ELIM"),
        ("Winner WF", "Winner LF"),
    ]
    matches = []
    for i in range(ELIM_COUNT):
        y, g_ = teams[i]
        r = res.get(i)
        matches.append(
            {
                "n": i + 1,
                "label": ELIM_LABELS[i],
                "yellow": y or placeholders[i][0],
                "green": g_ or placeholders[i][1],
                "decided": y is not None and g_ is not None,
                "result": {"yellow": r["yellow_score"], "green": r["green_score"]} if r else None,
            }
        )
    return {"seeds": seeds, "matches": matches, "champion": winner(5)}


@app.route("/api/schedule")
def api_schedule():
    db = get_db()
    row = db.execute(
        "SELECT schedule_index, phase, elim_index FROM match_state WHERE id = 1"
    ).fetchone()
    quali = get_quali_matches(db)
    results = {
        r["match_index"]: dict(r) for r in db.execute("SELECT * FROM results")
    }
    matches = []
    for m in quali:
        r = results.get(m["match_index"])
        matches.append(
            {
                "n": m["match_index"] + 1,
                "rotation": m["rotation"],
                "yellow": m["yellow_team"],
                "green": m["green_team"],
                "result": {"yellow": r["yellow_score"], "green": r["green_score"]} if r else None,
                "full_result": {f: r[f] for f in RESULT_FIELDS} if r else None,
            }
        )
    bracket = elim_bracket(db)
    return jsonify(
        {
            "phase": row["phase"],
            "roster": ROSTER,
            "current": row["schedule_index"],
            "matches": matches,
            "byes": compute_byes(quali),
            "rankings": compute_rankings(db, quali),
            "elims": dict(bracket, current=row["elim_index"]) if bracket else None,
        }
    )


def load_match(db, phase, idx, teams, scores=None):
    """Point the live scoreboard at a match: set teams, scores (or zeros),
    clear fouls unless provided, reset the timer."""
    s = scores or {f: 0 for f in RESULT_FIELDS}
    db.execute(
        "UPDATE match_state SET phase = ?, {0} = ?, match_number = ?,"
        " yellow_team = ?, green_team = ?,"
        " yellow_score = ?, green_score = ?, yellow_fouls = ?, yellow_majors = ?,"
        " green_fouls = ?, green_majors = ?,"
        " timer_running = 0, timer_remaining = timer_duration WHERE id = 1".format(
            "elim_index" if phase == "elims" else "schedule_index"
        ),
        (
            phase, idx, idx + 1, teams[0], teams[1],
            s["yellow_score"], s["green_score"], s["yellow_fouls"], s["yellow_majors"],
            s["green_fouls"], s["green_majors"],
        ),
    )


@app.route("/api/next_match", methods=["POST"])
def api_next_match():
    """Advance to the next match (dir=1) or go back one (dir=-1) in whichever
    phase we're in. Advancing saves the current scores as the final result;
    going back restores the saved result so it can be corrected."""
    data = request.get_json(silent=True) or {}
    direction = data.get("dir")
    if direction not in (1, -1):
        return jsonify({"error": "bad request"}), 400
    db = get_db()
    row = db.execute("SELECT * FROM match_state WHERE id = 1").fetchone()

    if row["phase"] == "elims":
        table, count = "elim_results", ELIM_COUNT
        idx = min(max(row["elim_index"], 0), count - 1)
        if direction == 1 and row["yellow_score"] == row["green_score"]:
            return jsonify({"error": "elimination matches can't end in a tie —"
                            " someone has to win before you advance"}), 400
    else:
        table, count = "results", QUALI_COUNT
        idx = min(max(row["schedule_index"], 0), count - 1)

    if direction == 1:
        db.execute(
            "INSERT OR REPLACE INTO {0} (match_index, {1})"
            " VALUES (?, ?, ?, ?, ?, ?, ?)".format(table, ", ".join(RESULT_FIELDS)),
            (idx,) + tuple(row[f] for f in RESULT_FIELDS),
        )
        new_idx = min(idx + 1, count - 1)
    else:
        new_idx = max(idx - 1, 0)

    saved = db.execute(
        "SELECT * FROM {0} WHERE match_index = ?".format(table), (new_idx,)
    ).fetchone()
    scores = {f: saved[f] for f in RESULT_FIELDS} if (direction == -1 and saved) else None

    if row["phase"] == "elims":
        bracket = elim_bracket(db)
        m = bracket["matches"][new_idx]
        if not m["decided"]:
            return jsonify({"error": "that bracket spot isn't decided yet"}), 400
        load_match(db, "elims", new_idx, (m["yellow"], m["green"]), scores)
    else:
        m = db.execute(
            "SELECT * FROM quali_matches WHERE match_index = ?", (new_idx,)
        ).fetchone()
        load_match(db, "quali", new_idx, (m["yellow_team"], m["green_team"]), scores)
    db.commit()
    return state_json()


@app.route("/api/quali_match", methods=["POST"])
def api_quali_match():
    """Edit one quali match: change the pairing, set/clear its saved result,
    or make it the live match on the scoreboard."""
    data = request.get_json(silent=True) or {}
    try:
        idx = int(data.get("index"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad match index"}), 400
    if not 0 <= idx < QUALI_COUNT:
        return jsonify({"error": "bad match index"}), 400
    db = get_db()
    row = db.execute("SELECT * FROM match_state WHERE id = 1").fetchone()

    if "yellow" in data or "green" in data:
        m = db.execute(
            "SELECT * FROM quali_matches WHERE match_index = ?", (idx,)
        ).fetchone()
        yellow = data.get("yellow", m["yellow_team"])
        green = data.get("green", m["green_team"])
        if yellow not in ROSTER or green not in ROSTER or yellow == green:
            return jsonify({"error": "pick two different teams"}), 400
        db.execute(
            "UPDATE quali_matches SET yellow_team = ?, green_team = ?"
            " WHERE match_index = ?",
            (yellow, green, idx),
        )
        if row["phase"] == "quali" and row["schedule_index"] == idx:
            db.execute(
                "UPDATE match_state SET yellow_team = ?, green_team = ? WHERE id = 1",
                (yellow, green),
            )

    if "result" in data:
        if data["result"] is None:
            db.execute("DELETE FROM results WHERE match_index = ?", (idx,))
        else:
            existing = db.execute(
                "SELECT * FROM results WHERE match_index = ?", (idx,)
            ).fetchone()
            vals = {}
            for f in RESULT_FIELDS:
                supplied = data["result"].get(f)
                if supplied is None:
                    vals[f] = existing[f] if existing else 0
                else:
                    try:
                        vals[f] = min(max(int(supplied), 0), 999)
                    except (TypeError, ValueError):
                        return jsonify({"error": "scores must be whole numbers"}), 400
            db.execute(
                "INSERT OR REPLACE INTO results (match_index, {0})"
                " VALUES (?, ?, ?, ?, ?, ?, ?)".format(", ".join(RESULT_FIELDS)),
                (idx,) + tuple(vals[f] for f in RESULT_FIELDS),
            )

    if data.get("make_current"):
        if row["phase"] != "quali":
            return jsonify({"error": "leave eliminations first"}), 400
        m = db.execute(
            "SELECT * FROM quali_matches WHERE match_index = ?", (idx,)
        ).fetchone()
        saved = db.execute(
            "SELECT * FROM results WHERE match_index = ?", (idx,)
        ).fetchone()
        scores = {f: saved[f] for f in RESULT_FIELDS} if saved else None
        load_match(db, "quali", idx, (m["yellow_team"], m["green_team"]), scores)

    db.commit()
    return state_json()


@app.route("/api/elims", methods=["POST"])
def api_elims():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    db = get_db()

    if action == "start":
        seeds = data.get("seeds")
        if (not isinstance(seeds, list) or len(seeds) != 4
                or len(set(seeds)) != 4 or any(t not in ROSTER for t in seeds)):
            return jsonify({"error": "pick four different teams"}), 400
        db.execute("DELETE FROM elim_seeds")
        for i, team in enumerate(seeds, start=1):
            db.execute("INSERT INTO elim_seeds (seed, team) VALUES (?, ?)", (i, team))
        db.execute("DELETE FROM elim_results")
        load_match(db, "elims", 0, (seeds[0], seeds[3]))
        db.commit()
        return state_json()

    if action == "back_to_quali":
        row = db.execute("SELECT * FROM match_state WHERE id = 1").fetchone()
        idx = min(max(row["schedule_index"], 0), QUALI_COUNT - 1)
        m = db.execute(
            "SELECT * FROM quali_matches WHERE match_index = ?", (idx,)
        ).fetchone()
        saved = db.execute(
            "SELECT * FROM results WHERE match_index = ?", (idx,)
        ).fetchone()
        scores = {f: saved[f] for f in RESULT_FIELDS} if saved else None
        load_match(db, "quali", idx, (m["yellow_team"], m["green_team"]), scores)
        db.commit()
        return state_json()

    return jsonify({"error": "bad request"}), 400


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
    if data.get("restore_pairings"):
        seed_official_pairings(db)
    if data.get("reset_schedule"):
        db.execute("DELETE FROM results")
        db.execute("DELETE FROM elim_results")
        db.execute("DELETE FROM elim_seeds")
        m = db.execute(
            "SELECT * FROM quali_matches WHERE match_index = 0"
        ).fetchone()
        load_match(db, "quali", 0, (m["yellow_team"], m["green_team"]))
    db.commit()
    return state_json()


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
