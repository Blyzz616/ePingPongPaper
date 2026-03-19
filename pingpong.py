#!/usr/bin/python3
"""
=============================================================================
 Ping-Pong Scoring System — Raspberry Pi Zero W v1
 IT8951 800x600 e-paper  +  2x ESP32-C6 MQTT buttons
 Version 2.0
=============================================================================

ASSET INVENTORY (/home/jim/images/)
------------------------------------
Rule-selection (shown directly, full GC16 refresh):
  gamelen.bmp            both buttons connected — choose race-to length
  gl11.bmp               after green tap: race-to-11 chosen, ask best-of
  gl21.bmp               after blue  tap: race-to-21 chosen, ask best-of
  serveask.bmp           "who serves first?" prompt

In-game base images (full GC16 refresh, once per game):
  gl11bo3.bmp            race-to-11, best-of-3
  gl11bo5.bmp            race-to-11, best-of-5
  gl21bo3.bmp            race-to-21, best-of-3
  gl21bo5.bmp            race-to-21, best-of-5

Partial-update overlays (A2 fast refresh):
  serve.bmp              237x82,  placed at x=283, y=27  (always on in-game)
  serveleft.bmp          282x150, placed at x=0,   y=0   (left side serves)
  serveright.bmp         282x150, placed at x=518, y=0   (right side serves)
  serveblank.bmp         282x150, used to erase the inactive arrow

Point-score digit images (0.bmp … 41.bmp), each 330x215:
  Left  point digit:     x=35,  y=218
  Right point digit:     x=424, y=218

Games-won digit images (g0.bmp, g1.bmp, g2.bmp), each 72x106:
  Left  games digit:     x=164, y=477
  Right games digit:     x=565, y=477

Match-over base images (full GC16 refresh):
  gameover.bmp           used for best-of-5 (or extended) matches
  gameover3.bmp          used for best-of-3 matches / extend prompt

Spare assets (not used in code):
  switch.bmp             292x79, reserved for future use
  gl11bo3conf.bmp        confirmation screens — documented but not used
  gl11bo5conf.bmp
  gl21bo3conf.bmp
  gl21bo5conf.bmp

DISPLAY STRATEGY
----------------
  GC16 (mode 2, ~4s): full-screen menu images, base game image once per game,
                       match-over screens.
  A2   (mode 4, ~0.3s): all partial overlay updates during play.

Per-point update sends only the elements that changed:
  - The one score digit that incremented
  - Both arrow images (blank + new) only when serve switches

STATE MACHINE
-------------
  WAITING_BUTTONS  both ESP32s connect
  RULE_RACE        green=11, blue=21
  RULE_BO          green=3,  blue=5
  SERVING_CHOICE   first tap = first server
  PLAYING          live scoring
  WIN_CONFIRM      only used for BO3 tied 1-1 extend-to-5 prompt
  MATCH_OVER       match finished

UNDO
----
GameState is deep-copied onto a stack before every mutation.
After undo in PLAYING state all in-game elements are redrawn as partial
updates (no base image re-flash needed since the base never changes
mid-game).
"""

import copy
import os
import queue
import subprocess
import sys
import threading
import time
import signal
import logging
from datetime import datetime
from enum import Enum, auto

# ── Version ───────────────────────────────────────────────────────────────────
VERSION = "2.0"

# Handle --version / -v before anything else
if "--version" in sys.argv or "-v" in sys.argv:
    print(f"pingpong.py version {VERSION}")
    sys.exit(0)

# ── MQTT ──────────────────────────────────────────────────────────────────────
try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    print("[WARN] paho-mqtt not installed – simulation mode only.")

# =============================================================================
#  CONFIGURATION
# =============================================================================

MQTT_BROKER_HOST     = "localhost"
MQTT_BROKER_PORT     = 1883
MQTT_KEEPALIVE       = 60
MQTT_TOPIC_GREEN     = "button/green"
MQTT_TOPIC_BLUE      = "button/blue"
MQTT_STATUS_GREEN    = "status/green"
MQTT_STATUS_BLUE     = "status/blue"
MQTT_RECONNECT_DELAY = 5

EPAPER_CMD = "/IT8951/IT8951"   # path to the IT8951 display binary

ASSETS  = "/home/jim/images"   # Jim's pre-made artwork

LOG_DIR = "/home/jim/logs"

