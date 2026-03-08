#!/usr/bin/python3
"""
=============================================================================
 Ping-Pong Scoring System — Raspberry Pi Zero W v1
 IT8951 800x600 e-paper  +  2x ESP32-C6 buttons over MQTT
 Version 0.7
=============================================================================

IMAGE PIPELINE OVERVIEW
-----------------------
All artwork is pre-made by Jim and lives in /home/jim/images/.
This script never generates background artwork — it only *composites*
pre-made layers together with ImageMagick.

Setup / rule-selection screens (shown directly via IT8951):
  gamelen.bmp          -> shown as soon as both buttons connect
  gl11.bmp             -> shown after green tap  (race-to-11 chosen)
  gl21.bmp             -> shown after blue tap   (race-to-21 chosen)
  gl11bo3conf.bmp      -> confirmation: race-to-11, best-of-3
  gl11bo5conf.bmp      -> confirmation: race-to-11, best-of-5
  gl21bo3conf.bmp      -> confirmation: race-to-21, best-of-3
  gl21bo5conf.bmp      -> confirmation: race-to-21, best-of-5
  serve.bmp            -> "who serves first?" prompt

In-game base images (background layer for score composites):
  gl11bo3.bmp          -> race-to-11, best-of-3
  gl11bo5.bmp          -> race-to-11, best-of-5
  gl21bo3.bmp          -> race-to-21, best-of-3
  gl21bo5.bmp          -> race-to-21, best-of-5

Serve indicator overlays (composited onto base):
  serveleft.bmp        -> placed at x=0,   y=0
  serveright.bmp       -> placed at x=518, y=0

Score digit images (0.bmp ... 41.bmp, each 33x215 px):
  Left  digit: x=35,  y=218
  Right digit: x=424, y=218

PRE-GENERATION STRATEGY
-----------------------
After every serve starts (including the very first one after the serve
choice), we immediately build the two next possible score BMPs in a
background thread:

  /tmp/<serve_num:02d>.<left+1>-<right>.bmp   (left scores)
  /tmp/<serve_num:02d>.<left>-<right+1>.bmp   (right scores)

serve_num is a global monotonic counter (never reset, even across games).
It uniquely identifies each "serve slot" for the undo system.

When a button is pressed we show the already-built image instantly,
advance the state, then kick off the next pair of pre-generations.

UNDO
----
GameState is deep-copied onto a stack before every mutation.
serve_num is part of GameState, so pop-and-restore gives us back the
exact filename of the image we need to re-display — no extra bookkeeping.
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

EPAPER_CMD  = "/IT8951/IT8951"   # path to the IT8951 display binary

# Jim's pre-made artwork
ASSETS = "/home/jim/images"

# Where composited score images are written
TMP_DIR = "/tmp"

# Score digit image dimensions (Jim's files are 33x215 px)
DIGIT_W = 33
DIGIT_H = 215

# Pixel positions where score digits are composited onto the base image
LEFT_SCORE_X  = 35
LEFT_SCORE_Y  = 218
RIGHT_SCORE_X = 424
RIGHT_SCORE_Y = 218

# Pixel positions where serve overlays are composited onto the base image
SERVE_LEFT_X  = 0
SERVE_LEFT_Y  = 0
SERVE_RIGHT_X = 518
SERVE_RIGHT_Y = 0

# serve.bmp — the "serving" bar shown on all in-game score screens
SERVE_BAR_X   = 283
SERVE_BAR_Y   = 27

# switch.bmp — shown on the end-of-game screen
SWITCH_X      = 256
SWITCH_Y      = 26

# Games-won digit positions (g0.bmp / g1.bmp / g2.bmp)
GAMES_LEFT_X  = 164
GAMES_RIGHT_X = 565
GAMES_Y       = 477

LOG_DIR = "logs"

SIMULATION_MODE = "--sim" in sys.argv

VERSION = "0.7"

# Handle --version / -v immediately, before anything else runs
if "--version" in sys.argv or "-v" in sys.argv:
    print(f"pingpong.py version {VERSION}")
    sys.exit(0)


# =============================================================================
#  ASSET PATH HELPERS
# =============================================================================

def asset(name: str) -> str:
    """Full path to a file in Jim's images directory."""
    return os.path.join(ASSETS, name)


def digit_path(n: int) -> str:
    """Full path to the pre-made digit BMP for number n."""
    return asset(f"{n}.bmp")


