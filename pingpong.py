#!/usr/bin/env python3
"""
=============================================================================
 Ping-Pong Scoring System — Raspberry Pi Server  v3.0
 Flask + SocketIO + MQTT + SQLite crash recovery
=============================================================================

 MQTT topics consumed
 ─────────────────────
  button/green|blue   — short / double / reset
  status/green|blue   — "connected"  (retained)
  heartbeat/green|blue— "ok"         (every 5s from ESP32)
  battery/green|blue  — "85"         (percent, every 30s)

 Logging
 ────────
  ~/pingpong/logs/game_YYYYMMDD.log   — plain English, human-readable
  ~/pingpong/game_state.db            — SQLite crash-recovery state

 Crash recovery
 ───────────────
  On every state mutation the full GameState is written to SQLite.
  On startup: if last exit was unclean (crash / power loss), state is
  restored and the game resumes where it left off.
  On quad-press reset: exit is marked clean → fresh start at RULE_RACE.
"""

import copy
import json
import logging
import os
import queue
import signal
import sqlite3
import sys
import threading
import time
import warnings
from datetime import datetime
from enum import Enum, auto
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

from flask import Flask, render_template
from flask_socketio import SocketIO, emit
import paho.mqtt.client as mqtt

# =============================================================================
#  CONFIG
# =============================================================================

VERSION          = "3.0"
BASE_DIR         = Path.home() / "pingpong"
LOG_DIR          = BASE_DIR / "logs"
DB_PATH          = BASE_DIR / "game_state.db"

MQTT_HOST        = "localhost"
MQTT_PORT        = 1883
FLASK_HOST       = "0.0.0.0"
FLASK_PORT       = 5000

HEARTBEAT_TIMEOUT = 12   # seconds before a button is marked disconnected
BATTERY_LOW_PCT   = 25   # flash warning below this level

# =============================================================================
#  LOGGING SETUP
# =============================================================================

LOG_DIR.mkdir(parents=True, exist_ok=True)

def _build_logger() -> logging.Logger:
    fmt = logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # Daily rotating human-readable log
    fh = TimedRotatingFileHandler(
        LOG_DIR / f"game_{datetime.now().strftime('%Y%m%d')}.log",
        when="midnight", backupCount=14, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.INFO)

    # Console (journald picks this up via systemd)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    ch.setLevel(logging.DEBUG)

    log = logging.getLogger("pingpong")
    log.setLevel(logging.DEBUG)
    log.addHandler(fh)
    log.addHandler(ch)
    return log

log = _build_logger()

# =============================================================================
#  STATE MACHINE
# =============================================================================

class State(Enum):
    WAITING_BUTTONS = auto()
    RULE_RACE       = auto()
    RULE_BO         = auto()
    SERVING_CHOICE  = auto()
    PLAYING         = auto()
    WIN_CONFIRM     = auto()
    MATCH_OVER      = auto()

# =============================================================================
#  GAME STATE
# =============================================================================