SIMULATION_MODE = ("-s" in sys.argv or "--sim" in sys.argv)

# ── Display modes ─────────────────────────────────────────────────────────────
DISPLAY_MODE_FULL = 2   # GC16 — full quality, ~4s, for menus and base images
DISPLAY_MODE_FAST = 4   # A2   — binary fast,  ~0.3s, for in-game overlays

# ── Asset dimensions (hardcoded — no runtime detection needed) ────────────────
DIGIT_W        = 330    # 0.bmp – 41.bmp
DIGIT_H        = 215
GAMES_DIGIT_W  = 72     # g0.bmp – g2.bmp
GAMES_DIGIT_H  = 106
SERVE_BAR_W    = 237    # serve.bmp
SERVE_BAR_H    = 82
SERVE_ARROW_W  = 282    # serveleft.bmp, serveright.bmp, serveblank.bmp
SERVE_ARROW_H  = 150

# ── Overlay positions ─────────────────────────────────────────────────────────
LEFT_SCORE_X  = 35
LEFT_SCORE_Y  = 218
RIGHT_SCORE_X = 424
RIGHT_SCORE_Y = 218

SERVE_BAR_X = 283
SERVE_BAR_Y = 27

SERVE_LEFT_X  = 0
SERVE_LEFT_Y  = 0
SERVE_RIGHT_X = 518
SERVE_RIGHT_Y = 0

GAMES_LEFT_X  = 164
GAMES_RIGHT_X = 565
GAMES_Y       = 477


# =============================================================================
#  ASSET PATH HELPERS
# =============================================================================

def asset(name: str) -> str:
    return os.path.join(ASSETS, name)

def digit_path(n: int) -> str:
    return asset(f"{n}.bmp")

def games_digit_path(n: int) -> str:
    """g0/g1/g2 only exist; clamp so we never request a missing file."""
    return asset(f"g{min(n, 2)}.bmp")

def base_image_name(race_to: int, best_of: int) -> str:
    return f"gl{race_to}bo{best_of}.bmp"


# =============================================================================
#  STATE DEFINITIONS
# =============================================================================

class State(Enum):
    WAITING_BUTTONS = auto()
    RULE_RACE       = auto()
    RULE_BO         = auto()
    SERVING_CHOICE  = auto()
    PLAYING         = auto()
    WIN_CONFIRM     = auto()   # only for BO3 tied 1-1 extend prompt
    MATCH_OVER      = auto()


# =============================================================================
#  GAME STATE
# =============================================================================

class GameState:
    """
    Complete match snapshot.  Deep-copied before every mutation.

    POSITIONAL MODEL
    ----------------
    score["left"] / score["right"]      — points for whoever is on that side NOW
    games_won["left"] / ["right"]       — positional game tally

    Players swap ends after every game.  games_won is also swapped at that
    point so the columns remain accurate.

    Green button = always LEFT.   Blue button = always RIGHT.
    server is stored as "left" | "right", never as a colour string.

    serve_num counts every individual serve across the entire match
    (never resets).
    """

    def __init__(self):
        self.race_to      = 11
        self.best_of      = 3

        self.games_won    = {"left": 0, "right": 0}
        self.current_game = 1

        self.score        = {"left": 0, "right": 0}

        self.server       = "left"
        self.serve_count  = 1
        self.serve_num    = 0

        # "gl{race_to}bo{best_of}.bmp" — set once rules are confirmed
        self.base_image   = None

        self.state        = State.WAITING_BUTTONS

        # Used only during the BO3→BO5 extend prompt
        self.extend_prompt = False
        self.game_winner   = None   # "left" | "right"

        # [{left, right, winner_side, winner_colour}, …]
        self.game_history  = []

    @staticmethod
    def colour_to_side(colour: str) -> str:
        return "left" if colour == "green" else "right"

    @staticmethod
    def side_to_colour(side: str) -> str:
        return "green" if side == "left" else "blue"

    def server_colour(self) -> str:
        return self.side_to_colour(self.server)

    def server_side_label(self) -> str:
        return self.server.capitalize()

    def clone(self):
        return copy.deepcopy(self)


# =============================================================================
#  DISPLAY MANAGER
# =============================================================================

