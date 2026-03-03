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
    """

    def __init__(self):
        # ── Rules ──────────────────────────────────────────────────────────
        self.race_to      = 11        # 11 or 21
        self.best_of      = 3         # 3 or 5

        # ── Match progress ─────────────────────────────────────────────────
        self.games_won    = {"green": 0, "blue": 0}  # games won in match
        self.current_game = 1         # 1-based game counter

        # ── Current game score ─────────────────────────────────────────────
        # left / right are the *sides*, which swap each game.
        # green and blue always refer to the physical button colour.
        # We track by colour internally; display maps to side.
        self.score        = {"green": 0, "blue": 0}

        # ── Sides ──────────────────────────────────────────────────────────
        # which colour is currently on the LEFT side
        self.left_player  = "green"   # "green" or "blue"

        # ── Serve ──────────────────────────────────────────────────────────
        self.server       = "green"   # colour of current server
        self.serve_count  = 1         # 1 or 2 within this server's turn

        # ── State machine ──────────────────────────────────────────────────
        self.state        = State.WAITING_BUTTONS

        # ── Confirmation tracking (CONFIRM_RULES) ──────────────────────────
        self.confirmed    = {"green": False, "blue": False}

        # ── WIN_CONFIRM prompt ─────────────────────────────────────────────
        # After best-of-3 we ask "extend to 5?". True = yes-question active.
        self.extend_prompt = False
        self.game_winner  = None      # colour of game winner (set at WIN_CONFIRM)

        # ── History of game scores (for match summary) ──────────────────────
        # list of {"green": g_score, "blue": b_score, "winner": colour}
        self.game_history = []

    def right_player(self):
        """Return the colour on the right side."""
        return "blue" if self.left_player == "green" else "green"

    def score_left(self):
        return self.score[self.left_player]

    def score_right(self):
        return self.score[self.right_player()]

    def server_side(self):
        """Return 'Left' or 'Right' based on who is serving."""
        return "Left" if self.server == self.left_player else "Right"

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
        """
        colour = gs.server.capitalize()
        side   = gs.server_side()
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
        Canonical key for a score screen.
        e.g.  score_G3_B7_sv_green_s2_gm2
        """
        return (
            f"score_G{gs.score['green']}_B{gs.score['blue']}"
            f"_sv{gs.server}_s{gs.serve_count}"
            f"_gm{gs.current_game}"
            f"_gl{gs.games_won['green']}_bl{gs.games_won['blue']}"
        )

    # ── Pre-generation helpers ────────────────────────────────────────────

    def _gen_score_screen(self, gs: GameState, key: str):
        """Render a score BMP for the given game state."""
        left  = gs.left_player
        right = gs.right_player()
        outfile = os.path.join(IMAGE_DIR, f"{key}.bmp")
        if os.path.exists(self._resolve(key)):
            return   # already exists or override present
        self._make_score_bmp(
            left_score   = gs.score[left],
            right_score  = gs.score[right],
            left_label   = left.capitalize(),
            right_label  = right.capitalize(),
            server_side  = gs.server_side().lower(),
            serve_count  = gs.serve_count,
            game_num     = gs.current_game,
            games_left   = gs.games_won[left],
            games_right  = gs.games_won[right],
            outfile      = outfile
        )

    def pregenerate_score_screens(self, gs: GameState):
        """
        After any score change, immediately render the three most likely
        next screens in a background thread so they are ready instantly:
          1. Current state (already displayed, but regenerate if missing)
          2. Left player scores next
          3. Right player scores next
          4. Undo (previous state – stored separately by the engine)
        """
        def _work():
            with self._pregen_lock:
                # Current
                self._gen_score_screen(gs, self.score_key(gs))

                # --- Simulate left scores ---
                gs_l = gs.clone()
                _apply_point(gs_l, gs_l.left_player)
                self._gen_score_screen(gs_l, self.score_key(gs_l))

                # --- Simulate right scores ---
                gs_r = gs.clone()
                _apply_point(gs_r, gs_r.right_player())
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
# =============================================================================

def _advance_serve(gs: GameState):
    """
    Advance the serve counter.  2 serves per player always,
    even at deuce.  Modifies gs in-place.
    Returns True if there was a change of server.
    """
    if gs.serve_count == 1:
        gs.serve_count = 2
        return False   # same server, second serve
    else:
        # rotate server
        gs.serve_count = 1
        gs.server = gs.right_player() if gs.server == gs.left_player else gs.left_player
        return True    # server changed


def _apply_point(gs: GameState, scorer: str):
    """
    Award a point to scorer (colour string).
    Advance serve counter.
    Does NOT check for game win – use check_game_win() for that.
    Modifies gs in-place.
    Returns True if there was a change of server.
    """
    gs.score[scorer] += 1
    return _advance_serve(gs)


def check_game_win(gs: GameState):
    """
    Return the winning colour if the current game is over, else None.
    Win condition: score >= race_to AND lead >= 2.
    """
    g = gs.score["green"]
    b = gs.score["blue"]
    needed = gs.race_to
    if g >= needed or b >= needed:
        if abs(g - b) >= 2:
            return "green" if g > b else "blue"
    return None


def _swap_sides(gs: GameState):
    """Swap left/right player assignment."""
    gs.left_player = gs.right_player()


def start_new_game(gs: GameState, winner_of_last: str):
    """
    Reset per-game score, swap sides, set winner as server.
    Call AFTER recording the completed game in game_history.
    """
    gs.score      = {"green": 0, "blue": 0}
    gs.current_game += 1
    _swap_sides(gs)
    gs.server     = winner_of_last
    gs.serve_count = 1


def match_winner(gs: GameState):
    """Return the colour that has won the match, or None."""
    needed = (gs.best_of // 2) + 1   # 2 for BO3, 3 for BO5
    for colour in ("green", "blue"):
        if gs.games_won[colour] >= needed:
            return colour
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
            # The player who presses first will serve first
            # Their side is always their colour: green=left, blue=right
            gs.server      = colour
            gs.serve_count = 1
            gs.state       = State.PLAYING
            side = "Left" if colour == gs.left_player else "Right"
            self.logger.event(
                f"{colour.capitalize()} button pressed. "
                f"{colour.capitalize()} on {side}."
            )
            self.logger.blank()
            # Print first serve header
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
        """Award a point to the player who pressed their button."""
        gs = self.gs
        self._push_undo()

        prev_server     = gs.server
        prev_serve_count = gs.serve_count

        changed_server = _apply_point(gs, colour)

        left  = gs.left_player
        right = gs.right_player()
        left_score  = gs.score[left]
        right_score = gs.score[right]
        score_str   = f"{left_score}-{right_score}"

        # ── Log the point ──────────────────────────────────────────────────
        self.logger.event(
            f"{colour.capitalize()} button pressed. "
            f"{colour.capitalize()} scores. {score_str}"
        )

        # ── Check for game win ─────────────────────────────────────────────
        winner = check_game_win(gs)
        if winner:
            gs.game_history.append({
                "green": gs.score["green"],
                "blue":  gs.score["blue"],
                "winner": winner
            })
            gs.games_won[winner] += 1
            gs.game_winner = winner

            if changed_server:
                self.logger.serve_change()
            else:
                self.logger.blank()

            self.logger.event(
                f"{winner.capitalize()} wins game {gs.current_game}! "
                f"Score: {gs.games_won['green']}-{gs.games_won['blue']} in games."
            )

            # Check for match win
            m_winner = match_winner(gs)
            if m_winner:
                gs.state = State.MATCH_OVER
                self._show_match_summary()
                return

            # Transition to WIN_CONFIRM
            gs.state = State.WIN_CONFIRM
            self._win_confirmed = {"green": False, "blue": False}

            # Special case: after game 3 of a best-of-3, offer to extend
            games_played = sum(gs.games_won.values())
            if gs.best_of == 3 and games_played == 3:
                # One player just won the match already → match over (caught above)
                # This branch: score is 2-1 (someone needs to win a 4th game)
                # Actually if best_of==3 and no match winner yet, it's 1-1 after game 2.
                # After game 3 one player has 2 wins = match winner → caught above.
                # So this branch handles asking "extend to 5?" after game 2 (1-1 score).
                pass

            # After game N of best-of-3 at 1-1: offer extend
            if gs.best_of == 3 and gs.games_won["green"] == 1 and gs.games_won["blue"] == 1:
                gs.extend_prompt = True
                self._show_extend_prompt()
            else:
                self._show_win_confirm(winner)
            return

        # ── Normal point: log serve info ───────────────────────────────────
        if changed_server:
            self.logger.serve_change()
        else:
            self.logger.blank()
        self.logger.serve_header(gs)

        # ── Update display ─────────────────────────────────────────────────
        key = DisplayManager.score_key(gs)
        # Try to show pre-generated image; fall back to generating now
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
            # Green = Yes (extend to 5), Blue = No (end match)
            if colour == "blue":
                self.logger.event("Blue button pressed. Players chose NOT to extend to best of 5.")
                gs.state = State.MATCH_OVER
                self._show_match_summary()
            else:
                self.logger.event("Green button pressed. Extending match to best of 5!")
                self._push_undo()
                gs.best_of       = 5
                gs.extend_prompt = False
                # Continue – start next game
                winner = gs.game_winner
                gs.game_history_at_extend = len(gs.game_history)
                start_new_game(gs, winner)
                gs.state = State.PLAYING
                self.logger.blank()
                self.logger.serve_header(gs)
                self._show_score()
                self.display.pregenerate_score_screens(gs)
            return

        # Normal end-of-game confirmation: both players short-press
        self._win_confirmed[colour] = True
        self.logger.event(
            f"{colour.capitalize()} confirmed. "
            f"{'Both confirmed – starting next game.' if all(self._win_confirmed.values()) else 'Waiting for other player.'}"
        )
        if all(self._win_confirmed.values()):
            self._push_undo()
            winner = gs.game_winner
            start_new_game(gs, winner)
            gs.state = State.PLAYING
            self.logger.blank()
            self.logger.serve_header(gs)
            self._show_score()
            self.display.pregenerate_score_screens(gs)

    # ── Undo ──────────────────────────────────────────────────────────────

    def _handle_undo(self, colour: str):
        """Undo the last action."""
        if not self._undo_stack:
            self.logger.event(f"{colour.capitalize()} double pressed. Nothing to undo.")
            return

        prev = self.gs.clone()   # keep current to log from
        self._pop_undo()
        gs = self.gs

        left_score  = gs.score[gs.left_player]
        right_score = gs.score[gs.right_player()]
        score_str   = f"{left_score}-{right_score}"

        self.logger.event(
            f"{colour.capitalize()} double pressed. Score reverted. {score_str}"
        )
        self.logger.blank()
        self.logger.serve_header(gs)

        # Refresh display
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

    def _show_win_confirm(self, winner: str):
        gs = self.gs
        lines = [
            f"Game {gs.current_game} over!",
            "",
            f"{winner.capitalize()} wins!",
            f"Games: Green {gs.games_won['green']} – {gs.games_won['blue']} Blue",
            "",
            "Both players press to start next game.",
        ]
        key = f"win_confirm_g{gs.current_game}"
        self.display.generate_and_show_menu(key, lines)

    def _show_extend_prompt(self):
        gs = self.gs
        lines = [
            "Games tied 1–1!",
            "",
            "Extend to Best of 5?",
            "",
            "GREEN = YES   |   BLUE = NO",
        ]
        self.display.generate_and_show_menu("extend_prompt", lines)
        self.logger.event("Asking players if they want to extend to best of 5.")

    def _show_match_summary(self):
        gs = self.gs
        w  = match_winner(gs)
        lines = ["=== MATCH OVER ===", ""]
        for i, g in enumerate(gs.game_history, 1):
            lines.append(
                f"Game {i}: Green {g['green']} – {g['blue']} Blue  "
                f"({g['winner'].capitalize()} wins)"
            )
        lines += [
            "",
            f"Games: Green {gs.games_won['green']} – {gs.games_won['blue']} Blue",
            "",
            f"WINNER: {w.upper() if w else '???'}",
            "",
            "Long press to play again.",
        ]
        key = "match_summary"
        self.display.generate_and_show_menu(key, lines, font_size=42)
        self.logger.blank()
        self.logger.event(f"=== MATCH OVER. Winner: {w} ===")
        for i, g in enumerate(gs.game_history, 1):
            self.logger.event(
                f"  Game {i}: Green {g['green']} – {g['blue']} Blue ({g['winner']} wins)"
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