def tmp_score_path(serve_num: int, next_server: str, left: int, right: int) -> str:
    """
    Full path for a pre-generated score composite in /tmp.

    Format:  /tmp/<serve_num:02d>.<next_server>.<left>-<right>.bmp
    Example: /tmp/02.left.1-1.bmp

    next_server ("left" or "right") is included in the filename because
    two different scoring paths can arrive at the same score on the same
    serve_num but with different next servers (e.g. score 1-1 on serve 2
    reached via green scoring is different from 1-1 on serve 2 reached
    via blue scoring when one triggers a serve rotation and the other
    doesn't).  Including the next server makes every filename unique.
    """
    return os.path.join(TMP_DIR, f"{serve_num:02d}.{next_server}.{left}-{right}.bmp")


# =============================================================================
#  STATE DEFINITIONS
# =============================================================================

class State(Enum):
    WAITING_BUTTONS = auto()   # waiting for both ESP32s to connect
    RULE_RACE       = auto()   # green=race-to-11, blue=race-to-21
    RULE_BO         = auto()   # green=best-of-3,  blue=best-of-5
    CONFIRM_RULES   = auto()   # either player taps to confirm
    SERVING_CHOICE  = auto()   # first tap picks the server
    PLAYING         = auto()   # live scoring
    WIN_CONFIRM     = auto()   # end-of-game; both tap to continue
    MATCH_OVER      = auto()   # match finished


# =============================================================================
#  GAME STATE
# =============================================================================

class GameState:
    """
    Complete snapshot of the match.  Deep-copied before every mutation so
    double-press can restore any prior state instantly.

    POSITIONAL SCORE MODEL
    ----------------------
    score["left"] and score["right"] track points for whoever is physically
    on that side RIGHT NOW.  games_won is also positional.

    When players swap ends between games, games_won is flipped so the
    left/right columns stay accurate.  This is what makes:
      - Andrew wins game 1 on the left  -> games_won = {left:1, right:0}
      - After swapping                  -> games_won = {left:0, right:1}
    work correctly without tracking player identities.

    Green button = always LEFT side.
    Blue  button = always RIGHT side.

    server = "left" | "right" (never a colour string).

    serve_num is a monotonically-increasing integer across the entire match.
    It forms the first component of every pre-generated BMP filename, making
    undo trivially simple: just restore the snapshot and the filename is known.
    """

    def __init__(self):
        self.race_to      = 11
        self.best_of      = 3

        self.games_won    = {"left": 0, "right": 0}
        self.current_game = 1

        self.score        = {"left": 0, "right": 0}

        self.server       = "left"   # "left" or "right"
        self.serve_count  = 1        # 1 or 2 within current server's turn
        self.serve_num    = 0        # global monotonic serve counter

        # Set to "gl{race_to}bo{best_of}.bmp" after rules are confirmed
        self.base_image   = None

        self.state        = State.WAITING_BUTTONS

        self.extend_prompt = False
        self.game_winner   = None    # "left" or "right"

        # list of {left, right, winner_side, winner_colour}
        self.game_history  = []

    @staticmethod
    def colour_to_side(colour: str) -> str:
        """Green is always left, blue is always right."""
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
#  COMPOSITOR  (ImageMagick wrapper)
# =============================================================================