class DisplayManager:
    """
    Owns all communication with the e-paper display.

    All display calls are non-blocking — work is queued and executed
    sequentially in a background thread.  Each queue item is a list of
    (path, x, y, mode) tuples executed back-to-back, ensuring that a
    multi-part update (e.g. base image + overlays) completes as an
    atomic batch.

    REFRESH MODES
    -------------
    DISPLAY_MODE_FULL (GC16, ~4s): menus, base game image once per game,
                                   match-over screens.
    DISPLAY_MODE_FAST (A2,  ~0.3s): all in-game overlay updates.

    PER-POINT UPDATE STRATEGY
    -------------------------
    show_score(gs, prev_gs) diffs the two states and sends only what
    changed — typically a single digit image (~0.3s).  Arrow images are
    only sent when the server changes (blank old side, draw new side).

    GAME START
    ----------
    setup_game_screen(gs) sends the full base image as GC16 followed by
    all overlay images as A2 partial updates, all in one atomic batch.
    """

    def __init__(self):
        self._display_queue = queue.Queue()
        threading.Thread(target=self._display_worker, daemon=True).start()

    # ── Worker ────────────────────────────────────────────────────────────

    def _display_worker(self):
        """Drain the display queue one batch at a time."""
        while True:
            items = self._display_queue.get()
            for path, x, y, mode in items:
                if not os.path.exists(path):
                    logging.error(f"[Display] Missing: {path}")
                    continue
                logging.info(f"[Display] -> {path} @ ({x},{y}) mode={mode}")
                try:
                    subprocess.run(
                        [EPAPER_CMD, str(x), str(y), path, str(mode)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=30,
                    )
                except subprocess.TimeoutExpired:
                    logging.error(f"[Display] IT8951 timed out: {path}")
                except Exception as e:
                    logging.error(f"[Display] IT8951 failed: {e}")
            self._display_queue.task_done()

    def _flush_queue(self):
        """Drop any pending (not yet started) display batches."""
        while not self._display_queue.empty():
            try:
                self._display_queue.get_nowait()
                self._display_queue.task_done()
            except queue.Empty:
                break

    def _send(self, items: list):
        """Queue a batch of (path, x, y, mode) tuples as one atomic unit."""
        self._display_queue.put(items)

    # ── Public API ────────────────────────────────────────────────────────

    def show_asset(self, filename: str):
        """Full-screen menu image — GC16 full refresh."""
        self._flush_queue()
        self._send([(asset(filename), 0, 0, DISPLAY_MODE_FULL)])

    def show_file(self, path: str, mode: int = DISPLAY_MODE_FULL):
        """Full-screen file — used internally for match-over etc."""
        self._flush_queue()
        self._send([(path, 0, 0, mode)])

    def update_elements(self, elements: list):
        """
        Partial A2 update.  elements is a list of (path, x, y).
        All sent as one atomic batch.
        """
        self._flush_queue()
        self._send([(p, x, y, DISPLAY_MODE_FAST) for p, x, y in elements])

    # ── Arrow helpers ─────────────────────────────────────────────────────

    def _arrow_elements(self, server: str) -> list:
        """
        Return (path, x, y) list that blanks the inactive arrow side
        and draws the active one.  Always both sides to ensure correctness
        regardless of what is currently on screen.
        """
        if server == "left":
            return [
                (asset("serveblank.bmp"), SERVE_RIGHT_X, SERVE_RIGHT_Y),
                (asset("serveleft.bmp"),  SERVE_LEFT_X,  SERVE_LEFT_Y),
            ]
        else:
            return [
                (asset("serveblank.bmp"),  SERVE_LEFT_X,  SERVE_LEFT_Y),
                (asset("serveright.bmp"),  SERVE_RIGHT_X, SERVE_RIGHT_Y),
            ]

    # ── Game screen setup ─────────────────────────────────────────────────

    def setup_game_screen(self, gs: GameState):
        """
        Full GC16 base image followed by A2 partial overlays.
        Always called at score 0-0 (start of every game).
        All items sent as one atomic batch so they cannot be interrupted.
        Only the correct arrow side is drawn (no blank needed — the base
        image has no arrows).
        """
        self._flush_queue()

        if gs.server == "left":
            arrow = (asset("serveleft.bmp"), SERVE_LEFT_X, SERVE_LEFT_Y)
        else:
            arrow = (asset("serveright.bmp"), SERVE_RIGHT_X, SERVE_RIGHT_Y)

        items = [
            # Full base image — GC16
            (asset(gs.base_image),                    0,             0,             DISPLAY_MODE_FULL),
            # Overlays — A2
            (asset("serve.bmp"),                      SERVE_BAR_X,   SERVE_BAR_Y,   DISPLAY_MODE_FAST),
            (digit_path(0),                           LEFT_SCORE_X,  LEFT_SCORE_Y,  DISPLAY_MODE_FAST),
            (digit_path(0),                           RIGHT_SCORE_X, RIGHT_SCORE_Y, DISPLAY_MODE_FAST),
            (games_digit_path(gs.games_won["left"]),  GAMES_LEFT_X,  GAMES_Y,       DISPLAY_MODE_FAST),
            (games_digit_path(gs.games_won["right"]), GAMES_RIGHT_X, GAMES_Y,       DISPLAY_MODE_FAST),
            (*arrow,                                                                 DISPLAY_MODE_FAST),
        ]
        self._send(items)

    # ── Per-point update ──────────────────────────────────────────────────

    def show_score(self, gs: GameState, prev_gs: GameState):
        """
        Diff gs against prev_gs and send only the changed elements
        as A2 partial updates.  Typically a single digit image (~0.3s).
        Arrow images are only sent when the server changes.
        """
        elements = []

        if gs.score["left"] != prev_gs.score["left"]:
            elements.append(
                (digit_path(gs.score["left"]), LEFT_SCORE_X, LEFT_SCORE_Y)
            )

        if gs.score["right"] != prev_gs.score["right"]:
            elements.append(
                (digit_path(gs.score["right"]), RIGHT_SCORE_X, RIGHT_SCORE_Y)
            )

        if gs.server != prev_gs.server:
            elements += self._arrow_elements(gs.server)

        if gs.games_won["left"] != prev_gs.games_won["left"]:
            elements.append(
                (games_digit_path(gs.games_won["left"]), GAMES_LEFT_X, GAMES_Y)
            )

        if gs.games_won["right"] != prev_gs.games_won["right"]:
            elements.append(
                (games_digit_path(gs.games_won["right"]), GAMES_RIGHT_X, GAMES_Y)
            )

        if elements:
            self.update_elements(elements)

    def show_all_elements(self, gs: GameState):
        """
        Redraw every in-game overlay as A2 partial updates.
        Used after undo so the screen reflects the restored state
        without needing a slow full base-image refresh.
        _arrow_elements always blanks the inactive side and draws the
        active side, guaranteeing correctness regardless of prior state.
        """
        elements = [
            (asset("serve.bmp"),                     SERVE_BAR_X,   SERVE_BAR_Y),
            (digit_path(gs.score["left"]),            LEFT_SCORE_X,  LEFT_SCORE_Y),
            (digit_path(gs.score["right"]),           RIGHT_SCORE_X, RIGHT_SCORE_Y),
            (games_digit_path(gs.games_won["left"]),  GAMES_LEFT_X,  GAMES_Y),
            (games_digit_path(gs.games_won["right"]), GAMES_RIGHT_X, GAMES_Y),
        ] + self._arrow_elements(gs.server)
        self.update_elements(elements)

    # ── Match-over screen ─────────────────────────────────────────────────

    def show_match_over(self, gs: GameState):
        """
        Full GC16 gameover base image then A2 partial updates for the
        two games-won digits.  Sent as one atomic batch.
        gameover.bmp for BO5/extended, gameover3.bmp for BO3.
        """
        self._flush_queue()
        base = asset("gameover.bmp" if gs.best_of >= 5 else "gameover3.bmp")
        items = [
            (base,                                    0,             0,             DISPLAY_MODE_FULL),
            (digit_path(gs.games_won["left"]),        LEFT_SCORE_X,  LEFT_SCORE_Y,  DISPLAY_MODE_FAST),
            (digit_path(gs.games_won["right"]),       RIGHT_SCORE_X, RIGHT_SCORE_Y, DISPLAY_MODE_FAST),
        ]
        self._send(items)


# =============================================================================
#  LOGGER
# =============================================================================

class MatchLogger:
    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        epoch    = int(time.time())
        path     = os.path.join(LOG_DIR, f"{epoch}.txt")
        self._fh = open(path, "w", buffering=1)
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )
        self._log = logging.getLogger("pingpong")
        self._log.info(f"Log file: {path}")

    def _ts(self) -> str:
        now = datetime.now()
        day = now.strftime("%-d").rjust(2)
        return now.strftime(f"%a {day} %b %H:%M:%S PST %Y")

    def write(self, text: str):
        self._fh.write(text + "\n")
        self._fh.flush()

    def blank(self):
        self.write("")

    def event(self, msg: str):
        line = f"{self._ts()} - {msg}"
        self.write(line)
        self._log.info(line)

    def serve_header(self, gs: GameState):
        colour = gs.server_colour().capitalize()
        side   = gs.server_side_label()
        self.write(f"{colour}/{side} serving ({gs.serve_count})")

    def serve_change(self):
        self.write("Change of serve")
        self.blank()

    def close(self):
        self._fh.close()