class GameState:
    def __init__(self):
        self.race_to       = 11
        self.best_of       = 3
        self.games_won     = {"left": 0, "right": 0}
        self.current_game  = 1
        self.score         = {"left": 0, "right": 0}
        self.server        = "left"
        self.serve_count   = 1
        self.serve_num     = 0
        self.state         = State.WAITING_BUTTONS
        self.extend_prompt = False
        self.game_winner   = None
        self.game_history  = []

    @staticmethod
    def colour_to_side(colour: str) -> str:
        return "left" if colour == "green" else "right"

    @staticmethod
    def side_to_colour(side: str) -> str:
        return "green" if side == "left" else "blue"

    def server_colour(self) -> str:
        return self.side_to_colour(self.server)

    def clone(self):
        return copy.deepcopy(self)

    def to_dict(self) -> dict:
        return {
            "state":        self.state.name,
            "race_to":      self.race_to,
            "best_of":      self.best_of,
            "games_won":    dict(self.games_won),
            "current_game": self.current_game,
            "score":        dict(self.score),
            "server":       self.server,
            "serve_count":  self.serve_count,
            "serve_num":    self.serve_num,
            "extend_prompt":self.extend_prompt,
            "game_winner":  self.game_winner,
            "game_history": list(self.game_history),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GameState":
        gs = cls()
        gs.state         = State[d["state"]]
        gs.race_to       = d["race_to"]
        gs.best_of       = d["best_of"]
        gs.games_won     = d["games_won"]
        gs.current_game  = d["current_game"]
        gs.score         = d["score"]
        gs.server        = d["server"]
        gs.serve_count   = d["serve_count"]
        gs.serve_num     = d["serve_num"]
        gs.extend_prompt = d["extend_prompt"]
        gs.game_winner   = d.get("game_winner")
        gs.game_history  = d.get("game_history", [])
        return gs

# =============================================================================
#  SQLITE MANAGER
# =============================================================================

class DatabaseManager:
    """
    Single-row current_state table + append-only match_history table.
    Writes are synchronous and atomic — safe against crash mid-write.
    """

    def __init__(self, path: Path):
        self.path = str(path)
        self._init_db()

    def _conn(self):
        return sqlite3.connect(self.path)

    def _init_db(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS current_state (
                    id          INTEGER PRIMARY KEY CHECK (id = 1),
                    timestamp   TEXT    NOT NULL,
                    state_json  TEXT    NOT NULL,
                    clean_exit  INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS match_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   TEXT    NOT NULL,
                    race_to     INTEGER,
                    best_of     INTEGER,
                    winner      TEXT,
                    games_json  TEXT,
                    duration_s  INTEGER
                );
            """)
        log.debug("[DB] Initialised at %s", self.path)

    def save_state(self, gs: GameState, clean_exit: bool = False):
        """Overwrite the single current-state row."""
        with self._conn() as c:
            c.execute("""
                INSERT INTO current_state (id, timestamp, state_json, clean_exit)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    timestamp  = excluded.timestamp,
                    state_json = excluded.state_json,
                    clean_exit = excluded.clean_exit
            """, (datetime.now().isoformat(), json.dumps(gs.to_dict()), int(clean_exit)))

    def load_state(self) -> "GameState | None":
        """
        Returns restored GameState if last exit was unclean (crash / power loss).
        Returns None if clean exit or no prior state.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT state_json, clean_exit FROM current_state WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        state_json, clean_exit = row
        if clean_exit:
            log.info("[DB] Last exit was clean — starting fresh.")
            return None
        log.info("[DB] Unclean exit detected — restoring state.")
        return GameState.from_dict(json.loads(state_json))

    def record_match(self, gs: GameState, winner: str, duration_s: int):
        with self._conn() as c:
            c.execute("""
                INSERT INTO match_history (timestamp, race_to, best_of, winner, games_json, duration_s)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(),
                gs.race_to, gs.best_of, winner,
                json.dumps(gs.game_history), duration_s
            ))
        log.info("[DB] Match recorded: winner=%s race_to=%d best_of=%d", winner, gs.race_to, gs.best_of)

# =============================================================================
#  PURE GAME LOGIC
# =============================================================================

def _advance_serve(gs: GameState) -> bool:
    gs.serve_num += 1
    if gs.serve_count == 1:
        gs.serve_count = 2
        return False
    gs.serve_count = 1
    gs.server = "right" if gs.server == "left" else "left"
    return True

def _apply_point(gs: GameState, side: str) -> bool:
    gs.score[side] += 1
    return _advance_serve(gs)

def check_game_win(gs: GameState):
    l, r = gs.score["left"], gs.score["right"]
    if (l >= gs.race_to or r >= gs.race_to) and abs(l - r) >= 2:
        return "left" if l > r else "right"
    return None

def swap_games_won(gs: GameState):
    gs.games_won["left"], gs.games_won["right"] = gs.games_won["right"], gs.games_won["left"]

def start_new_game(gs: GameState, winning_side: str):
    new_server = "right" if winning_side == "left" else "left"
    swap_games_won(gs)
    gs.score        = {"left": 0, "right": 0}
    gs.current_game += 1
    gs.server       = new_server
    gs.serve_count  = 1

def match_winner(gs: GameState):
    needed = (gs.best_of // 2) + 1
    for side in ("left", "right"):
        if gs.games_won[side] >= needed:
            return side
    return None

# =============================================================================
#  FLASK + SOCKETIO
# =============================================================================

app = Flask(__name__)
app.config["SECRET_KEY"] = "pingpong_v3"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# =============================================================================
#  MATCH ENGINE
# =============================================================================

class MatchEngine:

    def __init__(self, db: DatabaseManager):
        self.db           = db
        self.gs           = GameState()
        self._undo_stack  = []
        self._connected   = {"green": False, "blue": False}
        self._heartbeat   = {"green": 0.0, "blue": 0.0}
        self._battery     = {"green": -1,   "blue": -1}
        self.event_queue  = queue.Queue()
        self._match_start = None

        # Attempt crash recovery
        restored = db.load_state()
        if restored is not None:
            self.gs = restored
            # Assume buttons will reconnect shortly (retained MQTT messages)
            self._connected = {"green": True, "blue": True}
            self._heartbeat = {"green": time.time(), "blue": time.time()}
            log.info("[Engine] Restored state: %s | score %s | games %s",
                     self.gs.state.name, self.gs.score, self.gs.games_won)
        else:
            log.info("[Engine] Fresh start.")

    # ── State broadcast ──────────────────────────────────────────────────

    def _push_state(self, message: str = ""):
        payload = self.gs.to_dict()
        payload["message"]   = message
        payload["connected"] = dict(self._connected)
        payload["battery"]   = dict(self._battery)
        payload["heartbeat"] = {
            k: (time.time() - v) < HEARTBEAT_TIMEOUT
            for k, v in self._heartbeat.items()
        }
        socketio.emit("state_update", payload)
        self.db.save_state(self.gs, clean_exit=False)
        log.debug("[State] %s | %s | games %s | server %s",
                  self.gs.state.name, self.gs.score,
                  self.gs.games_won, self.gs.server)

    # ── Undo ─────────────────────────────────────────────────────────────

    def _push_undo(self):
        self._undo_stack.append(self.gs.clone())

    def _pop_undo(self) -> bool:
        if self._undo_stack:
            self.gs = self._undo_stack.pop()
            return True
        return False

    # ── Main dispatcher ──────────────────────────────────────────────────

    def handle_button(self, colour: str, press_type: str):
        log.info("[Button] %s %s", colour.upper(), press_type)

        if press_type == "reset":
            self._handle_reset(colour)
            return

        if press_type == "double":
            self._handle_undo(colour)
            return

        if press_type == "short":
            self._handle_short(colour)

    def _handle_short(self, colour: str):
        gs = self.gs
        s  = gs.state

        if s == State.WAITING_BUTTONS:
            pass

        elif s == State.RULE_RACE:
            self._push_undo()
            gs.race_to = 11 if colour == "green" else 21
            gs.state   = State.RULE_BO
            log.info("[Game] Race to %d selected.", gs.race_to)
            self._push_state(f"Race to {gs.race_to} selected. Choose best of.")

        elif s == State.RULE_BO:
            self._push_undo()
            gs.best_of = 3 if colour == "green" else 5
            gs.state   = State.SERVING_CHOICE
            log.info("[Game] Best of %d selected.", gs.best_of)
            self._push_state(f"Best of {gs.best_of}. Who serves first?")

        elif s == State.SERVING_CHOICE:
            self._push_undo()
            gs.server      = GameState.colour_to_side(colour)
            gs.serve_count = 1
            gs.serve_num   = 1
            gs.state       = State.PLAYING
            self._match_start = time.time()
            log.info("[Game] %s serves first.", colour.upper())
            self._push_state(f"{colour.upper()} serves first!")

        elif s == State.PLAYING:
            self._handle_score(colour)

        elif s == State.WIN_CONFIRM:
            self._handle_win_confirm(colour)

        elif s == State.MATCH_OVER:
            self._push_state("Long press to start a new match.")

    # ── Scoring ──────────────────────────────────────────────────────────

    def _handle_score(self, colour: str):
        gs   = self.gs
        side = GameState.colour_to_side(colour)

        self._push_undo()
        changed_server = _apply_point(gs, side)

        score_str = f"{gs.score['left']}–{gs.score['right']}"
        log.info("[Score] %s scores. Score: %s. Server: %s (%d/2).",
                 colour.upper(), score_str,
                 gs.server_colour().upper(), gs.serve_count)

        winning_side = check_game_win(gs)
        if winning_side:
            self._handle_game_win(winning_side)
            return

        self._push_state(f"{colour.upper()} scores! {score_str}")

    # ── Game win ─────────────────────────────────────────────────────────

    def _handle_game_win(self, winning_side: str):
        gs           = self.gs
        winner_colour = GameState.side_to_colour(winning_side)

        gs.game_history.append({
            "left":          gs.score["left"],
            "right":         gs.score["right"],
            "winner_side":   winning_side,
            "winner_colour": winner_colour,
        })
        gs.games_won[winning_side] += 1
        gs.game_winner = winning_side
        m_winner = match_winner(gs)

        log.info("[Game] %s wins game %d! Games: L%d–R%d.",
                 winner_colour.upper(), gs.current_game,
                 gs.games_won["left"], gs.games_won["right"])

        if m_winner and gs.best_of == 3:
            gs.state         = State.WIN_CONFIRM
            gs.extend_prompt = True
            self._push_state(
                f"{winner_colour.upper()} wins! "
                "GREEN = best of 5  |  BLUE = new match"
            )
            return

        if m_winner:
            gs.state = State.MATCH_OVER
            duration = int(time.time() - self._match_start) if self._match_start else 0
            self.db.record_match(gs, winner_colour, duration)
            self.db.save_state(gs, clean_exit=False)
            log.info("[Match] %s wins the match! Duration: %ds.", winner_colour.upper(), duration)
            self._push_state(f"{winner_colour.upper()} wins the match!")
            return

        start_new_game(gs, winning_side)
        gs.state = State.PLAYING
        log.info("[Game] Game %d starting.", gs.current_game)
        self._push_state(
            f"{winner_colour.upper()} wins game {gs.current_game - 1}! "
            f"Game {gs.current_game} starting…"
        )

    # ── BO3 extend ───────────────────────────────────────────────────────

    def _handle_win_confirm(self, colour: str):
        gs = self.gs
        if colour == "blue":
            log.info("[Game] Blue pressed — starting new match.")
            self._clean_reset()
            return
        self._push_undo()
        winning_side     = gs.game_winner
        gs.best_of       = 5
        gs.extend_prompt = False
        start_new_game(gs, winning_side)
        gs.state = State.PLAYING
        log.info("[Game] Extended to best of 5.")
        self._push_state("Extended to best of 5! Game on!")

    # ── Undo ─────────────────────────────────────────────────────────────

    def _handle_undo(self, colour: str):
        if not self._undo_stack:
            log.info("[Undo] Nothing to undo.")
            self._push_state("Nothing to undo.")
            return
        self._pop_undo()
        log.info("[Undo] %s double-pressed. Score reverted to %s.",
                 colour.upper(), self.gs.score)
        self._push_state("⟵ Point undone.")

    # ── Reset (quad-press) ───────────────────────────────────────────────

    def _handle_reset(self, colour: str):
        log.info("[Reset] %s quad-pressed — resetting to rule selection.", colour.upper())
        self._clean_reset()

    def _clean_reset(self):
        """Mark clean exit in DB, reset state, skip button-wait."""
        old_connected = dict(self._connected)
        old_heartbeat = dict(self._heartbeat)
        old_battery   = dict(self._battery)

        self.gs           = GameState()
        self._undo_stack  = []
        self._match_start = None

        # Skip WAITING_BUTTONS — assume buttons still connected
        self.gs.state    = State.RULE_RACE
        self._connected  = old_connected
        self._heartbeat  = old_heartbeat
        self._battery    = old_battery

        # Mark as clean so crash recovery won't restore this reset
        self.db.save_state(self.gs, clean_exit=True)
        log.info("[Reset] Clean reset. Going to RULE_RACE.")
        self._push_state("Reset! Choose game length.")

    # ── Connection events ────────────────────────────────────────────────

    def on_button_connected(self, colour: str):
        self._connected[colour]  = True
        self._heartbeat[colour]  = time.time()
        log.info("[Connect] %s button connected.", colour.capitalize())

        if all(self._connected.values()):
            if self.gs.state == State.WAITING_BUTTONS:
                self.gs.state = State.RULE_RACE
                log.info("[Connect] Both connected — advancing to rule selection.")
                self._push_state("Both buttons connected! Choose game length.")
            else:
                # Reconnect during play — just update status
                self._push_state(f"{colour.capitalize()} button reconnected.")
        else:
            self._push_state(f"{colour.capitalize()} button connected. Waiting for the other…")

    def on_heartbeat(self, colour: str):
        self._heartbeat[colour] = time.time()
        # No state push — heartbeat monitor handles disconnect detection

    def on_battery(self, colour: str, percent: int):
        self._battery[colour] = percent
        log.info("[Battery] %s: %d%%", colour.capitalize(), percent)
        if 0 <= percent <= BATTERY_LOW_PCT:
            log.warning("[Battery] %s battery LOW: %d%%", colour.capitalize(), percent)
        self._push_state()   # silent push — updates battery display

    # ── Heartbeat monitor ────────────────────────────────────────────────

    def run_heartbeat_monitor(self):
        """Background thread — marks buttons disconnected if silent > timeout."""
        while True:
            time.sleep(2)
            now = time.time()
            for colour in ("green", "blue"):
                if self._connected[colour]:
                    age = now - self._heartbeat.get(colour, 0)
                    if age > HEARTBEAT_TIMEOUT:
                        self._connected[colour] = False
                        log.warning("[Heartbeat] %s button timed out (%.0fs).",
                                    colour.capitalize(), age)
                        self._push_state(f"{colour.capitalize()} button disconnected.")

    # ── Main event loop ──────────────────────────────────────────────────

    def run(self):
        log.info("[Engine] Ping-Pong scorer v%s running.", VERSION)
        # Push initial state to any already-connected browsers
        self._push_state("Server started.")
        while True:
            try:
                item = self.event_queue.get(timeout=1)
                event_type = item[0]
                if event_type == "button":
                    _, colour, press = item
                    self.handle_button(colour, press)
                elif event_type == "connected":
                    _, colour = item
                    self.on_button_connected(colour)
                elif event_type == "heartbeat":
                    _, colour = item
                    self.on_heartbeat(colour)
                elif event_type == "battery":
                    _, colour, pct = item
                    self.on_battery(colour, pct)
            except queue.Empty:
                pass
            except Exception as e:
                log.exception("[Engine] Unhandled error: %s", e)

# =============================================================================
#  MQTT CLIENT
# =============================================================================

class MQTTClient:
    def __init__(self, engine: MatchEngine):
        self.engine  = engine
        self._client = mqtt.Client(client_id="pingpong_server")

    def start(self):
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                self._client.connect(MQTT_HOST, MQTT_PORT, 60)
                self._client.loop_forever()
            except Exception as e:
                log.error("[MQTT] Failed: %s. Retry in 5s.", e)
                time.sleep(5)

    def _on_connect(self, client, userdata, flags, rc):
        log.info("[MQTT] Connected (rc=%d).", rc)
        for topic in (
            "button/green", "button/blue",
            "status/green", "status/blue",
            "heartbeat/green", "heartbeat/blue",
            "battery/green", "battery/blue",
        ):
            client.subscribe(topic)

    def _on_disconnect(self, client, userdata, rc):
        log.warning("[MQTT] Disconnected (rc=%d). Reconnecting…", rc)

    def _on_message(self, client, userdata, msg):
        topic   = msg.topic
        payload = msg.payload.decode().strip().lower()
        log.debug("[MQTT] %s → %s", topic, payload)

        if topic in ("status/green", "status/blue") and payload == "connected":
            colour = topic.split("/")[1]
            self.engine.event_queue.put(("connected", colour))

        elif topic in ("button/green", "button/blue") and payload in ("short", "double", "reset"):
            colour = topic.split("/")[1]
            self.engine.event_queue.put(("button", colour, payload))

        elif topic in ("heartbeat/green", "heartbeat/blue"):
            colour = topic.split("/")[1]
            self.engine.event_queue.put(("heartbeat", colour))

        elif topic in ("battery/green", "battery/blue"):
            colour = topic.split("/")[1]
            try:
                pct = int(payload)
                self.engine.event_queue.put(("battery", colour, pct))
            except ValueError:
                pass

# =============================================================================
#  FLASK ROUTES + SOCKETIO EVENTS
# =============================================================================

_engine: MatchEngine = None

@app.route("/")
def index():
    return render_template("scoreboard.html")

@socketio.on("connect")
def handle_connect():
    log.info("[SocketIO] Browser connected — state pushed.")
    if _engine:
        payload = _engine.gs.to_dict()
        payload["message"]   = "Connected to scorer."
        payload["connected"] = dict(_engine._connected)
        payload["battery"]   = dict(_engine._battery)
        payload["heartbeat"] = {
            k: (time.time() - v) < HEARTBEAT_TIMEOUT
            for k, v in _engine._heartbeat.items()
        }
        emit("state_update", payload)

@socketio.on("sim_button")
def handle_sim(data):
    colour     = data.get("colour", "green")
    press_type = data.get("press", "short")
    if _engine:
        _engine.event_queue.put(("button", colour, press_type))

# =============================================================================
#  ENTRY POINT
# =============================================================================

def main():
    global _engine

    db     = DatabaseManager(DB_PATH)
    engine = MatchEngine(db)
    _engine = engine

    # Heartbeat monitor in background
    threading.Thread(target=engine.run_heartbeat_monitor, daemon=True).start()

    # MQTT
    MQTTClient(engine).start()

    # Engine event loop in background
    threading.Thread(target=engine.run, daemon=True).start()

    def _shutdown(sig, frame):
        log.info("[Server] Shutdown signal — saving clean state.")
        db.save_state(engine.gs, clean_exit=True)
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("[Server] Starting Flask on %s:%d", FLASK_HOST, FLASK_PORT)
    socketio.run(
        app,
        host=FLASK_HOST,
        port=FLASK_PORT,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )

if __name__ == "__main__":
    main()
