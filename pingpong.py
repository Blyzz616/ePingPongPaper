#!/usr/bin/env python3
"""
=============================================================================
 Ping-Pong Scoring System for Raspberry Pi Zero W v1
 IT8951 800x600 monochrome e-paper display + 2x ESP32-C6 buttons over MQTT
=============================================================================

ARCHITECTURE OVERVIEW
---------------------
 - The Pi acts as a Wi-Fi Access Point (hostapd + dnsmasq).
 - A local Mosquitto MQTT broker runs on the Pi.
 - Each ESP32-C6 connects to the AP, then publishes to:
     button/green  (payloads: short | double | long)
     button/blue   (payloads: short | double | long)
   and subscribes to:
     status/green  /  status/blue  (for connection acknowledgement)
 - The Pi subscribes to both button topics.

STATE MACHINE
-------------
  WAITING_BUTTONS  → both ESP32s must connect before anything else
  RULE_RACE        → green=Race11, blue=Race21
  RULE_BO          → green=BestOf3, blue=BestOf5
  CONFIRM_RULES    → both players short-press to confirm
  SERVING_CHOICE   → first press picks server (green=green serves, blue=blue serves)
  PLAYING          → live scoring
  WIN_CONFIRM      → end-of-game confirmation / extend-to-5 offer
  MATCH_OVER       → display summary, long-press resets

DISPLAY PIPELINE
----------------
  ImageMagick  →  BMP file  →  /IT8951/IT8951 0 0 <file>

  All "next possible" screens are pre-generated immediately after any state
  change so the display update is instant when the next button is pressed.

UNDO
----
  A full copy of the game state is pushed onto a stack before every mutation.
  Double-press pops the stack and restores the previous state, then
  re-displays and re-pre-generates from that restored state.

LOGGING
-------
  File: logs/<epoch>.txt
  Format follows the specification exactly, including serve headers,
  blank lines, and "Change of serve" markers.
"""

# ── Standard library ──────────────────────────────────────────────────────────
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

# ── Third-party ───────────────────────────────────────────────────────────────
try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    print("[WARN] paho-mqtt not installed – MQTT disabled, simulation mode only.")

# =============================================================================
#  CONFIGURATION  (edit these to match your hardware / network)
# =============================================================================

MQTT_BROKER_HOST   = "localhost"   # Pi is the broker
MQTT_BROKER_PORT   = 1883
MQTT_KEEPALIVE     = 60
MQTT_TOPIC_GREEN   = "button/green"
MQTT_TOPIC_BLUE    = "button/blue"
MQTT_STATUS_GREEN  = "status/green"
MQTT_STATUS_BLUE   = "status/blue"
MQTT_RECONNECT_DELAY = 5           # seconds between reconnect attempts

EPAPER_CMD         = "/IT8951/IT8951"   # path to the IT8951 display binary
DISPLAY_WIDTH      = 800
DISPLAY_HEIGHT     = 600
IMAGE_DIR          = "/tmp/pingpong_imgs"   # working directory for BMP files
LOG_DIR            = "logs"

# ImageMagick font / style (tweak to taste)
IM_FONT            = "DejaVu-Sans-Bold"
IM_BG              = "white"
IM_FG              = "black"
SCORE_FONT_SIZE    = 220   # huge score digits
LABEL_FONT_SIZE    = 48
STATUS_FONT_SIZE   = 36

# Button timing (used in simulation mode; ESP32 handles real debounce)
SIMULATION_MODE    = "--sim" in sys.argv

# =============================================================================
#  STATE DEFINITIONS
# =============================================================================

class State(Enum):
    WAITING_BUTTONS = auto()   # waiting for both ESP32s to connect
    RULE_RACE       = auto()   # choose race-to (11 or 21)
    RULE_BO         = auto()   # choose best-of (3 or 5)
    CONFIRM_RULES   = auto()   # both players confirm rules
    SERVING_CHOICE  = auto()   # first press picks who serves
    PLAYING         = auto()   # active scoring
    WIN_CONFIRM     = auto()   # game over – next game or extend-to-5 prompt
    MATCH_OVER      = auto()   # match finished

# =============================================================================
#  GAME STATE  (everything we need to snapshot for undo)
# =============================================================================