# =============================================================================
#  PURE GAME LOGIC
# =============================================================================

def _advance_serve(gs: GameState) -> bool:
    """
    Advance serve counter.
    serve_num increments on every serve.
    serve_count cycles 1->2->(rotate server)->1.
    Returns True if the server changed.
    """
    gs.serve_num += 1
    if gs.serve_count == 1:
        gs.serve_count = 2
        return False
    else:
        gs.serve_count = 1
        gs.server = "right" if gs.server == "left" else "left"
        return True


def _apply_point(gs: GameState, side: str) -> bool:
    """Award a point to side and advance serve. Returns True if server changed."""
    gs.score[side] += 1
    return _advance_serve(gs)


def check_game_win(gs: GameState):
    """
    Return "left", "right", or None.
    Win: score >= race_to AND lead >= 2 (win-by-two).
    """
    l, r = gs.score["left"], gs.score["right"]
    if (l >= gs.race_to or r >= gs.race_to) and abs(l - r) >= 2:
        return "left" if l > r else "right"
    return None


def swap_games_won(gs: GameState):
    """Swap positional games tally when players change ends."""
    gs.games_won["left"], gs.games_won["right"] = (
        gs.games_won["right"], gs.games_won["left"])


def start_new_game(gs: GameState, winning_side: str):
    """
    Set up the next game.

    winning_side is the side BEFORE the end-of-game swap.
    After the swap the winner is on the opposite side.
    The winner serves first in the new game, so server is set to
    the opposite of winning_side.

    serve_num continues incrementing across games.
    """
    new_server = "right" if winning_side == "left" else "left"
    swap_games_won(gs)
    gs.score        = {"left": 0, "right": 0}
    gs.current_game += 1
    gs.server       = new_server
    gs.serve_count  = 1


