#!/usr/bin/python3
"""
=============================================================================
 Ping-Pong Scoring System — Raspberry Pi Zero W v1
 IT8951 800x600 e-paper  +  2x ESP32-C6 MQTT buttons
 Version 0.9
=============================================================================

ASSET INVENTORY (/home/jim/images/)
------------------------------------
Rule-selection (shown directly, no compositing):
  gamelen.bmp            both buttons connected — choose race-to length
  gl11.bmp               after green tap: race-to-11 chosen, ask best-of
  gl21.bmp               after blue  tap: race-to-21 chosen, ask best-of
  gl11bo3conf.bmp        confirmation screen: race-to-11, best-of-3
  gl11bo5conf.bmp        confirmation screen: race-to-11, best-of-5
  gl21bo3conf.bmp        confirmation screen: race-to-21, best-of-3
  gl21bo5conf.bmp        confirmation screen: race-to-21, best-of-5
  serveask.bmp           "who serves first?" prompt

In-game base images (composited onto for every score image):
  gl11bo3.bmp            race-to-11, best-of-3
  gl11bo5.bmp            race-to-11, best-of-5
  gl21bo3.bmp            race-to-21, best-of-3
  gl21bo5.bmp            race-to-21, best-of-5

serve.bmp overlay (always on in-game screens):
  serve.bmp              placed at x=283, y=27

Serve-side arrow overlays:
  serveleft.bmp          placed at x=0,   y=0   (left side serves next)
  serveright.bmp         placed at x=518, y=0   (right side serves next)

Point-score digit images (0.bmp … 41.bmp):
  Left  point digit:     x=35,  y=218
  Right point digit:     x=424, y=218

Games-won digit images (g0.bmp, g1.bmp, g2.bmp):
  Left  games digit:     x=164, y=477
  Right games digit:     x=565, y=477

End-of-game (next-game start) screen — auto-displayed when a game is won:
  Same base image as in-game
  + serve.bmp @ (283, 27)
  + serve-side overlay for who serves next
    (winner of previous game serves — but they've swapped sides, so
     if left won  -> winner is now on right -> serveright.bmp
     if right won -> winner is now on left  -> serveleft.bmp)
  + 0.bmp @ left  point position
  + 0.bmp @ right point position
  + gN.bmp @ left  games position  (post-swap games tally)
  + gN.bmp @ right games position

Match-over screen — auto-displayed when match is won:
  gameover.bmp  (BO5) or  gameover3.bmp  (BO3/extended)  as base
  + large point-digit at LEFT  position = left  games won
  + large point-digit at RIGHT position = right games won
  NO serve overlays, NO gN images (games tally IS the main score)

PRE-GENERATION STRATEGY
-----------------------
After every serve, two BMPs are built in a background thread:
  /tmp/<serve_num:02d>.<next_server>.<left>-<right>.bmp

serve_num  — global monotonic counter, never resets across games.
next_server — "left" or "right" — included because the same score at the
              same serve_num can require different overlays depending on
              whether this serve triggers a rotation or not.

UNDO
----
GameState is deep-copied onto a stack before every mutation.
Restoring a snapshot gives back the exact filename keys needed to re-
display or re-pregenerate — no extra bookkeeping.

STATE MACHINE
-------------
  WAITING_BUTTONS  both ESP32s connect
  RULE_RACE        green=11, blue=21
  RULE_BO          green=3,  blue=5
  CONFIRM_RULES    one tap confirms
  SERVING_CHOICE   first tap = first server
  PLAYING          live scoring
  WIN_CONFIRM      only used for BO3 tied 1-1 extend-to-5 prompt
  MATCH_OVER       match finished
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
VERSION = "0.8"

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
TMP_DIR = "/tmp"               # where composited score images are written

# A unique tag for this process run.  Embedding it in every /tmp filename
# means stale files from previous sessions are never reused.  (Stale files
# without the gN-layer overlay — from an older version of the code — would
# be silently reused without this tag, causing wrong images to be shown.)
import time as _time
SESSION_ID = str(int(_time.time()))

# Point-score digit positions
LEFT_SCORE_X  = 35
LEFT_SCORE_Y  = 218
RIGHT_SCORE_X = 424
RIGHT_SCORE_Y = 218

# serve.bmp overlay position (always shown during play)
SERVE_BAR_X = 283
SERVE_BAR_Y = 27

# Serve-side arrow positions
SERVE_LEFT_X  = 0
SERVE_LEFT_Y  = 0
SERVE_RIGHT_X = 518
SERVE_RIGHT_Y = 0

# Games-won digit positions
GAMES_LEFT_X  = 164
GAMES_RIGHT_X = 565
GAMES_Y       = 477

LOG_DIR = "logs"

SIMULATION_MODE = ("-s" in sys.argv or "--sim" in sys.argv)


# =============================================================================
#  ASSET PATH HELPERS
# =============================================================================

def asset(name: str) -> str:
    return os.path.join(ASSETS, name)

def digit_path(n: int) -> str:
    return asset(f"{n}.bmp")

def games_digit_path(n: int) -> str:
    return asset(f"g{n}.bmp")

def tmp_score_path(serve_num: int, next_server: str, left: int, right: int) -> str:
    """
    /tmp/<SESSION_ID>.<serve_num:02d>.<next_server>.<left>-<right>.bmp
    e.g.  /tmp/1741234567.03.left.1-1.bmp

    SESSION_ID prevents files from a previous run (possibly built with an
    older code version that lacked certain overlay layers) from being reused.
    next_server is included because the same score at the same serve_num can
    require different overlays depending on whether a serve rotation occurred.
    """
    return os.path.join(TMP_DIR, f"{SESSION_ID}.{serve_num:02d}.{next_server}.{left}-{right}.bmp")


# =============================================================================
#  STATE DEFINITIONS
# =============================================================================

class State(Enum):
    WAITING_BUTTONS = auto()
    RULE_RACE       = auto()
    RULE_BO         = auto()
    CONFIRM_RULES   = auto()
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
    (never resets).  It is the primary key for pre-generated filenames,
    so undo needs no extra bookkeeping — just restore the snapshot.
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
#  COMPOSITOR
# =============================================================================

class Compositor:
    """
    Thin ImageMagick wrapper.  All artwork is pre-made; this class only
    composites layers together.

    IN-GAME SCORE image (7 layers):
      1. base (gl11bo3.bmp etc.)
      2. serve.bmp           @ (SERVE_BAR_X, SERVE_BAR_Y)
      3. serveleft/right.bmp @ serve-arrow position
      4. left  point digit   @ (LEFT_SCORE_X,  LEFT_SCORE_Y)
      5. right point digit   @ (RIGHT_SCORE_X, RIGHT_SCORE_Y)
      6. left  gN digit      @ (GAMES_LEFT_X,  GAMES_Y)
      7. right gN digit      @ (GAMES_RIGHT_X, GAMES_Y)

    END-OF-GAME / NEXT-GAME START image (7 layers):
      same structure, but point digits are 0-0 and the serve arrow
      reflects the winner's new side (players have swapped).

    MATCH-OVER image (3 layers):
      1. gameover.bmp or gameover3.bmp
      2. left  point digit = left  games won
      3. right point digit = right games won
      (no serve arrow, no gN digits — the point digit IS the game tally)
    """

    @staticmethod
    def run(args: list, outfile: str) -> bool:
        cmd = ["convert"] + args + [outfile]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=15)
            if r.returncode != 0:
                logging.warning(f"[IM] {r.stderr.decode().strip()}")
                return False
            return True
        except Exception as e:
            logging.error(f"[IM] exception: {e}")
            return False

    @staticmethod
    def build_score(
        base_img: str,
        serve_overlay: str, serve_x: int, serve_y: int,
        left_score: int, right_score: int,
        left_games: int, right_games: int,
        outfile: str,
    ) -> bool:
        """Build one in-game (or start-of-game) score composite."""
        for p in (
            base_img,
            asset("serve.bmp"),
            serve_overlay,
            digit_path(left_score),
            digit_path(right_score),
            games_digit_path(left_games),
            games_digit_path(right_games),
        ):
            if not os.path.exists(p):
                logging.error(f"[Compositor] Missing asset: {p}")
                return False

        args = [
            base_img,
            asset("serve.bmp"),
            "-geometry", f"+{SERVE_BAR_X}+{SERVE_BAR_Y}", "-composite",
            serve_overlay,
            "-geometry", f"+{serve_x}+{serve_y}", "-composite",
            digit_path(left_score),
            "-geometry", f"+{LEFT_SCORE_X}+{LEFT_SCORE_Y}", "-composite",
            digit_path(right_score),
            "-geometry", f"+{RIGHT_SCORE_X}+{RIGHT_SCORE_Y}", "-composite",
            games_digit_path(left_games),
            "-geometry", f"+{GAMES_LEFT_X}+{GAMES_Y}", "-composite",
            games_digit_path(right_games),
            "-geometry", f"+{GAMES_RIGHT_X}+{GAMES_Y}", "-composite",
        ]
        return Compositor.run(args, outfile)

    @staticmethod
    def build_match_over(
        best_of: int,
        left_games: int, right_games: int,
        outfile: str,
    ) -> bool:
        """
        Match-over screen.
        gameover.bmp (BO5) or gameover3.bmp (BO3/extended).
        Large point-digit slots show games won — no gN images, no serve arrow.
        games_won is NOT swapped here; we display it as-is at the win moment.
        """
        base_img = asset("gameover.bmp" if best_of >= 5 else "gameover3.bmp")
        for p in (base_img, digit_path(left_games), digit_path(right_games)):
            if not os.path.exists(p):
                logging.error(f"[Compositor] Missing asset: {p}")
                return False

        args = [
            base_img,
            digit_path(left_games),
            "-geometry", f"+{LEFT_SCORE_X}+{LEFT_SCORE_Y}", "-composite",
            digit_path(right_games),
            "-geometry", f"+{RIGHT_SCORE_X}+{RIGHT_SCORE_Y}", "-composite",
        ]
        return Compositor.run(args, outfile)


# =============================================================================
#  DISPLAY MANAGER
# =============================================================================

class DisplayManager:
    """
    Owns:
      - Sending images to the e-paper
      - Building composite score images on demand
      - Pre-generating the next two images in a background thread

    SERVE OVERLAY RULE — always shows who serves NEXT
    --------------------------------------------------
    Every image is built using the server state AFTER _advance_serve()
    has been called for that point.  This is achieved in pregenerate()
    by cloning gs and running _apply_point() before reading gs.server.

    END-OF-GAME SERVE OVERLAY
    -------------------------
    When a game is won, start_new_game() sets gs.server to the winner's
    NEW side (which is the opposite of the winning side, because ends swap).
    So gs.server at the start of the new game is already the correct
    next-server for the end-of-game image.

    FILENAME SCHEME
    ---------------
    /tmp/<serve_num:02d>.<next_server>.<left>-<right>.bmp
    """

    def __init__(self):
        self._pregen_lock = threading.Lock()

    def show_file(self, path: str):
        if not os.path.exists(path):
            logging.error(f"[Display] File not found: {path}")
            return
        logging.info(f"[Display] -> {path}")
        try:
            subprocess.Popen(
                [EPAPER_CMD, "0", "0", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logging.error(f"[Display] IT8951 failed: {e}")

    def show_asset(self, filename: str):
        self.show_file(asset(filename))

    def _overlay_for(self, server: str) -> tuple:
        """Return (overlay_path, x, y) for the given server side."""
        if server == "left":
            return asset("serveleft.bmp"), SERVE_LEFT_X, SERVE_LEFT_Y
        return asset("serveright.bmp"), SERVE_RIGHT_X, SERVE_RIGHT_Y

    # ── Score image ───────────────────────────────────────────────────────

    def build_score_image(
        self,
        base_image: str,
        next_server: str,
        left_score: int,
        right_score: int,
        left_games: int,
        right_games: int,
        serve_num: int,
    ) -> str:
        """
        Composite one in-game score BMP and return its path.
        next_server is the server for the NEXT point.
        """
        outfile = tmp_score_path(serve_num, next_server, left_score, right_score)
        if os.path.exists(outfile):
            return outfile
        overlay, sx, sy = self._overlay_for(next_server)
        Compositor.build_score(
            base_img      = asset(base_image),
            serve_overlay = overlay,
            serve_x       = sx,
            serve_y       = sy,
            left_score    = left_score,
            right_score   = right_score,
            left_games    = left_games,
            right_games   = right_games,
            outfile       = outfile,
        )
        return outfile

    def show_score(self, gs: GameState):
        """Show the score image matching the current GameState (builds if missing)."""
        path = tmp_score_path(
            gs.serve_num, gs.server,
            gs.score["left"], gs.score["right"],
        )
        if not os.path.exists(path):
            self.build_score_image(
                base_image  = gs.base_image,
                next_server = gs.server,
                left_score  = gs.score["left"],
                right_score = gs.score["right"],
                left_games  = gs.games_won["left"],
                right_games = gs.games_won["right"],
                serve_num   = gs.serve_num,
            )
        self.show_file(path)

    # ── End-of-game / next-game start image ──────────────────────────────

    def build_new_game_image(self, gs: GameState) -> str:
        """
        Build the start-of-new-game image (score 0-0, games tally updated,
        serve arrow for whoever serves the next game).

        Called AFTER start_new_game() has already:
          - swapped games_won
          - set gs.server to the winner's new side
          - reset score to 0-0

        gs.server is therefore the correct next-server overlay.

        Filename is keyed on game number and games tally so it's stable
        and unambiguous (not serve-keyed, since serve_num hasn't advanced yet).
        """
        gl = gs.games_won["left"]
        gr = gs.games_won["right"]
        outfile = os.path.join(
            TMP_DIR, f"{SESSION_ID}.newgame.g{gs.current_game}.{gl}-{gr}.bmp"
        )
        if not os.path.exists(outfile):
            overlay, sx, sy = self._overlay_for(gs.server)
            ok = Compositor.build_score(
                base_img      = asset(gs.base_image),
                serve_overlay = overlay,
                serve_x       = sx,
                serve_y       = sy,
                left_score    = 0,
                right_score   = 0,
                left_games    = gl,
                right_games   = gr,
                outfile       = outfile,
            )
            if not ok or not os.path.exists(outfile):
                logging.error(f"[Display] build_new_game_image FAILED: {outfile}")
        return outfile

    # ── Match-over image ──────────────────────────────────────────────────

    def build_match_over_image(self, gs: GameState) -> str:
        """
        Build the match-over screen (games tally as main score, no swap).
        games_won is used as-is — the winning point was just scored.
        """
        gl = gs.games_won["left"]
        gr = gs.games_won["right"]
        outfile = os.path.join(TMP_DIR, f"{SESSION_ID}.matchover.{gl}-{gr}.bmp")
        if not os.path.exists(outfile):
            Compositor.build_match_over(
                best_of     = gs.best_of,
                left_games  = gl,
                right_games = gr,
                outfile     = outfile,
            )
        return outfile

    # ── Pre-generation ────────────────────────────────────────────────────

    def pregenerate(self, gs: GameState):
        """
        Background thread: build the two next score images.

        For each outcome (left scores / right scores):
          1. Clone gs
          2. Call _apply_point() — advances score AND serve
          3. Use resulting serve_num + server + score as the filename key
          4. Build the composite with the clone's server as next-server overlay

        Both images are keyed on POST-advance serve_num because that is
        what will be in gs.serve_num when _handle_score runs next.
        """
        gs_snap = gs.clone()

        def _work():
            with self._pregen_lock:
                for side in ("left", "right"):
                    g = gs_snap.clone()
                    _apply_point(g, side)
                    path = tmp_score_path(
                        g.serve_num, g.server,
                        g.score["left"], g.score["right"],
                    )
                    if not os.path.exists(path):
                        self.build_score_image(
                            base_image  = g.base_image,
                            next_server = g.server,
                            left_score  = g.score["left"],
                            right_score = g.score["right"],
                            left_games  = gs_snap.games_won["left"],
                            right_games = gs_snap.games_won["right"],
                            serve_num   = g.serve_num,
                        )
                        logging.debug(f"[Pregen] {path}")

        threading.Thread(target=_work, daemon=True).start()


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
    serve_num increments on every serve (both 1st and 2nd).
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

    serve_num is NOT reset — it continues incrementing across games.
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


def base_image_name(race_to: int, best_of: int) -> str:
    return f"gl{race_to}bo{best_of}.bmp"


# =============================================================================
#  MATCH ENGINE
# =============================================================================

class MatchEngine:
    """
    Central controller.  Owns GameState, undo stack, display, and logger.

    Critical timing for every scored point:
      1. Simulate _apply_point on a clone to find post-advance server/serve_num
      2. Look up (or build) the pre-generated image using those keys
      3. Show the image immediately
      4. Push undo snapshot
      5. Apply the point for real (mutates gs)
      6. Log; check game/match win; pre-generate next two images
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
            gs.best_of = 3 if colour == "green" else 5
            self.logger.event(f"{colour.capitalize()} pressed – Best of {gs.best_of}")
            gs.state = State.CONFIRM_RULES
            conf = f"gl{gs.race_to}bo{gs.best_of}conf.bmp"
            self.display.show_asset(conf)
            self.logger.event(f"Confirmation screen: {conf}")

        # ── CONFIRM_RULES: one tap from either player confirms ─────────────
        elif state == State.CONFIRM_RULES:
            self._push_undo()
            gs.base_image = base_image_name(gs.race_to, gs.best_of)
            self.logger.event(
                f"{colour.capitalize()} pressed – Rules confirmed: "
                f"race to {gs.race_to}, best of {gs.best_of}. "
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

            # Build the 0-0 start image synchronously (nothing pre-generated yet).
            # gs.server is already correct (just set above).
            self.display.build_score_image(
                base_image  = gs.base_image,
                next_server = gs.server,
                left_score  = 0,
                right_score = 0,
                left_games  = gs.games_won["left"],
                right_games = gs.games_won["right"],
                serve_num   = gs.serve_num,
            )
            self.display.show_score(gs)
            self.display.pregenerate(gs)

        # ── PLAYING ────────────────────────────────────────────────────────
        elif state == State.PLAYING:
            self._handle_score(colour)

        # ── WIN_CONFIRM: only reached for BO3 tied 1-1 extend prompt ───────
        elif state == State.WIN_CONFIRM:
            self._handle_win_confirm(colour)

        # ── MATCH_OVER: long press resets; short press re-shows summary ────
        elif state == State.MATCH_OVER:
            path = self.display.build_match_over_image(self.gs)
            self.display.show_file(path)

    # ── Score a point ─────────────────────────────────────────────────────

    def _handle_score(self, colour: str):
        gs   = self.gs
        side = GameState.colour_to_side(colour)

        # Simulate the advance to find post-advance serve_num and server.
        # This determines both the filename to look up AND the overlay to
        # use if the image needs to be built synchronously.
        gs_tmp = gs.clone()
        _apply_point(gs_tmp, side)
        post_server   = gs_tmp.server
        post_serve_num = gs_tmp.serve_num
        new_left  = gs_tmp.score["left"]
        new_right = gs_tmp.score["right"]

        img_path = tmp_score_path(post_serve_num, post_server, new_left, new_right)
        if not os.path.exists(img_path):
            logging.warning(f"[Engine] Pre-gen missing: {img_path} — building now")
            self.display.build_score_image(
                base_image  = gs.base_image,
                next_server = post_server,
                left_score  = new_left,
                right_score = new_right,
                left_games  = gs.games_won["left"],
                right_games = gs.games_won["right"],
                serve_num   = post_serve_num,
            )
        self.display.show_file(img_path)

        # Save undo snapshot BEFORE mutating state.
        self._push_undo()

        # Apply the point for real.
        changed_server = _apply_point(gs, side)

        score_str = f"{gs.score['left']}-{gs.score['right']}"
        self.logger.event(
            f"{colour.capitalize()} button pressed. "
            f"{colour.capitalize()} scores. {score_str}"
        )

        # Check for game win.
        winning_side = check_game_win(gs)
        if winning_side:
            self._handle_game_win(winning_side, changed_server)
            return

        # Normal point: log serve state and kick off next pre-generation.
        if changed_server:
            self.logger.serve_change()
        else:
            self.logger.blank()
        self.logger.serve_header(gs)
        self.display.pregenerate(gs)

    # ── Game won ──────────────────────────────────────────────────────────

    def _handle_game_win(self, winning_side: str, changed_server: bool):
        """
        Called immediately after check_game_win() returns a winner.

        Behaviour:
          - Always auto-advance to next game (no confirmation required).
          - Exception: BO3 tied 1-1 — pause and ask to extend to BO5.
          - Match winner: show gameover image, enter MATCH_OVER state.

        The start-of-new-game image is built using the serve-bar overlay
        (serve.bmp) plus the correct serve-side arrow.  After start_new_game()
        runs, gs.server is already the winner's new side (they swap ends, so
        the winner's new side is opposite to winning_side).
        """
        gs            = self.gs
        winner_colour = GameState.side_to_colour(winning_side)

        # Record history and increment positional games tally.
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

        # Check for match win BEFORE swapping (games_won reflects current sides).
        m_winner = match_winner(gs)
        if m_winner:
            gs.state = State.MATCH_OVER
            path = self.display.build_match_over_image(gs)
            self.display.show_file(path)
            self._log_match_summary()
            return

        total = gs.games_won["left"] + gs.games_won["right"]
        is_extend_offer = (gs.best_of == 3 and total == 2)

        if is_extend_offer:
            # BO3 tied 1-1: ask to extend.  Show next-game image while waiting.
            gs.state         = State.WIN_CONFIRM
            gs.extend_prompt = True
            # Temporarily preview what the next game would look like.
            # We save this snapshot so undo from here restores correctly.
            # Show the end-of-game state using the post-swap setup so players
            # can see the games tally while deciding.
            gs_preview = gs.clone()
            start_new_game(gs_preview, winning_side)
            path = self.display.build_new_game_image(gs_preview)
            self.display.show_file(path)
            self.logger.event(
                "Games tied 1-1 in best-of-3.  "
                "Green = extend to best of 5.  Blue = end match now."
            )
            return

        # Normal game win: auto-advance immediately.
        start_new_game(gs, winning_side)
        gs.state = State.PLAYING
        self.logger.blank()
        self.logger.serve_header(gs)

        # Build and show the new-game 0-0 start image.
        path = self.display.build_new_game_image(gs)
        self.display.show_file(path)

        # Pre-generate the first two possible outcomes of the new game.
        self.display.pregenerate(gs)

    # ── BO3 tied 1-1 extend prompt ────────────────────────────────────────

    def _handle_win_confirm(self, colour: str):
        """
        Green = extend to best of 5.
        Blue  = end match now with current BO3 result.
        """
        gs = self.gs

        if colour == "blue":
            self.logger.event("Blue pressed – not extending. Match over.")
            gs.state = State.MATCH_OVER
            path = self.display.build_match_over_image(gs)
            self.display.show_file(path)
            self._log_match_summary()
            return

        # Green pressed — extend to best of 5.
        self.logger.event("Green pressed – extending to best of 5!")
        self._push_undo()

        gs.best_of       = 5
        gs.extend_prompt = False
        gs.base_image    = base_image_name(gs.race_to, gs.best_of)
        winning_side     = gs.game_winner   # side BEFORE the swap

        start_new_game(gs, winning_side)
        gs.state = State.PLAYING
        self.logger.blank()
        self.logger.serve_header(gs)

        path = self.display.build_new_game_image(gs)
        self.display.show_file(path)
        self.display.pregenerate(gs)

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
            path = tmp_score_path(
                gs.serve_num, gs.server,
                gs.score["left"], gs.score["right"],
            )
            if not os.path.exists(path):
                self.display.build_score_image(
                    base_image  = gs.base_image,
                    next_server = gs.server,
                    left_score  = gs.score["left"],
                    right_score = gs.score["right"],
                    left_games  = gs.games_won["left"],
                    right_games = gs.games_won["right"],
                    serve_num   = gs.serve_num,
                )
            self.display.show_file(path)
            self.display.pregenerate(gs)
        elif gs.state == State.WIN_CONFIRM:
            # Undo back into the extend-or-end prompt:
            # re-show the new-game preview image.
            gs_preview = gs.clone()
            start_new_game(gs_preview, gs.game_winner)
            path = self.display.build_new_game_image(gs_preview)
            self.display.show_file(path)
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
        elif s == State.CONFIRM_RULES:
            self.display.show_asset(f"gl{gs.race_to}bo{gs.best_of}conf.bmp")
        elif s == State.SERVING_CHOICE:
            self.display.show_asset("serveask.bmp")
        elif s == State.MATCH_OVER:
            path = self.display.build_match_over_image(gs)
            self.display.show_file(path)

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