class GameState:
    """
    Immutable-ish snapshot of the entire match.
    We deep-copy this onto the undo stack before every mutation.

    SCORE MODEL — POSITIONAL, NOT BY COLOUR
    ----------------------------------------
    Scores and game-win counts are stored by *side* (left / right), not by
    button colour (green / blue).  This is the key design decision that makes
    the side-swap logic correct:

        Green is ALWAYS the left button.
        Blue  is ALWAYS the right button.

    When players physically swap ends between games, the person who was on
    the left moves to the right and vice versa.  From the scoreboard's point
    of view the LEFT column still belongs to whoever is standing on the left —
    which is now the person who was previously on the right.

    Concretely: if Andrew starts on the left (green) and wins game 1, the
    scoreboard shows  Games 1–0.  After swapping, Andrew is on the right
    (blue), so game 2 starts as  Games 0–1  — the LEFT column has reset to 0
    because the left side is now Bill, who has 0 game wins.

    Implementation:
        self.score      = {"left": 0, "right": 0}   # points in current game
        self.games_won  = {"left": 0, "right": 0}   # games won in match

    Button colour → side mapping (fixed for the whole match):
        green button  → always LEFT
        blue button   → always RIGHT

    "server" is stored as a side ("left" | "right") so it survives swaps
    naturally — the server label stays with the physical side, not the player.
    """

    def __init__(self):
        # ── Rules ──────────────────────────────────────────────────────────
        self.race_to      = 11        # 11 or 21
        self.best_of      = 3         # 3 or 5

        # ── Match progress ─────────────────────────────────────────────────
        # Stored by side (left/right).  Sides swap each game, so the numbers
        # flip automatically — exactly what the spec requires.
        self.games_won    = {"left": 0, "right": 0}
        self.current_game = 1         # 1-based game counter

        # ── Current game score (positional) ────────────────────────────────
        self.score        = {"left": 0, "right": 0}

        # ── Serve ──────────────────────────────────────────────────────────
        # server is "left" or "right" (the side, not the colour).
        self.server       = "left"    # side of current server
        self.serve_count  = 1         # 1 or 2 within this server's turn

        # ── State machine ──────────────────────────────────────────────────
        self.state        = State.WAITING_BUTTONS

        # ── Confirmation tracking (CONFIRM_RULES) ──────────────────────────
        self.confirmed    = {"green": False, "blue": False}

        # ── WIN_CONFIRM prompt ─────────────────────────────────────────────
        # After best-of-3 we ask "extend to 5?". True = yes-question active.
        self.extend_prompt  = False
        self.game_winner    = None    # side ("left"|"right") of game winner

        # ── History of game scores (for match summary) ──────────────────────
        # list of {"left": l_score, "right": r_score, "winner": side}
        self.game_history = []

    # ── Colour → side helpers (green is always left, blue always right) ────

    @staticmethod
    def colour_to_side(colour: str) -> str:
        """Map button colour to physical side. Always green=left, blue=right."""
        return "left" if colour == "green" else "right"

    @staticmethod
    def side_to_colour(side: str) -> str:
        """Map physical side to button colour."""
        return "green" if side == "left" else "blue"

    def server_colour(self) -> str:
        """Return the colour of the current server."""
        return self.side_to_colour(self.server)

    def server_side_label(self) -> str:
        """Return 'Left' or 'Right' (capitalised) for the current server."""
        return self.server.capitalize()

    def clone(self):
        return copy.deepcopy(self)


# =============================================================================
#  LOGGER  (human-readable, spec-compliant format)
# =============================================================================

class MatchLogger:
    """
    Writes to logs/<epoch>.txt using the exact format specified:

        <blank line>
        Green/Left serving (1)
        <timestamp> - <colour> button pressed. <description>. <score>
        <blank line>   OR   Change of serve\n<blank line>
        Green/Left serving (2)
        ...
    """

    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        epoch = int(time.time())
        path  = os.path.join(LOG_DIR, f"{epoch}.txt")
        self._fh   = open(path, "w", buffering=1)  # line-buffered
        self._path = path
        # Also mirror to console via Python logging
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(message)s"
        )
        self._log = logging.getLogger("pingpong")
        self._log.info(f"Log file: {path}")

    def _ts(self):
        """Return human-readable timestamp matching spec: Mon  2 Mar 21:37:34 PST 2026"""
        # We use local time; 'PST' is hard-coded – replace with your zone if needed
        now = datetime.now()
        # day with leading space if single digit (spec shows " 2" not "02")
        day = now.strftime("%-d")          # Linux: no zero-pad
        day_padded = day.rjust(2)          # right-justify in 2 chars = " 2"
        ts  = now.strftime(f"%a {day_padded} %b %H:%M:%S PST %Y")
        return ts

    def write(self, text, newline=True):
        """Raw write to log file."""
        self._fh.write(text + ("\n" if newline else ""))
        self._fh.flush()

    def blank(self):
        self.write("")

    def event(self, msg):
        """Timestamped event line."""
        line = f"{self._ts()} - {msg}"
        self.write(line)
        self._log.info(line)

    def serve_header(self, gs: GameState):
        """
        Emit:   Green/Left serving (1)
        Called immediately after any serve change or game start.
        server is stored as a side ("left"/"right"); colour is derived.
        """
        colour = gs.server_colour().capitalize()   # "Green" or "Blue"
        side   = gs.server_side_label()            # "Left"  or "Right"
        n      = gs.serve_count
        self.write(f"{colour}/{side} serving ({n})")

    def serve_change(self):
        """Emit 'Change of serve' then blank line."""
        self.write("Change of serve")
        self.blank()

    def close(self):
        self._fh.close()


# =============================================================================
#  IMAGE GENERATION  (ImageMagick → BMP → IT8951)
# =============================================================================