class Compositor:
    """
    Composites pre-made BMP layers using ImageMagick.

    IN-GAME SCORE IMAGE layers (build_score):
      1. Base image          (gl11bo3.bmp etc.)
      2. serve.bmp           at (283, 27)   — always present during play
      3. Serve-side overlay  (serveleft.bmp or serveright.bmp)
      4. Left point digit    (N.bmp)        at (LEFT_SCORE_X,  LEFT_SCORE_Y)
      5. Right point digit   (N.bmp)        at (RIGHT_SCORE_X, RIGHT_SCORE_Y)
      6. Left games-won      (gN.bmp)       at (GAMES_LEFT_X,  GAMES_Y)
      7. Right games-won     (gN.bmp)       at (GAMES_RIGHT_X, GAMES_Y)

    END-OF-GAME image layers (build_end_of_game) — shown when a game is won:
      1. Base image
      2. switch.bmp          at (256, 26)
      3. serveleft.bmp       at (0, 0)
      4. serveright.bmp      at (518, 0)
      5. Left point digit    0 at (LEFT_SCORE_X,  LEFT_SCORE_Y)
      6. Right point digit   0 at (RIGHT_SCORE_X, RIGHT_SCORE_Y)
      7. Left games-won      (gN.bmp) at (GAMES_LEFT_X,  GAMES_Y)
      8. Right games-won     (gN.bmp) at (GAMES_RIGHT_X, GAMES_Y)

    MATCH-OVER image layers (build_match_over):
      1. gameover.bmp  (BO5) or gameover3.bmp (BO3)
      2. Left games-won digit  (N.bmp) at (LEFT_SCORE_X,  LEFT_SCORE_Y)
      3. Right games-won digit (N.bmp) at (RIGHT_SCORE_X, RIGHT_SCORE_Y)
      (no serve overlay, no small gN images — the point-score IS the game score)
    """

    @staticmethod
    def run(args: list, outfile: str) -> bool:
        """Run: convert <args...> <outfile>"""
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
        """
        Composite a normal in-game score image.

        Layers (in order):
          base -> serve.bmp@(283,27) -> serve-side overlay -> left digit
               -> right digit -> left gN -> right gN
        """
        left_digit  = digit_path(left_score)
        right_digit = digit_path(right_score)
        serve_bar   = asset("serve.bmp")
        left_gdig   = asset(f"g{left_games}.bmp")
        right_gdig  = asset(f"g{right_games}.bmp")

        for p in (base_img, serve_bar, serve_overlay, left_digit, right_digit,
                  left_gdig, right_gdig):
            if not os.path.exists(p):
                logging.error(f"[Compositor] Missing asset: {p}")
                return False

        args = [
            base_img,
            serve_bar,
            "-geometry", f"+{SERVE_BAR_X}+{SERVE_BAR_Y}", "-composite",
            serve_overlay,
            "-geometry", f"+{serve_x}+{serve_y}", "-composite",
            left_digit,
            "-geometry", f"+{LEFT_SCORE_X}+{LEFT_SCORE_Y}", "-composite",
            right_digit,
            "-geometry", f"+{RIGHT_SCORE_X}+{RIGHT_SCORE_Y}", "-composite",
            left_gdig,
            "-geometry", f"+{GAMES_LEFT_X}+{GAMES_Y}", "-composite",
            right_gdig,
            "-geometry", f"+{GAMES_RIGHT_X}+{GAMES_Y}", "-composite",
        ]
        return Compositor.run(args, outfile)

    @staticmethod
    def build_end_of_game(
        base_img: str,
        left_games: int, right_games: int,
        outfile: str,
    ) -> bool:
        """
        Composite the between-games screen shown when a game is won.

        Shows: base + switch.bmp + both serve overlays + 0-0 point score
               + updated games-won digits.
        The winner has already been incremented in games_won before this
        is called, so left_games/right_games reflect the new tally.
        """
        zero        = digit_path(0)
        switch_img  = asset("switch.bmp")
        sl          = asset("serveleft.bmp")
        sr          = asset("serveright.bmp")
        left_gdig   = asset(f"g{left_games}.bmp")
        right_gdig  = asset(f"g{right_games}.bmp")

        for p in (base_img, switch_img, sl, sr, zero, left_gdig, right_gdig):
            if not os.path.exists(p):
                logging.error(f"[Compositor] Missing asset: {p}")
                return False

        args = [
            base_img,
            switch_img,
            "-geometry", f"+{SWITCH_X}+{SWITCH_Y}", "-composite",
            sl,
            "-geometry", f"+{SERVE_LEFT_X}+{SERVE_LEFT_Y}", "-composite",
            sr,
            "-geometry", f"+{SERVE_RIGHT_X}+{SERVE_RIGHT_Y}", "-composite",
            zero,
            "-geometry", f"+{LEFT_SCORE_X}+{LEFT_SCORE_Y}", "-composite",
            zero,
            "-geometry", f"+{RIGHT_SCORE_X}+{RIGHT_SCORE_Y}", "-composite",
            left_gdig,
            "-geometry", f"+{GAMES_LEFT_X}+{GAMES_Y}", "-composite",
            right_gdig,
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
        Composite the match-over screen.

        Uses gameover.bmp (BO5) or gameover3.bmp (BO3) as the base.
        The large point-digit positions show games won — no small gN images.
        No sides are swapped here; left_games/right_games are as-is at the
        moment the winning point was scored.
        """
        base_img    = asset("gameover.bmp" if best_of == 5 else "gameover3.bmp")
        left_digit  = digit_path(left_games)
        right_digit = digit_path(right_games)

        for p in (base_img, left_digit, right_digit):
            if not os.path.exists(p):
                logging.error(f"[Compositor] Missing asset: {p}")
                return False

        args = [
            base_img,
            left_digit,
            "-geometry", f"+{LEFT_SCORE_X}+{LEFT_SCORE_Y}", "-composite",
            right_digit,
            "-geometry", f"+{RIGHT_SCORE_X}+{RIGHT_SCORE_Y}", "-composite",
        ]
        return Compositor.run(args, outfile)


# =============================================================================
#  DISPLAY MANAGER
# =============================================================================

class DisplayManager:
    """
    Handles:
      - Sending any BMP to the e-paper
      - Building composite score images on demand
      - Pre-generating the next two score images in a background thread

    SERVE OVERLAY RULE — "show who serves NEXT"
    --------------------------------------------
    The overlay on every score image must show who will serve on the
    NEXT point, not who served on the point that just finished.

    Concretely:
      - After the serve choice (0-0, serve 1): show serveleft/right
        for whoever was chosen — they ARE about to serve, so this is
        both "current server" and "next server" simultaneously.
      - After serve 1 of a pair: the same player serves again (serve 2),
        so the overlay stays the same.
      - After serve 2 of a pair: the server rotates, so the overlay
        changes to the OTHER side.

    Implementation: every image is built using the server state that
    exists AFTER _advance_serve() has been called for that point.
    We simulate this in pregenerate() by cloning gs and running
    _advance_serve() on each clone before reading gs.server.

    PRE-GENERATION FILENAMES
    ------------------------
    /tmp/<serve_num:02d>.<left>-<right>.bmp

    serve_num is the value AFTER _advance_serve() runs for that point.
    This matches what will be in gs.serve_num when the engine later
    calls show_file() to display that image.

    So when pre-generating "left scores next":
      clone gs → call _advance_serve() → use resulting serve_num,
      server, serve_count to build the image.
    """

    def __init__(self):
        self._pregen_lock = threading.Lock()

    # ── E-paper output ────────────────────────────────────────────────────

    def show_file(self, path: str):
        """Send a BMP file to the IT8951 e-paper display."""
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
        """Send a file from ASSETS to the display."""
        self.show_file(asset(filename))

    # ── Score image building ──────────────────────────────────────────────

    def _overlay_for(self, server: str) -> tuple:
        """Return (overlay_path, x, y) for the given server side string."""
        if server == "left":
            return asset("serveleft.bmp"), SERVE_LEFT_X, SERVE_LEFT_Y
        else:
            return asset("serveright.bmp"), SERVE_RIGHT_X, SERVE_RIGHT_Y

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
        Composite and write one in-game score BMP.

        next_server:  "left" or "right" — server for the NEXT point.
        left_games:   current games won by left side (for gN overlay).
        right_games:  current games won by right side.
        serve_num:    global serve counter (post-advance); first part of filename.

        Returns the output path (builds if not already on disk).
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

    def build_end_of_game_image(self, gs: GameState) -> str:
        """
        Build and return the between-games screen path.
        games_won already has the winner's tally incremented.
        Uses a stable filename (not serve-keyed) since it's shown once.
        """
        gl = gs.games_won["left"]
        gr = gs.games_won["right"]
        outfile = os.path.join(TMP_DIR, f"endgame.{gs.current_game}.{gl}-{gr}.bmp")
        if not os.path.exists(outfile):
            Compositor.build_end_of_game(
                base_img    = asset(gs.base_image),
                left_games  = gl,
                right_games = gr,
                outfile     = outfile,
            )
        return outfile

    def build_match_over_image(self, gs: GameState) -> str:
        """
        Build and return the match-over screen path.
        games_won is NOT swapped — we display it as-is from the winning moment.
        """
        gl = gs.games_won["left"]
        gr = gs.games_won["right"]
        outfile = os.path.join(TMP_DIR, f"matchover.{gl}-{gr}.bmp")
        if not os.path.exists(outfile):
            Compositor.build_match_over(
                best_of     = gs.best_of,
                left_games  = gl,
                right_games = gr,
                outfile     = outfile,
            )
        return outfile

    def show_score(self, gs: GameState):
        """
        Show the score image for the current GameState.
        gs.serve_num and gs.server are post-advance; they represent the NEXT serve.
        Builds synchronously if not already on disk.
        """
        path = tmp_score_path(gs.serve_num, gs.server, gs.score["left"], gs.score["right"])
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

    # ── Pre-generation ────────────────────────────────────────────────────

    def pregenerate(self, gs: GameState):
        """
        Spawn a background thread to pre-build the two next score images.

        For each possible outcome (left scores / right scores) we:
          1. Clone gs.
          2. Call _apply_point() on the clone — this increments the score
             AND calls _advance_serve(), which increments serve_num and
             possibly rotates gs.server.
          3. Read the resulting serve_num, server, and score from the clone.
          4. Build /tmp/<serve_num>.<left>-<right>.bmp using the clone's
             server as the next-server overlay.

        This means each pre-generated image already has the correct serve
        indicator for whoever will serve AFTER that point is scored.
        """
        gs_snap = gs.clone()   # snapshot is immune to main-thread mutations

        def _work():
            with self._pregen_lock:
                base = gs_snap.base_image

                # ── Pre-generate: left scores next ─────────────────────────
                gs_l = gs_snap.clone()
                _apply_point(gs_l, "left")        # advances serve_num + server
                p_l = tmp_score_path(
                    gs_l.serve_num,
                    gs_l.server,            # next server AFTER this point
                    gs_l.score["left"],
                    gs_l.score["right"],
                )
                if not os.path.exists(p_l):
                    self.build_score_image(
                        base_image  = base,
                        next_server = gs_l.server,
                        left_score  = gs_l.score["left"],
                        right_score = gs_l.score["right"],
                        left_games  = gs_snap.games_won["left"],
                        right_games = gs_snap.games_won["right"],
                        serve_num   = gs_l.serve_num,
                    )
                    logging.debug(f"[Pregen] {p_l}")

                # ── Pre-generate: right scores next ────────────────────────
                gs_r = gs_snap.clone()
                _apply_point(gs_r, "right")
                p_r = tmp_score_path(
                    gs_r.serve_num,
                    gs_r.server,            # next server AFTER this point
                    gs_r.score["left"],
                    gs_r.score["right"],
                )
                if not os.path.exists(p_r):
                    self.build_score_image(
                        base_image  = base,
                        next_server = gs_r.server,
                        left_score  = gs_r.score["left"],
                        right_score = gs_r.score["right"],
                        left_games  = gs_snap.games_won["left"],
                        right_games = gs_snap.games_won["right"],
                        serve_num   = gs_r.serve_num,
                    )
                    logging.debug(f"[Pregen] {p_r}")

        threading.Thread(target=_work, daemon=True).start()


# =============================================================================
#  LOGGER
# =============================================================================

class MatchLogger:
    """
    Writes logs/<epoch>.txt in the exact format specified.

    Serve header format:  Green/Left serving (1)
    Point log format:     Mon  2 Mar 21:40:10 PST 2026 - Green button pressed. Green scores. 1-0
    Change of serve:      Change of serve   (then blank line)
    Undo format:          Mon  2 Mar 21:41:00 PST 2026 - Blue double pressed. Score reverted. 3-2
    """

    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        epoch    = int(time.time())
        path     = os.path.join(LOG_DIR, f"{epoch}.txt")
        self._fh = open(path, "w", buffering=1)   # line-buffered for safety
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )
        self._log = logging.getLogger("pingpong")
        self._log.info(f"Log file: {path}")

    def _ts(self) -> str:
        now = datetime.now()
        day = now.strftime("%-d").rjust(2)   # " 2" not "02"
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
        colour = gs.server_colour().capitalize()   # "Green" or "Blue"
        side   = gs.server_side_label()            # "Left"  or "Right"
        self.write(f"{colour}/{side} serving ({gs.serve_count})")

    def serve_change(self):
        """Emit 'Change of serve' followed by a blank line."""
        self.write("Change of serve")
        self.blank()

    def close(self):
        self._fh.close()


# =============================================================================
#  PURE GAME LOGIC
# =============================================================================

def _advance_serve(gs: GameState) -> bool:
    """
    Move to the next serve within the rotation.

    serve_num increments on every serve (1st and 2nd).
    serve_count cycles 1 -> 2 -> (rotate server) -> 1.
    Returns True if the server changed.
    """
    gs.serve_num += 1
    if gs.serve_count == 1:
        gs.serve_count = 2
        return False          # same server, second serve
    else:
        gs.serve_count = 1
        gs.server = "right" if gs.server == "left" else "left"
        return True           # server rotated


def _apply_point(gs: GameState, side: str) -> bool:
    """
    Award a point to side ("left" or "right") and advance the serve.
    Returns True if the server changed.
    """
    gs.score[side] += 1
    return _advance_serve(gs)


def check_game_win(gs: GameState):
    """
    Return "left", "right", or None.
    Win condition: score >= race_to AND lead >= 2 (win-by-two).
    """
    l, r = gs.score["left"], gs.score["right"]
    if (l >= gs.race_to or r >= gs.race_to) and abs(l - r) >= 2:
        return "left" if l > r else "right"
    return None


def swap_games_won(gs: GameState):
    """
    Flip the positional games_won dict to match the new physical positions
    after players swap ends.
    """
    gs.games_won["left"], gs.games_won["right"] = (
        gs.games_won["right"],
        gs.games_won["left"],
    )


def start_new_game(gs: GameState, winning_side: str):
    """
    Prepare the next game.

    winning_side is the side BEFORE the end-of-game side-swap.
    After the swap the winner is on the opposite side, so we set server
    to that opposite side (winner serves first in the new game).

    serve_num is NOT reset — it continues incrementing across games.
    """
    new_server = "right" if winning_side == "left" else "left"
    swap_games_won(gs)
    gs.score        = {"left": 0, "right": 0}
    gs.current_game += 1
    gs.server       = new_server
    gs.serve_count  = 1
    # serve_num keeps going; _advance_serve will increment it when the
    # first serve of the new game is recognised.


def match_winner(gs: GameState):
    """Return "left", "right", or None."""
    needed = (gs.best_of // 2) + 1   # 2 for BO3, 3 for BO5
    for side in ("left", "right"):
        if gs.games_won[side] >= needed:
            return side
    return None


def base_image_name(race_to: int, best_of: int) -> str:
    """e.g. race_to=11, best_of=3  ->  "gl11bo3.bmp" """
    return f"gl{race_to}bo{best_of}.bmp"


# =============================================================================
#  MATCH ENGINE
# =============================================================================

class MatchEngine:
    """
    Central controller:
      - Owns GameState and the undo stack
      - Dispatches button events to the correct handler
      - Calls DisplayManager and MatchLogger for all side-effects

    The critical timing flow for every scored point:
      1. Look up the pre-generated image file (serve_num BEFORE advance)
      2. Show it to the display immediately (near-instant)
      3. Apply the point to GameState (score++ and serve advance)
      4. Kick off pre-generation of the next two images (background thread)
    """

    def __init__(self, display: DisplayManager, logger: MatchLogger):
        self.display = display
        self.logger  = logger
        self.gs      = GameState()

        self._undo_stack: list[GameState] = []
        self._connected       = {"green": False, "blue": False}
        self.event_queue      = queue.Queue()
        self._win_confirmed   = {"green": False, "blue": False}

    # ── Undo stack ────────────────────────────────────────────────────────

    def _push_undo(self):
        self._undo_stack.append(self.gs.clone())

    def _pop_undo(self) -> bool:
        if self._undo_stack:
            self.gs = self._undo_stack.pop()
            return True
        return False

    # ── Top-level button dispatcher ───────────────────────────────────────

    def handle_button(self, colour: str, press_type: str):
        if press_type == "long":
            self.logger.event(f"{colour.capitalize()} long press — full reset.")
            self._full_reset()
        elif press_type == "double":
            self._handle_undo(colour)
        elif press_type == "short":
            self._handle_short(colour)

    # ── Short press dispatcher ────────────────────────────────────────────

    def _handle_short(self, colour: str):
        gs    = self.gs
        state = gs.state

        if state == State.WAITING_BUTTONS:
            pass   # only connection events advance this state

        # ── RULE_RACE ──────────────────────────────────────────────────────
        # gamelen.bmp is on screen. Green = 11, Blue = 21.
        elif state == State.RULE_RACE:
            self._push_undo()
            gs.race_to = 11 if colour == "green" else 21
            self.logger.event(
                f"{colour.capitalize()} pressed – Race to {gs.race_to}"
            )
            gs.state = State.RULE_BO
            # Show intermediate screen that asks best-of 3 or 5
            self.display.show_asset(f"gl{gs.race_to}.bmp")

        # ── RULE_BO ────────────────────────────────────────────────────────
        # gl11.bmp or gl21.bmp is on screen. Green = 3, Blue = 5.
        elif state == State.RULE_BO:
            self._push_undo()
            gs.best_of = 3 if colour == "green" else 5
            self.logger.event(
                f"{colour.capitalize()} pressed – Best of {gs.best_of}"
            )
            gs.state = State.CONFIRM_RULES
            conf = f"gl{gs.race_to}bo{gs.best_of}conf.bmp"
            self.display.show_asset(conf)
            self.logger.event(f"Confirmation screen: {conf}")

        # ── CONFIRM_RULES ──────────────────────────────────────────────────
        # Confirmation screen on display. ONE tap from either player confirms.
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
            self.logger.write(
                "Waiting for next button press to determine who serves first"
            )

        # ── SERVING_CHOICE ─────────────────────────────────────────────────
        # serve.bmp on screen. First tap = first server.
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

            # Build and show the initial 0-0 image synchronously.
            # gs.server is already the correct next-server (the chosen side).
            self.display.build_score_image(
                base_image  = gs.base_image,
                next_server = gs.server,
                left_score  = 0,
                right_score = 0,
                left_games  = 0,
                right_games = 0,
                serve_num   = gs.serve_num,
            )
            self.display.show_score(gs)

            # Pre-generate both first-point outcomes in background.
            self.display.pregenerate(gs)

        # ── PLAYING ────────────────────────────────────────────────────────
        elif state == State.PLAYING:
            self._handle_score(colour)

        # ── WIN_CONFIRM ────────────────────────────────────────────────────
        elif state == State.WIN_CONFIRM:
            self._handle_win_confirm(colour)

        # ── MATCH_OVER ─────────────────────────────────────────────────────
        elif state == State.MATCH_OVER:
            # Re-show the match-over image (already built)
            path = self.display.build_match_over_image(self.gs)
            self.display.show_file(path)

    # ── Score a point ─────────────────────────────────────────────────────

    def _handle_score(self, colour: str):
        """
        Flow (in order):
          1. Determine the side (left/right) from the button colour.
          2. Compute what the score WILL BE after this point.
          3. Look up / show that pre-generated image immediately.
          4. Push undo snapshot.
          5. Apply the point (mutates GameState: score++, serve advances).
          6. Log the point and serve rotation.
          7. Check for game/match win.
          8. Pre-generate next two images in background.
        """
        gs   = self.gs
        side = GameState.colour_to_side(colour)

        # ── Step 2: compute future score ───────────────────────────────────
        new_left  = gs.score["left"]  + (1 if side == "left"  else 0)
        new_right = gs.score["right"] + (1 if side == "right" else 0)

        # ── Step 3: show image immediately ─────────────────────────────────
        # The serve_num used as the key is the CURRENT one (before _advance_serve
        # increments it).  That is the same number we used when pre-generating
        # this image after the PREVIOUS point.
        # Simulate the advance now so we know the next server —
        # needed for both the filename lookup AND the fallback build.
        gs_tmp = gs.clone()
        _apply_point(gs_tmp, side)
        next_server_after = gs_tmp.server   # server AFTER this point is awarded

        img_path = tmp_score_path(gs_tmp.serve_num, next_server_after, new_left, new_right)
        if not os.path.exists(img_path):
            logging.warning(f"[Engine] Pre-generated image missing: {img_path} – building now")
            self.display.build_score_image(
                base_image  = gs_tmp.base_image,
                next_server = next_server_after,
                left_score  = new_left,
                right_score = new_right,
                left_games  = gs.games_won["left"],
                right_games = gs.games_won["right"],
                serve_num   = gs_tmp.serve_num,
            )
        self.display.show_file(img_path)

        # ── Step 4: save undo snapshot ─────────────────────────────────────
        self._push_undo()

        # ── Step 5: mutate state ───────────────────────────────────────────
        changed_server = _apply_point(gs, side)

        # ── Step 6: log ────────────────────────────────────────────────────
        score_str = f"{gs.score['left']}-{gs.score['right']}"
        self.logger.event(
            f"{colour.capitalize()} button pressed. "
            f"{colour.capitalize()} scores. {score_str}"
        )

        # ── Step 7: check for game / match win ─────────────────────────────
        winning_side = check_game_win(gs)
        if winning_side:
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
            if m_winner:
                # ── MATCH OVER: show gameover image with games-won as score ──
                gs.state = State.MATCH_OVER
                path = self.display.build_match_over_image(gs)
                self.display.show_file(path)
                self._log_match_summary()
                return

            # ── GAME OVER (not match): show between-games screen ──────────
            total = gs.games_won["left"] + gs.games_won["right"]
            is_extend_offer = (gs.best_of == 3 and total == 2)

            gs.state = State.WIN_CONFIRM
            self._win_confirmed = {"green": False, "blue": False}

            if is_extend_offer:
                gs.extend_prompt = True

            # Build and show the end-of-game image (switch + both serve
            # overlays + 0-0 + updated game scores).
            path = self.display.build_end_of_game_image(gs)
            self.display.show_file(path)

            if is_extend_offer:
                self.logger.event(
                    "Games tied 1-1.  Green = extend to best of 5.  "
                    "Blue = end match now."
                )
            else:
                self._log_win_confirm(winning_side)
            return

        # ── Step 8: normal point — log serve and pre-generate ──────────────
        if changed_server:
            self.logger.serve_change()
        else:
            self.logger.blank()
        self.logger.serve_header(gs)

        self.display.pregenerate(gs)

    # ── Win confirmation / game transition ────────────────────────────────

    def _handle_win_confirm(self, colour: str):
        gs = self.gs

        if gs.extend_prompt:
            # Green = YES extend to best of 5 / Blue = NO, end match now
            if colour == "blue":
                # End the match now.  Build match-over image (BO3 base).
                self.logger.event("Blue pressed – not extending. Match over.")
                gs.state = State.MATCH_OVER
                path = self.display.build_match_over_image(gs)
                self.display.show_file(path)
                self._log_match_summary()
            else:
                # Extend to best of 5.
                # Rule: treat the match as if it was always BO5.
                # The winner of game 2 (gs.game_winner, pre-swap side) has
                # just won that game, so after start_new_game() they will be
                # on the OPPOSITE side (sides swap each game).
                self.logger.event("Green pressed – extending to best of 5!")
                self._push_undo()
                gs.best_of       = 5
                gs.extend_prompt = False
                gs.base_image    = base_image_name(gs.race_to, gs.best_of)
                winning_side     = gs.game_winner   # side BEFORE swap
                # start_new_game swaps games_won AND sets server to the
                # winner's new side (opposite of winning_side).
                start_new_game(gs, winning_side)
                gs.state = State.PLAYING
                self.logger.blank()
                self.logger.serve_header(gs)
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
            return

        # Both players tap to confirm and start next game
        self._win_confirmed[colour] = True
        both = all(self._win_confirmed.values())
        self.logger.event(
            f"{colour.capitalize()} confirmed. "
            f"{'Both confirmed – next game.' if both else 'Waiting for other player.'}"
        )
        if both:
            self._push_undo()
            winning_side = gs.game_winner
            start_new_game(gs, winning_side)
            gs.state = State.PLAYING
            self.logger.blank()
            self.logger.serve_header(gs)
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
            # The image for the restored state was pre-generated (or built
            # synchronously) when we were in that state previously.
            path = tmp_score_path(gs.serve_num, gs.server, gs.score["left"], gs.score["right"])
            if not os.path.exists(path):
                # gs.server is already restored to the correct next-server.
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
        else:
            self._redraw_menu_state()

    # ── Full reset ────────────────────────────────────────────────────────

    def _full_reset(self):
        self.gs           = GameState()
        self._undo_stack  = []
        self._connected   = {"green": False, "blue": False}
        self.gs.state     = State.WAITING_BUTTONS
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

    # ── Menu display helpers ───────────────────────────────────────────────

    def _log_win_confirm(self, winning_side: str):
        """Log the between-games state (display is handled by build_end_of_game_image)."""
        wc = GameState.side_to_colour(winning_side)
        self.logger.event(
            f"Game {self.gs.current_game} over – {wc.capitalize()} wins.  "
            f"Games: left {self.gs.games_won['left']} – "
            f"{self.gs.games_won['right']} right.  "
            "Both tap to continue."
        )

    def _log_match_summary(self):
        """Log the match summary (display is handled by build_match_over_image)."""
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
            f"  Final games tally: "
            f"left {gs.games_won['left']} – {gs.games_won['right']} right"
        )
        self.logger.event("Long press either button to start a new match.")

    def _redraw_menu_state(self):
        """Re-show the correct asset after a menu-level undo."""
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
        elif s == State.WIN_CONFIRM:
            # Re-show the end-of-game image (already built for this game/score combo)
            path = self.display.build_end_of_game_image(gs)
            self.display.show_file(path)

    # ── Main event loop ───────────────────────────────────────────────────

    def run(self):
        self.logger.event("Ping-pong scorer started. Waiting for buttons.")
        while True:
            try:
                colour, press_type = self.event_queue.get(timeout=1)
                self.handle_button(colour, press_type)
            except queue.Empty:
                pass   # idle tick — keeps the thread alive
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
        """Reconnect loop — never gives up."""
        while True:
            try:
                self._client.connect(
                    MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_KEEPALIVE
                )
                self._client.loop_forever()
            except Exception as e:
                logging.error(
                    f"[MQTT] Failed: {e}. Retry in {MQTT_RECONNECT_DELAY}s"
                )
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
    print("\n=== SIMULATION MODE ===")
    print("  connect    – simulate both buttons connecting")
    print("  g / b      – green / blue short press")
    print("  gg / bb    – green / blue double press (undo)")
    print("  GL / BL    – green / blue long press  (full reset)")
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