def match_winner(gs: GameState):
    """Return "left", "right", or None."""
    needed = (gs.best_of // 2) + 1
    for side in ("left", "right"):
        if gs.games_won[side] >= needed:
            return side
    return None


# =============================================================================
#  MATCH ENGINE
# =============================================================================

class MatchEngine:
    """
    Central controller.  Owns GameState, undo stack, display, and logger.

    Per-point flow:
      1. Snapshot prev_gs before mutation
      2. Push undo snapshot
      3. Apply the point (mutates gs)
      4. If game win → _handle_game_win
      5. Otherwise  → display.show_score(gs, prev_gs) — partial diff update
      6. Log serve state
    """

    def __init__(self, display: DisplayManager, logger: MatchLogger):
        self.display = display
        self.logger  = logger
        self.gs      = GameState()

        self._undo_stack: list[GameState] = []
        self._connected  = {"green": False, "blue": False}
        self.event_queue = queue.Queue()

    def _push_undo(self):
        self._undo_stack.append(self.gs.clone())

    def _pop_undo(self) -> bool:
        if self._undo_stack:
            self.gs = self._undo_stack.pop()
            return True
        return False

    # ── Button dispatcher ─────────────────────────────────────────────────

    def handle_button(self, colour: str, press_type: str):
        if press_type == "long":
            self.logger.event(f"{colour.capitalize()} long press — full reset.")
            self._full_reset()
        elif press_type == "double":
            self._handle_undo(colour)
        elif press_type == "short":
            self._handle_short(colour)

    def _handle_short(self, colour: str):
        gs    = self.gs
        state = gs.state

        if state == State.WAITING_BUTTONS:
            pass

        # ── RULE_RACE: gamelen.bmp on screen; green=11, blue=21 ───────────
        elif state == State.RULE_RACE:
            self._push_undo()
            gs.race_to = 11 if colour == "green" else 21
            self.logger.event(f"{colour.capitalize()} pressed – Race to {gs.race_to}")
            gs.state = State.RULE_BO
            self.display.show_asset(f"gl{gs.race_to}.bmp")

        # ── RULE_BO: gl11/gl21.bmp on screen; green=3, blue=5 ─────────────
        elif state == State.RULE_BO:
            self._push_undo()
            gs.best_of    = 3 if colour == "green" else 5
            gs.base_image = base_image_name(gs.race_to, gs.best_of)
            self.logger.event(
                f"{colour.capitalize()} pressed – Best of {gs.best_of}. "
                f"Base image: {gs.base_image}"
            )
            gs.state = State.SERVING_CHOICE
            self.display.show_asset("serveask.bmp")

        # ── SERVING_CHOICE: first tap = first server ───────────────────────
        elif state == State.SERVING_CHOICE:
            self._push_undo()
            side           = GameState.colour_to_side(colour)
            gs.server      = side
            gs.serve_count = 1
            gs.serve_num   = 1   # first serve of the match
            gs.state       = State.PLAYING

            self.logger.event(
                f"{colour.capitalize()} pressed – "
                f"{colour.capitalize()}/{side.capitalize()} serves first."
            )
            self.logger.blank()
            self.logger.serve_header(gs)

            # Full base image + all overlays at 0-0
            self.display.setup_game_screen(gs)

        # ── PLAYING ────────────────────────────────────────────────────────
        elif state == State.PLAYING:
            self._handle_score(colour)

        # ── WIN_CONFIRM: only reached for BO3 tied 1-1 extend prompt ───────
        elif state == State.WIN_CONFIRM:
            self._handle_win_confirm(colour)

        # ── MATCH_OVER: long press resets; short press re-shows summary ────
        elif state == State.MATCH_OVER:
            self.display.show_match_over(self.gs)

    # ── Score a point ─────────────────────────────────────────────────────

    def _handle_score(self, colour: str):
        gs   = self.gs
        side = GameState.colour_to_side(colour)

        # Snapshot before mutation — used to diff what changed for display
        prev_gs = gs.clone()

        # Save undo snapshot and apply the point
        self._push_undo()
        changed_server = _apply_point(gs, side)

        score_str = f"{gs.score['left']}-{gs.score['right']}"
        self.logger.event(
            f"{colour.capitalize()} button pressed. "
            f"{colour.capitalize()} scores. {score_str}"
        )

        # Check for game win before showing score
        winning_side = check_game_win(gs)
        if winning_side:
            self._handle_game_win(winning_side, changed_server)
            return

        # Normal point — partial update of only what changed
        self.display.show_score(gs, prev_gs)

        if changed_server:
            self.logger.serve_change()
        else:
            self.logger.blank()
        self.logger.serve_header(gs)

    # ── Game won ──────────────────────────────────────────────────────────

    def _handle_game_win(self, winning_side: str, changed_server: bool):
        """
        Called immediately after check_game_win() returns a winner.

        Behaviour:
          - Always auto-advance to next game (no confirmation required).
          - Exception: BO3 match complete — pause and ask to extend to BO5.
          - Match winner: show match-over screen, enter MATCH_OVER state.
        """
        gs            = self.gs
        winner_colour = GameState.side_to_colour(winning_side)

        gs.game_history.append({
            "left":          gs.score["left"],
            "right":         gs.score["right"],
            "winner_side":   winning_side,
            "winner_colour": winner_colour,
        })
        gs.games_won[winning_side] += 1
        gs.game_winner = winning_side

        if changed_server:
            self.logger.serve_change()
        else:
            self.logger.blank()
        self.logger.event(
            f"{winner_colour.capitalize()} wins game {gs.current_game}!  "
            f"Games: left {gs.games_won['left']} – {gs.games_won['right']} right"
        )

        m_winner = match_winner(gs)

        if m_winner and gs.best_of == 3:
            # BO3 match complete — offer extend to BO5
            gs.state         = State.WIN_CONFIRM
            gs.extend_prompt = True
            self.display.show_match_over(gs)
            self.logger.event(
                f"Best-of-3 complete: left {gs.games_won['left']} – "
                f"{gs.games_won['right']} right.  "
                "Green = extend to best of 5.  Blue = new match."
            )
            return

        if m_winner:
            # BO5 (or extended) match complete
            gs.state = State.MATCH_OVER
            self.display.show_match_over(gs)
            self._log_match_summary()
            return

        # No match winner yet — auto-advance to next game
        start_new_game(gs, winning_side)
        gs.state = State.PLAYING
        self.logger.blank()
        self.logger.serve_header(gs)

        # Full base image refresh + all overlays for new game at 0-0
        self.display.setup_game_screen(gs)

    # ── BO3 end-of-match prompt ───────────────────────────────────────────

    def _handle_win_confirm(self, colour: str):
        """
        Reached after any BO3 match completes.

        Green = extend to best of 5.  Game history and scores carry over.
                Winner of the last game serves first in the next game.

        Blue  = start a completely new match from scratch.
        """
        gs = self.gs

        if colour == "blue":
            self.logger.event(
                "Blue pressed – not extending. Starting new match from scratch."
            )
            self._full_reset()
            return

        # ── Green: extend to best of 5 ────────────────────────────────────
        self.logger.event("Green pressed – extending to best of 5!")
        self._push_undo()

        winning_side     = gs.game_winner
        gs.best_of       = 5
        gs.extend_prompt = False
        gs.base_image    = base_image_name(gs.race_to, gs.best_of)

        start_new_game(gs, winning_side)
        gs.state = State.PLAYING
        self.logger.blank()
        self.logger.serve_header(gs)

        self.display.setup_game_screen(gs)

    # ── Undo ──────────────────────────────────────────────────────────────

    def _handle_undo(self, colour: str):
        if not self._undo_stack:
            self.logger.event(f"{colour.capitalize()} double pressed – nothing to undo.")
            return

        self._pop_undo()
        gs = self.gs

        score_str = f"{gs.score['left']}-{gs.score['right']}"
        self.logger.event(
            f"{colour.capitalize()} double pressed. Score reverted. {score_str}"
        )
        self.logger.blank()
        self.logger.serve_header(gs)

        if gs.state == State.PLAYING:
            # Redraw all in-game elements as partial updates.
            # _arrow_elements blanks the inactive side, so this is correct
            # regardless of what was on screen before the undo.
            self.display.show_all_elements(gs)
        elif gs.state == State.WIN_CONFIRM:
            # Undoing back into the extend prompt — re-show match-over screen
            self.display.show_match_over(gs)
        else:
            self._redraw_menu_state()

    # ── Full reset ────────────────────────────────────────────────────────

    def _full_reset(self):
        self.gs          = GameState()
        self._undo_stack = []
        self._connected  = {"green": False, "blue": False}
        self.gs.state    = State.WAITING_BUTTONS
        self.logger.blank()
        self.logger.event("=== FULL RESET ===")
        self.logger.blank()
        self._log_connection_status()

    # ── Connection ────────────────────────────────────────────────────────

    def on_button_connected(self, colour: str):
        self._connected[colour] = True
        self.logger.event(f"{colour.capitalize()} button connected.")
        self._log_connection_status()
        if all(self._connected.values()):
            self.logger.event("Both buttons connected – showing rule selection.")
            self.gs.state = State.RULE_RACE
            self.display.show_asset("gamelen.bmp")

    def _log_connection_status(self):
        g = "connected" if self._connected["green"] else "waiting"
        b = "connected" if self._connected["blue"]  else "waiting"
        self.logger.event(f"Green: {g}  Blue: {b}")

    # ── Logging helpers ───────────────────────────────────────────────────

    def _log_match_summary(self):
        gs = self.gs
        w  = match_winner(gs)
        wc = GameState.side_to_colour(w) if w else "unknown"
        self.logger.blank()
        self.logger.event(f"=== MATCH OVER – Winner: {wc.upper()} ===")
        for i, g in enumerate(gs.game_history, 1):
            self.logger.event(
                f"  Game {i}: left {g['left']} – {g['right']} right "
                f"({g['winner_colour']} wins)"
            )
        self.logger.event(
            f"  Final games: "
            f"left {gs.games_won['left']} – {gs.games_won['right']} right"
        )
        self.logger.event("Long press either button to start a new match.")

    # ── Menu / state redraw (after undo) ──────────────────────────────────

    def _redraw_menu_state(self):
        s  = self.gs.state
        gs = self.gs
        if s == State.RULE_RACE:
            self.display.show_asset("gamelen.bmp")
        elif s == State.RULE_BO:
            self.display.show_asset(f"gl{gs.race_to}.bmp")
        elif s == State.SERVING_CHOICE:
            self.display.show_asset("serveask.bmp")
        elif s == State.MATCH_OVER:
            self.display.show_match_over(gs)

    # ── Main event loop ───────────────────────────────────────────────────

    def run(self):
        self.logger.event(f"Ping-pong scorer v{VERSION} started. Waiting for buttons.")
        while True:
            try:
                colour, press_type = self.event_queue.get(timeout=1)
                self.handle_button(colour, press_type)
            except queue.Empty:
                pass
            except Exception as e:
                logging.exception(f"[Engine] Unhandled error (continuing): {e}")


# =============================================================================
#  MQTT CLIENT
# =============================================================================

class MQTTClient:
    def __init__(self, engine: MatchEngine):
        self.engine  = engine
        self._client = None

    def start(self):
        if not MQTT_AVAILABLE:
            logging.warning("paho-mqtt not available – MQTT disabled.")
            return
        self._client = mqtt.Client(client_id="pingpong_pi")
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                self._client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_KEEPALIVE)
                self._client.loop_forever()
            except Exception as e:
                logging.error(f"[MQTT] Failed: {e}. Retry in {MQTT_RECONNECT_DELAY}s")
                time.sleep(MQTT_RECONNECT_DELAY)

    def _on_connect(self, client, userdata, flags, rc):
        logging.info(f"[MQTT] Connected (rc={rc})")
        client.subscribe(MQTT_TOPIC_GREEN)
        client.subscribe(MQTT_TOPIC_BLUE)
        client.subscribe(MQTT_STATUS_GREEN)
        client.subscribe(MQTT_STATUS_BLUE)

    def _on_disconnect(self, client, userdata, rc):
        logging.warning(f"[MQTT] Disconnected (rc={rc}). Reconnecting…")

    def _on_message(self, client, userdata, msg):
        topic   = msg.topic
        payload = msg.payload.decode().strip().lower()
        logging.debug(f"[MQTT] {topic} -> {payload}")
        if topic == MQTT_STATUS_GREEN and payload == "connected":
            self.engine.on_button_connected("green")
        elif topic == MQTT_STATUS_BLUE and payload == "connected":
            self.engine.on_button_connected("blue")
        elif topic == MQTT_TOPIC_GREEN and payload in ("short", "double", "long"):
            self.engine.event_queue.put(("green", payload))
        elif topic == MQTT_TOPIC_BLUE and payload in ("short", "double", "long"):
            self.engine.event_queue.put(("blue", payload))