class DisplayManager:
    """
    Generates BMP images with ImageMagick and pushes them to the e-paper.

    Pre-generation: after every state change we call pregenerate() which
    renders all likely *next* screens into named files in IMAGE_DIR.
    When a button is pressed we already have the file ready – we just call
    show(filename).

    Hand-drawn artwork override: if a file named
        IMAGE_DIR/override/<key>.bmp
    exists it is used instead of the generated version.  'key' is the same
    string used as the pre-generated filename stem.
    """

    def __init__(self):
        os.makedirs(IMAGE_DIR, exist_ok=True)
        os.makedirs(os.path.join(IMAGE_DIR, "override"), exist_ok=True)
        self._current_file = None
        self._pregen_thread = None
        self._pregen_lock   = threading.Lock()

    # ── Low-level rendering ───────────────────────────────────────────────

    def _run_imagemagick(self, args: list, outfile: str) -> bool:
        """Run an ImageMagick convert command. Returns True on success."""
        cmd = ["convert"] + args + [outfile]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            if result.returncode != 0:
                logging.warning(f"ImageMagick error: {result.stderr.decode()}")
                return False
            return True
        except Exception as e:
            logging.error(f"ImageMagick exception: {e}")
            return False

    def _make_score_bmp(
        self,
        left_score: int, right_score: int,
        left_label: str, right_label: str,
        server_side: str,           # "left" or "right"
        serve_count: int,
        game_num: int,
        games_left: int, games_right: int,
        outfile: str
    ):
        """
        Generate the main in-game score screen.

        Layout (800×600):
          Row 1 (top):   left player label          right player label
          Row 2 (mid):   BIG left score   •   BIG right score
          Row 3 (bot):   serve indicator     game counter
        """
        w, h = DISPLAY_WIDTH, DISPLAY_HEIGHT

        # Serve indicator: underline the serving side label
        # We draw a circle/dot under the server's score
        serve_x = 200 if server_side == "left" else 600
        serve_mark = f"-fill black -draw 'circle {serve_x},420 {serve_x+18},420'"

        # Annotate: left label
        args = [
            "-size", f"{w}x{h}",
            f"xc:{IM_BG}",
            "-font", IM_FONT,
            "-fill", IM_FG,
            # Left player label
            "-pointsize", str(LABEL_FONT_SIZE),
            "-gravity", "NorthWest",
            "-annotate", "+40+30", left_label,
            # Right player label
            "-gravity", "NorthEast",
            "-annotate", "+40+30", right_label,
            # Left score
            "-pointsize", str(SCORE_FONT_SIZE),
            "-gravity", "West",
            "-annotate", "+60-40", str(left_score),
            # Right score
            "-gravity", "East",
            "-annotate", "+60-40", str(right_score),
            # Centre divider dash
            "-pointsize", str(SCORE_FONT_SIZE),
            "-gravity", "Center",
            "-annotate", "+0-40", "—",
            # Game score (top centre)
            "-pointsize", str(STATUS_FONT_SIZE),
            "-gravity", "North",
            "-annotate", f"+0+{LABEL_FONT_SIZE+10}",
            f"Game {game_num}   ({games_left} — {games_right})",
            # Serve indicator label (bottom)
            "-pointsize", str(STATUS_FONT_SIZE),
            "-gravity", "South",
            "-annotate", "+0+30",
            f"{'▶' if server_side=='left' else '   '}  Serving  {'◀' if server_side=='right' else '   '} ({serve_count})",
        ]
        self._run_imagemagick(args, outfile)

    def _make_text_bmp(self, lines: list, outfile: str, font_size: int = 48):
        """
        Simple full-screen text BMP for menus/status screens.
        lines: list of strings, centre-aligned vertically & horizontally.
        """
        w, h = DISPLAY_WIDTH, DISPLAY_HEIGHT
        # Build multiline label
        text = "\n".join(lines)
        args = [
            "-size", f"{w}x{h}",
            f"xc:{IM_BG}",
            "-font", IM_FONT,
            "-fill", IM_FG,
            "-pointsize", str(font_size),
            "-gravity", "Center",
            "-annotate", "+0+0", text,
        ]
        self._run_imagemagick(args, outfile)

    # ── Override hook ─────────────────────────────────────────────────────

    def _resolve(self, key: str) -> str:
        """
        Return override path if it exists, otherwise generated path.
        key is a short identifier like 'score_5_3_L1' or 'menu_race'.
        """
        override = os.path.join(IMAGE_DIR, "override", f"{key}.bmp")
        if os.path.exists(override):
            return override
        return os.path.join(IMAGE_DIR, f"{key}.bmp")

    # ── Public: show a screen ─────────────────────────────────────────────

    def show(self, key: str):
        """Display the BMP identified by key on the e-paper."""
        path = self._resolve(key)
        if not os.path.exists(path):
            logging.warning(f"[Display] BMP not found for key '{key}', regenerating…")
            return   # caller should have pre-generated; log and skip
        self._current_file = path
        try:
            subprocess.Popen(
                [EPAPER_CMD, "0", "0", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            logging.error(f"[Display] IT8951 command failed: {e}")

    def show_file(self, path: str):
        """Show an arbitrary BMP file path directly."""
        self._current_file = path
        try:
            subprocess.Popen(
                [EPAPER_CMD, "0", "0", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            logging.error(f"[Display] IT8951 command failed: {e}")

    # ── Key builders ──────────────────────────────────────────────────────

    @staticmethod
    def score_key(gs: GameState) -> str:
        """
        Canonical key for a score screen — fully positional.
        e.g.  score_L3_R7_svL_s2_gm2_gl1_rg0
        (L=left points, R=right points, sv=serving side, s=serve number,
         gm=game number, gl=games won by left, rg=games won by right)
        """
        return (
            f"score_L{gs.score['left']}_R{gs.score['right']}"
            f"_sv{gs.server}_s{gs.serve_count}"
            f"_gm{gs.current_game}"
            f"_gl{gs.games_won['left']}_rg{gs.games_won['right']}"
        )

    # ── Pre-generation helpers ────────────────────────────────────────────

    def _gen_score_screen(self, gs: GameState, key: str):
        """Render a score BMP for the given game state."""
        outfile = os.path.join(IMAGE_DIR, f"{key}.bmp")
        if os.path.exists(self._resolve(key)):
            return   # already exists or override present
        self._make_score_bmp(
            left_score   = gs.score["left"],
            right_score  = gs.score["right"],
            left_label   = "Green",   # green is always left button
            right_label  = "Blue",    # blue  is always right button
            server_side  = gs.server,
            serve_count  = gs.serve_count,
            game_num     = gs.current_game,
            games_left   = gs.games_won["left"],
            games_right  = gs.games_won["right"],
            outfile      = outfile
        )

    def pregenerate_score_screens(self, gs: GameState):
        """
        After any score change, immediately render the three most likely
        next screens in a background thread so they are ready instantly:
          1. Current state (already displayed, but regenerate if missing)
          2. Left side scores next  (green button pressed)
          3. Right side scores next (blue button pressed)
        All simulation is purely positional (left/right).
        """
        def _work():
            with self._pregen_lock:
                # Current
                self._gen_score_screen(gs, self.score_key(gs))

                # Left side scores next
                gs_l = gs.clone()
                _apply_point(gs_l, "left")
                self._gen_score_screen(gs_l, self.score_key(gs_l))

                # Right side scores next
                gs_r = gs.clone()
                _apply_point(gs_r, "right")
                self._gen_score_screen(gs_r, self.score_key(gs_r))

        t = threading.Thread(target=_work, daemon=True)
        t.start()

    def generate_menu(self, key: str, lines: list, font_size: int = 56):
        """Render a menu/status text screen (blocking – called before show)."""
        outfile = os.path.join(IMAGE_DIR, f"{key}.bmp")
        if not os.path.exists(self._resolve(key)):
            self._make_text_bmp(lines, outfile, font_size)

    def generate_and_show_menu(self, key: str, lines: list, font_size: int = 56):
        """Render + display a text menu screen."""
        self.generate_menu(key, lines, font_size)
        self.show(key)


# =============================================================================
#  PURE GAME LOGIC  (stateless helpers – operate on GameState clones)
#
#  All functions work in terms of "left" and "right" sides.
#  Button colour → side translation happens only at the engine boundary
#  (handle_button / _handle_score) via GameState.colour_to_side().
# =============================================================================

def _advance_serve(gs: GameState) -> bool:
    """
    Advance the serve counter.  2 serves per side always, even at deuce.
    Modifies gs in-place.
    Returns True if there was a change of server (i.e. server rotated).
    """
    if gs.serve_count == 1:
        gs.serve_count = 2
        return False   # same server, second serve
    else:
        # Rotate to the other side
        gs.serve_count = 1
        gs.server = "right" if gs.server == "left" else "left"
        return True    # server changed


def _apply_point(gs: GameState, side: str) -> bool:
    """
    Award a point to the given side ("left" or "right").
    Advance serve counter.
    Does NOT check for game win – use check_game_win() after calling this.
    Modifies gs in-place.
    Returns True if there was a change of server.
    """
    gs.score[side] += 1
    return _advance_serve(gs)


def check_game_win(gs: GameState):
    """
    Return the winning side ("left" | "right") if the current game is over,
    else None.
    Win condition: score >= race_to AND lead >= 2 (win-by-two rule).
    """
    l = gs.score["left"]
    r = gs.score["right"]
    if l >= gs.race_to or r >= gs.race_to:
        if abs(l - r) >= 2:
            return "left" if l > r else "right"
    return None


def swap_games_won(gs: GameState):
    """
    When players physically change ends between games, the left/right
    game-win counts must also swap so they stay positional.

    Example: Andrew won game 1 while on the left → games_won = {left:1, right:0}.
    After swapping ends, Andrew is on the right, so the same fact is now
    expressed as games_won = {left:0, right:1}.
    """
    gs.games_won["left"], gs.games_won["right"] = (
        gs.games_won["right"],
        gs.games_won["left"],
    )


def start_new_game(gs: GameState, winning_side: str):
    """
    Prepare for the next game:
      1. Record the winning side BEFORE swapping (caller already appended history).
      2. Swap games_won so they remain positional after the side change.
      3. Reset per-game points to 0–0.
      4. Increment game counter.
      5. Set winning_side as the server for the new game.
         (winning_side is passed in BEFORE the swap, so after the swap the
          winner is on the opposite side — we flip it here.)

    The serve is awarded to the player who won the previous game, regardless
    of which side they are now on.  Because winning_side was their side
    BEFORE the swap, after the swap they are on the other side.
    """
    # After the swap, the winner is on the opposite side to winning_side.
    new_server = "right" if winning_side == "left" else "left"

    swap_games_won(gs)          # flip games_won to match new positions
    gs.score        = {"left": 0, "right": 0}
    gs.current_game += 1
    gs.server       = new_server
    gs.serve_count  = 1


def match_winner(gs: GameState):
    """
    Return the winning side ("left" | "right") if the match is decided,
    else None.  Uses current positional games_won.
    """
    needed = (gs.best_of // 2) + 1   # 2 for BO3, 3 for BO5
    for side in ("left", "right"):
        if gs.games_won[side] >= needed:
            return side
    return None


# =============================================================================
#  MATCH ENGINE  (the main controller)
# =============================================================================

class MatchEngine:
    """
    Central controller that:
      - Owns the GameState and undo stack
      - Processes button events
      - Drives the display
      - Writes the log
    """

    def __init__(self, display: DisplayManager, logger: MatchLogger):
        self.display    = display
        self.logger     = logger
        self.gs         = GameState()
        self._undo_stack: list[GameState] = []   # stack of snapshots

        # Track connected buttons for WAITING_BUTTONS state
        self._connected  = {"green": False, "blue": False}

        # Queue for incoming button events (thread-safe)
        self.event_queue = queue.Queue()

        # For WIN_CONFIRM: track who confirmed
        self._win_confirmed = {"green": False, "blue": False}

        # For CONFIRM_RULES: track who confirmed
        self._rules_confirmed = {"green": False, "blue": False}

    # ── Undo stack ────────────────────────────────────────────────────────

    def _push_undo(self):
        """Save current state before a mutation."""
        self._undo_stack.append(self.gs.clone())

    def _pop_undo(self):
        """Restore previous state. Returns True if successful."""
        if self._undo_stack:
            self.gs = self._undo_stack.pop()
            return True
        return False

    # ── Event entry points ────────────────────────────────────────────────

    def handle_button(self, colour: str, press_type: str):
        """
        Main dispatcher.  Called from the MQTT thread (via queue) or
        directly from the simulation thread.

        colour:     "green" | "blue"
        press_type: "short" | "double" | "long"
        """
        gs = self.gs   # convenience alias

        # ── LONG PRESS: always resets the entire match ─────────────────────
        if press_type == "long":
            self.logger.event(f"{colour.capitalize()} long press. Resetting entire match.")
            self._full_reset()
            return

        # ── DOUBLE PRESS: undo last action ─────────────────────────────────
        if press_type == "double":
            self._handle_undo(colour)
            return

        # ── SHORT PRESS dispatched by state ────────────────────────────────
        if press_type == "short":
            self._handle_short(colour)

    def _handle_short(self, colour: str):
        gs = self.gs
        state = gs.state

        if state == State.WAITING_BUTTONS:
            # Buttons not used here; connection events drive this state
            pass

        elif state == State.RULE_RACE:
            self._push_undo()
            if colour == "green":
                gs.race_to = 11
                self.logger.event(f"Green button pressed - Setting game mode to Race to 11")
            else:
                gs.race_to = 21
                self.logger.event(f"Blue button pressed - Setting game mode to Race to 21")
            gs.state = State.RULE_BO
            self._show_rule_bo()

        elif state == State.RULE_BO:
            self._push_undo()
            if colour == "green":
                gs.best_of = 3
                self.logger.event(f"Green button pressed - Setting match to best of 3")
            else:
                gs.best_of = 5
                self.logger.event(f"Blue button pressed - Setting match to best of 5")
            gs.state = State.CONFIRM_RULES
            self._rules_confirmed = {"green": False, "blue": False}
            self._show_confirm_rules()

        elif state == State.CONFIRM_RULES:
            self._rules_confirmed[colour] = True
            self.logger.event(
                f"{colour.capitalize()} button pressed - Confirmed rules "
                f"({'both' if all(self._rules_confirmed.values()) else 'waiting for other player'})"
            )
            if all(self._rules_confirmed.values()):
                self._push_undo()
                gs.state = State.SERVING_CHOICE
                self._show_serving_choice()

        elif state == State.SERVING_CHOICE:
            self._push_undo()
            # Translate the button colour to a side — green=left, blue=right.
            # That side becomes the first server.
            side = GameState.colour_to_side(colour)
            gs.server      = side
            gs.serve_count = 1
            gs.state       = State.PLAYING
            self.logger.event(
                f"{colour.capitalize()} button pressed. "
                f"{colour.capitalize()} on {side.capitalize()} serves first."
            )
            self.logger.blank()
            self.logger.serve_header(gs)
            self._show_score()
            self.display.pregenerate_score_screens(gs)

        elif state == State.PLAYING:
            self._handle_score(colour)

        elif state == State.WIN_CONFIRM:
            self._handle_win_confirm(colour)

        elif state == State.MATCH_OVER:
            # Any press re-shows summary; long press resets (handled above)
            self._show_match_summary()

    # ── Scoring ───────────────────────────────────────────────────────────

    def _handle_score(self, colour: str):
        """
        Award a point to the side that matches the pressed button colour.
        Green button → left side.  Blue button → right side.
        All score/games_won tracking is positional (left/right).
        """
        gs = self.gs
        self._push_undo()

        # Translate button colour to the physical side it always occupies.
        side = GameState.colour_to_side(colour)

        changed_server = _apply_point(gs, side)

        left_score  = gs.score["left"]
        right_score = gs.score["right"]
        score_str   = f"{left_score}-{right_score}"

        # ── Log the point ──────────────────────────────────────────────────
        self.logger.event(
            f"{colour.capitalize()} button pressed. "
            f"{colour.capitalize()} scores. {score_str}"
        )

        # ── Check for game win ─────────────────────────────────────────────
        winning_side = check_game_win(gs)
        if winning_side:
            # Record the final points score for this game BEFORE resetting.
            gs.game_history.append({
                "left":  gs.score["left"],
                "right": gs.score["right"],
                "winner_side":   winning_side,
                "winner_colour": GameState.side_to_colour(winning_side),
            })
            # Award the game win to the winning side (positional).
            gs.games_won[winning_side] += 1
            gs.game_winner = winning_side

            if changed_server:
                self.logger.serve_change()
            else:
                self.logger.blank()

            winner_colour = GameState.side_to_colour(winning_side)
            self.logger.event(
                f"{winner_colour.capitalize()} wins game {gs.current_game}! "
                f"Games: {gs.games_won['left']}–{gs.games_won['right']} "
                f"(left–right)"
            )

            # Check for match win BEFORE calling start_new_game (which swaps).
            m_winner = match_winner(gs)
            if m_winner:
                gs.state = State.MATCH_OVER
                self._show_match_summary()
                return

            # Transition to WIN_CONFIRM
            gs.state = State.WIN_CONFIRM
            self._win_confirmed = {"green": False, "blue": False}

            # After game 2 of a best-of-3 with scores 1–1: offer to extend.
            total_games = gs.games_won["left"] + gs.games_won["right"]
            if gs.best_of == 3 and total_games == 2:
                gs.extend_prompt = True
                self._show_extend_prompt()
            else:
                self._show_win_confirm(winning_side)
            return

        # ── Normal point: log serve info ───────────────────────────────────
        if changed_server:
            self.logger.serve_change()
        else:
            self.logger.blank()
        self.logger.serve_header(gs)

        # ── Update display ─────────────────────────────────────────────────
        key = DisplayManager.score_key(gs)
        target = self.display._resolve(key)
        if not os.path.exists(target):
            self.display._gen_score_screen(gs, key)
        self.display.show(key)

        # ── Pre-generate next likely screens ──────────────────────────────
        self.display.pregenerate_score_screens(gs)

    # ── Win confirmation / game transition ────────────────────────────────

    def _handle_win_confirm(self, colour: str):
        gs = self.gs

        if gs.extend_prompt:
            # Green button (left) = YES extend to 5.
            # Blue button (right) = NO, end match now.
            if colour == "blue":
                self.logger.event("Blue button pressed. Players chose NOT to extend to best of 5.")
                gs.state = State.MATCH_OVER
                self._show_match_summary()
            else:
                self.logger.event("Green button pressed. Extending match to best of 5!")
                self._push_undo()
                gs.best_of       = 5
                gs.extend_prompt = False
                winning_side     = gs.game_winner   # side that won the last game
                start_new_game(gs, winning_side)    # swaps sides + games_won
                gs.state = State.PLAYING
                self.logger.blank()
                self.logger.serve_header(gs)
                self._show_score()
                self.display.pregenerate_score_screens(gs)
            return

        # Normal end-of-game: both players short-press to confirm.
        self._win_confirmed[colour] = True
        both = all(self._win_confirmed.values())
        self.logger.event(
            f"{colour.capitalize()} confirmed. "
            f"{'Both confirmed – starting next game.' if both else 'Waiting for other player.'}"
        )
        if both:
            self._push_undo()
            winning_side = gs.game_winner
            start_new_game(gs, winning_side)   # swaps sides + games_won
            gs.state = State.PLAYING
            self.logger.blank()
            self.logger.serve_header(gs)
            self._show_score()
            self.display.pregenerate_score_screens(gs)

    # ── Undo ──────────────────────────────────────────────────────────────

    def _handle_undo(self, colour: str):
        """Undo the last action and restore the previous state."""
        if not self._undo_stack:
            self.logger.event(f"{colour.capitalize()} double pressed. Nothing to undo.")
            return

        self._pop_undo()
        gs = self.gs

        # Score is positional — read directly from left/right.
        score_str = f"{gs.score['left']}-{gs.score['right']}"

        self.logger.event(
            f"{colour.capitalize()} double pressed. Score reverted. {score_str}"
        )
        self.logger.blank()
        self.logger.serve_header(gs)

        # Refresh display for the restored state.
        if gs.state == State.PLAYING:
            key = DisplayManager.score_key(gs)
            target = self.display._resolve(key)
            if not os.path.exists(target):
                self.display._gen_score_screen(gs, key)
            self.display.show(key)
            self.display.pregenerate_score_screens(gs)
        else:
            self._redraw_current_state()

    # ── Full reset ────────────────────────────────────────────────────────

    def _full_reset(self):
        """Reset everything back to RULE_RACE."""
        self.gs           = GameState()
        self._undo_stack  = []
        self._connected   = {"green": False, "blue": False}
        self.gs.state     = State.WAITING_BUTTONS
        self.logger.blank()
        self.logger.event("=== FULL RESET – New match starting ===")
        self.logger.blank()
        self._show_waiting()

    # ── Connection management ─────────────────────────────────────────────

    def on_button_connected(self, colour: str):
        """Called when an ESP32 connects and sends its hello."""
        self._connected[colour] = True
        self.logger.event(f"{colour.capitalize()} button connected.")
        self._show_waiting()
        if all(self._connected.values()):
            self.logger.event("Both buttons connected. Starting rule selection.")
            self.gs.state = State.RULE_RACE
            self._show_rule_race()

    # ── Display helpers ───────────────────────────────────────────────────

    def _show_waiting(self):
        g = "✓ Connected" if self._connected["green"] else "✗ Waiting…"
        b = "✓ Connected" if self._connected["blue"]  else "✗ Waiting…"
        lines = [
            "Ping-Pong Scorer",
            "",
            f"Green button:  {g}",
            f"Blue button:   {b}",
            "",
            "Waiting for both buttons…"
        ]
        self.display.generate_and_show_menu("waiting_buttons", lines)

    def _show_rule_race(self):
        lines = [
            "Select Race-To",
            "",
            "GREEN  =  Race to 11",
            "",
            "BLUE   =  Race to 21",
        ]
        self.display.generate_and_show_menu("menu_race", lines)
        self.logger.event("Displaying race-to selection.")

    def _show_rule_bo(self):
        lines = [
            f"Race to {self.gs.race_to} selected.",
            "",
            "Select Match Length",
            "",
            "GREEN  =  Best of 3",
            "",
            "BLUE   =  Best of 5",
        ]
        self.display.generate_and_show_menu("menu_bo", lines)
        self.logger.event("Displaying best-of selection.")

    def _show_confirm_rules(self):
        gs = self.gs
        lines = [
            f"Rules: Race to {gs.race_to}  |  Best of {gs.best_of}",
            "",
            "Both players: press your button",
            "to confirm.",
        ]
        self.display.generate_and_show_menu("confirm_rules", lines)
        self.logger.event("Waiting for both players to confirm rules.")

    def _show_serving_choice(self):
        lines = [
            "Who serves first?",
            "",
            "Press YOUR button to serve first.",
            "",
            "GREEN = left side",
            "BLUE  = right side",
        ]
        self.display.generate_and_show_menu("serving_choice", lines)
        self.logger.write("Waiting for next button press to determine who serves first")

    def _show_score(self):
        gs  = self.gs
        key = DisplayManager.score_key(gs)
        target = self.display._resolve(key)
        if not os.path.exists(target):
            self.display._gen_score_screen(gs, key)
        self.display.show(key)

    def _show_win_confirm(self, winning_side: str):
        gs = self.gs
        winner_colour = GameState.side_to_colour(winning_side)
        lines = [
            f"Game {gs.current_game} over!",
            "",
            f"{winner_colour.capitalize()} ({winning_side}) wins!",
            f"Games:  Left {gs.games_won['left']} – {gs.games_won['right']} Right",
            "",
            "Both players press to start next game.",
        ]
        key = f"win_confirm_g{gs.current_game}"
        self.display.generate_and_show_menu(key, lines)

    def _show_extend_prompt(self):
        gs = self.gs
        lines = [
            f"Games tied  {gs.games_won['left']}–{gs.games_won['right']}!",
            "",
            "Extend to Best of 5?",
            "",
            "GREEN (left) = YES   |   BLUE (right) = NO",
        ]
        self.display.generate_and_show_menu("extend_prompt", lines)
        self.logger.event("Asking players if they want to extend to best of 5.")

    def _show_match_summary(self):
        gs = self.gs
        w  = match_winner(gs)   # "left" or "right" side
        w_colour = GameState.side_to_colour(w) if w else "unknown"
        lines = ["=== MATCH OVER ===", ""]
        for i, g in enumerate(gs.game_history, 1):
            wc = g["winner_colour"].capitalize()
            ws = g["winner_side"].capitalize()
            lines.append(
                f"Game {i}: Left {g['left']} – {g['right']} Right  "
                f"({wc}/{ws} wins)"
            )
        lines += [
            "",
            f"Games:  Left {gs.games_won['left']} – {gs.games_won['right']} Right",
            "",
            f"WINNER: {w_colour.upper()} ({w.upper() if w else '???'} side)",
            "",
            "Long press either button to play again.",
        ]
        key = "match_summary"
        self.display.generate_and_show_menu(key, lines, font_size=42)
        self.logger.blank()
        self.logger.event(f"=== MATCH OVER. Winner: {w_colour} ({w} side) ===")
        for i, g in enumerate(gs.game_history, 1):
            self.logger.event(
                f"  Game {i}: Left {g['left']} – {g['right']} Right "
                f"({g['winner_colour']} wins)"
            )

    def _redraw_current_state(self):
        """Re-render whatever the current state demands after an undo."""
        s = self.gs.state
        if s == State.RULE_RACE:
            self._show_rule_race()
        elif s == State.RULE_BO:
            self._show_rule_bo()
        elif s == State.CONFIRM_RULES:
            self._show_confirm_rules()
        elif s == State.SERVING_CHOICE:
            self._show_serving_choice()
        elif s == State.PLAYING:
            self._show_score()
        elif s == State.WIN_CONFIRM:
            if self.gs.extend_prompt:
                self._show_extend_prompt()
            else:
                self._show_win_confirm(self.gs.game_winner)
        elif s == State.MATCH_OVER:
            self._show_match_summary()

    # ── Main loop ─────────────────────────────────────────────────────────

    def run(self):
        """Process events from the queue forever."""
        self.logger.event("New game started.")
        self._show_waiting()
        while True:
            try:
                colour, press_type = self.event_queue.get(timeout=1)
                self.handle_button(colour, press_type)
            except queue.Empty:
                pass   # keepalive tick
            except Exception as e:
                logging.exception(f"Error in event handler: {e}")
                # Never crash; log and continue


# =============================================================================
#  MQTT CLIENT
# =============================================================================

class MQTTClient:
    """
    Wraps paho-mqtt with automatic reconnect.
    Feeds events into the engine's queue.
    Also listens on status/green and status/blue for connection announcements.
    """

    def __init__(self, engine: MatchEngine):
        self.engine = engine
        self._client = None
        self._connected_to_broker = False

    def start(self):
        if not MQTT_AVAILABLE:
            logging.warning("paho-mqtt not available – MQTT client not started.")
            return
        self._client = mqtt.Client(client_id="pingpong_pi")
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        """Reconnect loop – never gives up."""
        while True:
            try:
                self._client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_KEEPALIVE)
                self._client.loop_forever()
            except Exception as e:
                logging.error(f"[MQTT] Connection failed: {e}. Retrying in {MQTT_RECONNECT_DELAY}s…")
                time.sleep(MQTT_RECONNECT_DELAY)

    def _on_connect(self, client, userdata, flags, rc):
        logging.info(f"[MQTT] Connected to broker (rc={rc})")
        self._connected_to_broker = True
        client.subscribe(MQTT_TOPIC_GREEN)
        client.subscribe(MQTT_TOPIC_BLUE)
        client.subscribe(MQTT_STATUS_GREEN)
        client.subscribe(MQTT_STATUS_BLUE)

    def _on_disconnect(self, client, userdata, rc):
        logging.warning(f"[MQTT] Disconnected (rc={rc}). Will reconnect…")
        self._connected_to_broker = False

    def _on_message(self, client, userdata, msg):
        topic   = msg.topic
        payload = msg.payload.decode("utf-8").strip().lower()
        logging.debug(f"[MQTT] {topic} → {payload}")

        # Connection announcements from ESP32s
        if topic == MQTT_STATUS_GREEN and payload == "connected":
            self.engine.on_button_connected("green")
            return
        if topic == MQTT_STATUS_BLUE and payload == "connected":
            self.engine.on_button_connected("blue")
            return

        # Button events
        if topic == MQTT_TOPIC_GREEN and payload in ("short", "double", "long"):
            self.engine.event_queue.put(("green", payload))
        elif topic == MQTT_TOPIC_BLUE and payload in ("short", "double", "long"):
            self.engine.event_queue.put(("blue", payload))


# =============================================================================
#  SIMULATION MODE  (type commands from the console)
# =============================================================================

def run_simulation(engine: MatchEngine):
    """
    Read lines from stdin and inject them as button events.
    Syntax:
        g         → green short press
        b         → blue short press
        gg        → green double press
        bb        → blue double press
        GL        → green long press
        BL        → blue long press
        connect   → simulate both buttons connecting
    """
    print("\n=== SIMULATION MODE ===")
    print("Commands: g/b (short)  gg/bb (double)  GL/BL (long)  connect")
    print("Type 'connect' first to simulate both buttons connecting.\n")

    # Simulate connection if desired immediately
    def _sim_input():
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
                print("Unknown command. Use: g b gg bb GL BL connect")

    t = threading.Thread(target=_sim_input, daemon=True)
    t.start()


# =============================================================================
#  ENTRY POINT
# =============================================================================

def main():
    # Ensure image directory exists
    os.makedirs(IMAGE_DIR, exist_ok=True)

    # Create subsystems
    logger  = MatchLogger()
    display = DisplayManager()
    engine  = MatchEngine(display, logger)

    # Graceful shutdown handler (Ctrl+C, SIGTERM)
    def _shutdown(sig, frame):
        logger.event("Shutdown signal received. Closing log.")
        logger.close()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Start MQTT (no-op if paho not installed)
    if not SIMULATION_MODE:
        mqtt_client = MQTTClient(engine)
        mqtt_client.start()
    else:
        run_simulation(engine)

    # Block forever in the engine loop
    engine.run()


if __name__ == "__main__":
    main()