# =============================================================================
#  SIMULATION MODE
# =============================================================================

def run_simulation(engine: MatchEngine):
    print(f"\n=== SIMULATION MODE (v{VERSION}) ===")
    print("  connect    – both buttons connect")
    print("  g / b      – short press")
    print("  gg / bb    – double press (undo)")
    print("  GL / BL    – long press  (full reset)")
    print()

    def _loop():
        while True:
            try:
                raw = input("sim> ").strip()
            except EOFError:
                break
            if not raw:
                continue
            if raw == "connect":
                engine.on_button_connected("green")
                engine.on_button_connected("blue")
            elif raw == "g":
                engine.event_queue.put(("green", "short"))
            elif raw == "b":
                engine.event_queue.put(("blue", "short"))
            elif raw == "gg":
                engine.event_queue.put(("green", "double"))
            elif raw == "bb":
                engine.event_queue.put(("blue", "double"))
            elif raw.upper() == "GL":
                engine.event_queue.put(("green", "long"))
            elif raw.upper() == "BL":
                engine.event_queue.put(("blue", "long"))
            else:
                print("  Unknown command.")

    threading.Thread(target=_loop, daemon=True).start()


# =============================================================================
#  ENTRY POINT
# =============================================================================

def main():
    logger  = MatchLogger()
    display = DisplayManager()
    engine  = MatchEngine(display, logger)

    def _shutdown(sig, frame):
        logger.event("Shutdown signal received.")
        logger.close()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if SIMULATION_MODE:
        run_simulation(engine)
    else:
        MQTTClient(engine).start()

    engine.run()


if __name__ == "__main__":
    main()
