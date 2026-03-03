#!/usr/bin/env bash
# =============================================================================
# PING-PONG MATCH SCORING SYSTEM
# Target hardware: Raspberry Pi Zero W v1
#                  800x600 monochrome e-paper display (IT8951 driver)
#                  Two ESP32 buttons over MQTT (green / blue)
#
# MQTT topics:   button/green  |  button/blue
# MQTT payloads: short | double | long
#
# DISPLAY COMMAND:
#   /IT8951/IT8951 0 0 "$OUT"   (set OUT before calling show_display)
#
# SIMULATION MODE:
#   Set SIMULATE=1 to echo display calls instead of driving the e-paper.
#   e.g.:  SIMULATE=1 bash pingpong.sh
#
# MQTT BROKER:
#   Set MQTT_HOST / MQTT_PORT if your broker differs from localhost:1883.
#
# DEPENDENCIES:
#   mosquitto_sub  (for MQTT)
#   convert        (ImageMagick, for dynamic BMP generation)
#   mkfifo         (bash built-in)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# 0.  CONFIGURATION  —  edit these to match your environment
# ---------------------------------------------------------------------------
SIMULATE="${SIMULATE:-0}"          # 1 = console mode, no real display
MQTT_HOST="${MQTT_HOST:-localhost}"
MQTT_PORT="${MQTT_PORT:-1883}"
BMP_DIR="${BMP_DIR:-/home/pi/pingpong/bmp}"   # where pre-drawn BMPs live
FONT="${FONT:-/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf}"
DISPLAY_CMD="/IT8951/IT8951 0 0"   # append "$OUT" to call it
DISPLAY_W=800
DISPLAY_H=600
LOG="${LOG:-/tmp/pingpong.log}"

# ---------------------------------------------------------------------------
# 1.  BMP FILENAME TEMPLATES
#     Pre-draw these in your art tool at 800x600 monochrome and place them
#     in $BMP_DIR.  The script will fall back to ImageMagick generation if
#     the file does not exist.
#
#     Static screens (create once):
#       bmp/rule_race11.bmp          "Press GREEN → race to 11 / BLUE → race to 21"
#       bmp/rule_race21.bmp          (same text, BLUE highlighted)
#       bmp/rule_bo3.bmp             "Press GREEN → Best of 3 / BLUE → Best of 5"
#       bmp/rule_bo5.bmp
#       bmp/confirm_rules.bmp        "Both press SHORT to confirm  |  DOUBLE to go back"
#       bmp/match_over_p1.bmp        "PLAYER 1 WINS THE MATCH!"
#       bmp/match_over_p2.bmp        "PLAYER 2 WINS THE MATCH!"
#
#     Dynamic scoring screens are generated at runtime via ImageMagick.
#     Naming convention (auto-generated):
#       bmp/score_<L>s<R>.bmp        L/R score, 's' marks server side (l or r)
#                                    e.g. score_3sl_9r.bmp = left serving, 3-9
#       bmp/win_confirm_p<N>.bmp     end-of-game win confirmation for player N
# ---------------------------------------------------------------------------

IMG_RULE_SELECT="$BMP_DIR/rule_race_select.bmp"
IMG_RULE_BO_SELECT="$BMP_DIR/rule_bo_select.bmp"
IMG_CONFIRM_RULES="$BMP_DIR/confirm_rules.bmp"
IMG_MATCH_OVER_P1="$BMP_DIR/match_over_p1.bmp"
IMG_MATCH_OVER_P2="$BMP_DIR/match_over_p2.bmp"

# ---------------------------------------------------------------------------
# 2.  DISPLAY FUNCTION
#     All rendering passes through here.  To swap artwork replace the file
#     referenced by $OUT before state transitions, or drop pre-drawn BMPs
#     into $BMP_DIR with the expected names.
# ---------------------------------------------------------------------------
show_display() {
    local img="$1"
    if [[ "$SIMULATE" == "1" ]]; then
        echo "[DISPLAY] $img" | tee -a "$LOG"
    else
        if [[ ! -f "$img" ]]; then
            echo "WARNING: BMP not found: $img" | tee -a "$LOG"
            return
        fi
        OUT="$img"
        $DISPLAY_CMD "$OUT" 2>>"$LOG" || echo "Display error for $img" >>"$LOG"
    fi
}

# ---------------------------------------------------------------------------
# 3.  IMAGE GENERATION  (ImageMagick fallback)
#     These functions generate minimal text-based BMPs when no pre-drawn
#     artwork exists.  Replace the convert calls with your own art pipeline.
# ---------------------------------------------------------------------------
mkdir -p "$BMP_DIR"

make_text_bmp() {
    # make_text_bmp <output.bmp> <line1> [line2] [line3] [line4]
    local out="$1"; shift
    local lines=("$@")
    local cmd=(convert -size "${DISPLAY_W}x${DISPLAY_H}" xc:white
               -font "$FONT" -fill black)
    local y=160
    for line in "${lines[@]}"; do
        cmd+=(-pointsize 48 -annotate "+60+${y}" "$line")
        (( y += 80 ))
    done
    cmd+=(-type Grayscale -depth 1 BMP:"$out")
    "${cmd[@]}" 2>>"$LOG"
}

make_score_bmp() {
    # make_score_bmp <score_left> <score_right> <server: left|right>
    #                <games_p1> <games_p2>
    local sl=$1 sr=$2 server=$3 gp1=$4 gp2=$5
    local tag
    if [[ "$server" == "left" ]]; then
        tag="score_${sl}sl_${sr}r"
    else
        tag="score_${sl}l_${sr}sr"
    fi
    local out="$BMP_DIR/${tag}.bmp"
    [[ -f "$out" ]] && { echo "$out"; return; }

    # Serve indicator: ● next to serving score
    local left_str="$sl" right_str="$sr"
    [[ "$server" == "left"  ]] && left_str="●$sl"
    [[ "$server" == "right" ]] && right_str="${sr}●"

    # Games won row
    local games_str="Games: P1=$gp1  P2=$gp2"

    make_text_bmp "$out" \
        "$games_str" \
        "" \
        "   $left_str    —    $right_str" \
        "" \
        "LEFT player        RIGHT player"

    # -----------------------------------------------------------------------
    # ARTWORK HOOK:  place a hand-drawn BMP at $out to replace this screen.
    # Naming:  bmp/score_<left>sl_<right>r.bmp  (left serving)
    #          bmp/score_<left>l_<right>sr.bmp  (right serving)
    # -----------------------------------------------------------------------
    echo "$out"
}

make_win_confirm_bmp() {
    # make_win_confirm_bmp <winner_label> <sl> <sr>
    local winner="$1" sl="$2" sr="$3"
    local out="$BMP_DIR/win_confirm_${winner// /_}.bmp"
    [[ -f "$out" ]] && { echo "$out"; return; }
    make_text_bmp "$out" \
        "$winner WINS?" \
        "Score: $sl - $sr" \
        "" \
        "Both SHORT press to confirm" \
        "DOUBLE press to undo"
    # ARTWORK HOOK:  bmp/win_confirm_<winner>.bmp
    echo "$out"
}

make_match_over_bmp() {
    local winner="$1"
    local out="$BMP_DIR/match_over_${winner// /_}.bmp"
    [[ -f "$out" ]] && { echo "$out"; return; }
    make_text_bmp "$out" \
        "🏆  $winner" \
        "WINS THE MATCH!" \
        "" \
        "LONG press to play again"
    # ARTWORK HOOK:  bmp/match_over_<winner>.bmp
    echo "$out"
}

# ---------------------------------------------------------------------------
# 4.  STATE VARIABLES
#     All game state lives here.  No external files except the undo stack.
# ---------------------------------------------------------------------------

# --- Match config ---
RACE_TO=11          # 11 or 21
BEST_OF=3           # 3 or 5
GAMES_TO_WIN=2      # ceil(BEST_OF/2), set after config

# --- Player <-> Button mapping ---
# Players are abstract: Player 1 and Player 2.
# Buttons are physical: green and blue.
# Each game, sides swap; track which physical button = which logical player.
# LEFT side of display = the player physically on the left.
# BUTTON_GREEN_IS_LEFT=1 means the green button belongs to the left-side player.
BUTTON_GREEN_IS_LEFT=1   # green starts on the left; flips each game

# Persistent player numbers (never change across games)
# P1 is always Player 1, P2 always Player 2.
# At start of each game we determine which button maps to which player.
# green_player and blue_player are set in init_game().
GREEN_PLAYER=1
BLUE_PLAYER=2

# --- Scores ---
SCORE_LEFT=0
SCORE_RIGHT=0
GAMES_P1=0
GAMES_P2=0

# --- Serve tracking ---
SERVER="left"        # "left" or "right"
SERVE_COUNT=0        # serves taken by current server this streak
SERVES_PER_TURN=2    # changes to 1 when both reach RACE_TO-1

# --- Game state machine ---
# States:
#   RULE_RACE      – waiting for race-to selection
#   RULE_BO        – waiting for best-of selection
#   CONFIRM_RULES  – waiting for both players to confirm rules
#   PLAYING        – active game
#   WIN_CONFIRM    – end-of-game, waiting for both to confirm
#   MATCH_OVER     – match finished
STATE="RULE_RACE"

# --- Confirmation flags (bitfield via variables) ---
CONFIRM_GREEN=0
CONFIRM_BLUE=0

# --- Winner of pending game (used during WIN_CONFIRM) ---
PENDING_WINNER=""    # "left" or "right"

# --- Winner of last completed game (for next-game server assignment) ---
LAST_GAME_WINNER=""  # "P1" or "P2"

# --- Undo stack (array of snapshots) ---
# Each entry is a colon-separated string:
#   SCORE_LEFT:SCORE_RIGHT:SERVER:SERVE_COUNT:SERVES_PER_TURN
declare -a UNDO_STACK=()
UNDO_MAX=20   # keep at most this many undo levels

push_undo() {
    local snap="${SCORE_LEFT}:${SCORE_RIGHT}:${SERVER}:${SERVE_COUNT}:${SERVES_PER_TURN}"
    UNDO_STACK+=("$snap")
    if (( ${#UNDO_STACK[@]} > UNDO_MAX )); then
        UNDO_STACK=("${UNDO_STACK[@]:1}")  # drop oldest
    fi
}

pop_undo() {
    local n=${#UNDO_STACK[@]}
    if (( n == 0 )); then
        echo "Nothing to undo." | tee -a "$LOG"
        return 1
    fi
    local snap="${UNDO_STACK[$((n-1))]}"
    UNDO_STACK=("${UNDO_STACK[@]:0:$((n-1))}")
    IFS=: read -r SCORE_LEFT SCORE_RIGHT SERVER SERVE_COUNT SERVES_PER_TURN <<< "$snap"
    return 0
}

# ---------------------------------------------------------------------------
# 5.  HELPER FUNCTIONS
# ---------------------------------------------------------------------------

# Map "left"/"right" to logical player number
side_to_player() {
    # Returns 1 or 2
    local side="$1"
    if [[ "$side" == "left" ]]; then
        (( BUTTON_GREEN_IS_LEFT )) && echo "$GREEN_PLAYER" || echo "$BLUE_PLAYER"
    else
        (( BUTTON_GREEN_IS_LEFT )) && echo "$BLUE_PLAYER" || echo "$GREEN_PLAYER"
    fi
}

# Which side does a button correspond to?
green_side() { (( BUTTON_GREEN_IS_LEFT )) && echo "left" || echo "right"; }
blue_side()  { (( BUTTON_GREEN_IS_LEFT )) && echo "right" || echo "left"; }

check_game_win() {
    # Returns 0 (true) and sets PENDING_WINNER if someone won; else returns 1
    local effective_target=$RACE_TO

    # Win-by-2: if both are at target-1, keep playing until 2 ahead
    local need_deucing=0
    if (( SCORE_LEFT >= RACE_TO - 1 && SCORE_RIGHT >= RACE_TO - 1 )); then
        need_deucing=1
    fi

    if (( need_deucing )); then
        local diff=$(( SCORE_LEFT - SCORE_RIGHT ))
        if (( diff >= 2 )); then
            PENDING_WINNER="left"; return 0
        elif (( diff <= -2 )); then
            PENDING_WINNER="right"; return 0
        fi
    else
        if (( SCORE_LEFT >= RACE_TO )); then
            PENDING_WINNER="left"; return 0
        elif (( SCORE_RIGHT >= RACE_TO )); then
            PENDING_WINNER="right"; return 0
        fi
    fi
    return 1
}

update_serve() {
    # Called after a point is scored (before checking win).
    # Serve changes every SERVES_PER_TURN serves.
    # At deuce (both RACE_TO-1) switch to 1 serve per turn.
    if (( SCORE_LEFT >= RACE_TO - 1 && SCORE_RIGHT >= RACE_TO - 1 )); then
        SERVES_PER_TURN=1
    fi
    (( SERVE_COUNT++ ))
    if (( SERVE_COUNT >= SERVES_PER_TURN )); then
        SERVE_COUNT=0
        [[ "$SERVER" == "left" ]] && SERVER="right" || SERVER="left"
    fi
}

render() {
    # Central render dispatcher — called after EVERY state change.
    # Add new states here as the game grows.
    case "$STATE" in
        RULE_RACE)
            # ARTWORK: pre-draw bmp/rule_race_select.bmp
            local img="$IMG_RULE_SELECT"
            if [[ ! -f "$img" ]]; then
                make_text_bmp "$img" \
                    "Select race length:" \
                    "" \
                    "  GREEN  →  Race to 11" \
                    "  BLUE   →  Race to 21"
            fi
            show_display "$img"
            ;;
        RULE_BO)
            local img="$IMG_RULE_BO_SELECT"
            if [[ ! -f "$img" ]]; then
                make_text_bmp "$img" \
                    "Select match length:" \
                    "" \
                    "  GREEN  →  Best of 3" \
                    "  BLUE   →  Best of 5"
            fi
            show_display "$img"
            ;;
        CONFIRM_RULES)
            local img="$IMG_CONFIRM_RULES"
            if [[ ! -f "$img" ]]; then
                make_text_bmp "$img" \
                    "Rules: Race to $RACE_TO  |  Best of $BEST_OF" \
                    "" \
                    "Both players: SHORT press to confirm" \
                    "Any player:   DOUBLE press to go back" \
                    "" \
                    "Confirmed: ${CONFIRM_GREEN:+GREEN }${CONFIRM_BLUE:+BLUE}"
            fi
            # Re-generate each time (confirm state changes)
            make_text_bmp "$img" \
                "Rules: Race to $RACE_TO  |  Best of $BEST_OF" \
                "" \
                "Both SHORT press to confirm  |  DOUBLE to go back" \
                "" \
                "Confirmed: GREEN=$CONFIRM_GREEN  BLUE=$CONFIRM_BLUE"
            show_display "$img"
            ;;
        PLAYING)
            local img
            img=$(make_score_bmp "$SCORE_LEFT" "$SCORE_RIGHT" "$SERVER" "$GAMES_P1" "$GAMES_P2")
            show_display "$img"
            ;;
        WIN_CONFIRM)
            local wp
            wp=$(side_to_player "$PENDING_WINNER")
            local img
            img=$(make_win_confirm_bmp "Player $wp" "$SCORE_LEFT" "$SCORE_RIGHT")
            show_display "$img"
            ;;
        MATCH_OVER)
            local img
            img=$(make_match_over_bmp "Player $PENDING_WINNER")
            show_display "$img"
            ;;
    esac
}

# ---------------------------------------------------------------------------
# 6.  GAME / MATCH INITIALISATION
# ---------------------------------------------------------------------------

init_match() {
    # Called at very start or after long-press reset.
    SCORE_LEFT=0; SCORE_RIGHT=0
    GAMES_P1=0;   GAMES_P2=0
    SERVE_COUNT=0; SERVER="left"; SERVES_PER_TURN=2
    UNDO_STACK=()
    CONFIRM_GREEN=0; CONFIRM_BLUE=0
    PENDING_WINNER=""
    LAST_GAME_WINNER=""
    BUTTON_GREEN_IS_LEFT=1   # green starts on left; reset to default
    GREEN_PLAYER=1; BLUE_PLAYER=2
    RACE_TO=11; BEST_OF=3; GAMES_TO_WIN=2
    STATE="RULE_RACE"
    render
}

init_game() {
    # Called at the start of each game (after first-game config OR after confirmation).
    # Sides swap each game.
    SCORE_LEFT=0; SCORE_RIGHT=0
    SERVE_COUNT=0; SERVES_PER_TURN=2
    UNDO_STACK=()
    CONFIRM_GREEN=0; CONFIRM_BLUE=0
    PENDING_WINNER=""

    # Assign server: winner of last game serves first.
    # Map last-game winner (P1/P2) to current left/right.
    if [[ -n "$LAST_GAME_WINNER" ]]; then
        local left_player
        left_player=$(side_to_player "left")
        if [[ "$LAST_GAME_WINNER" == "P$left_player" ]]; then
            SERVER="left"
        else
            SERVER="right"
        fi
    else
        SERVER="left"   # first game default
    fi

    STATE="PLAYING"
    render
}

swap_sides() {
    # Called between games.  Toggle which button = which side.
    if (( BUTTON_GREEN_IS_LEFT )); then
        BUTTON_GREEN_IS_LEFT=0
    else
        BUTTON_GREEN_IS_LEFT=1
    fi
}

# ---------------------------------------------------------------------------
# 7.  EVENT HANDLERS  (one function per meaningful button+payload combo)
# ---------------------------------------------------------------------------

handle_rule_race() {
    local button="$1" payload="$2"
    case "$payload" in
        short)
            [[ "$button" == "green" ]] && RACE_TO=11 || RACE_TO=21
            STATE="RULE_BO"
            render
            ;;
        # long or double in RULE_RACE: ignore (nothing to go back to)
    esac
}

handle_rule_bo() {
    local button="$1" payload="$2"
    case "$payload" in
        short)
            if [[ "$button" == "green" ]]; then
                BEST_OF=3; GAMES_TO_WIN=2
            else
                BEST_OF=5; GAMES_TO_WIN=3
            fi
            CONFIRM_GREEN=0; CONFIRM_BLUE=0
            STATE="CONFIRM_RULES"
            render
            ;;
        double)
            # Go back to race selection
            STATE="RULE_RACE"
            render
            ;;
    esac
}

handle_confirm_rules() {
    local button="$1" payload="$2"
    case "$payload" in
        short)
            [[ "$button" == "green" ]] && CONFIRM_GREEN=1 || CONFIRM_BLUE=1
            if (( CONFIRM_GREEN && CONFIRM_BLUE )); then
                # Both confirmed — start first game
                init_game
            else
                render   # show updated confirm status
            fi
            ;;
        double)
            # Any player double-press → back to rule selection
            CONFIRM_GREEN=0; CONFIRM_BLUE=0
            STATE="RULE_RACE"
            render
            ;;
    esac
}

handle_playing() {
    local button="$1" payload="$2"
    case "$payload" in
        short)
            # Score point for the player whose button was pressed.
            local side
            [[ "$button" == "green" ]] && side=$(green_side) || side=$(blue_side)

            push_undo

            if [[ "$side" == "left" ]]; then
                (( SCORE_LEFT++ ))
            else
                (( SCORE_RIGHT++ ))
            fi

            update_serve

            if check_game_win; then
                # Freeze scoring, show confirmation screen
                STATE="WIN_CONFIRM"
                CONFIRM_GREEN=0; CONFIRM_BLUE=0
                render
            else
                render
            fi
            ;;
        double)
            # Undo last point
            if pop_undo; then
                STATE="PLAYING"   # in case we were briefly in WIN_CONFIRM
                render
            fi
            ;;
        long)
            # Full match reset
            init_match
            ;;
    esac
}

handle_win_confirm() {
    local button="$1" payload="$2"
    case "$payload" in
        short)
            [[ "$button" == "green" ]] && CONFIRM_GREEN=1 || CONFIRM_BLUE=1
            if (( CONFIRM_GREEN && CONFIRM_BLUE )); then
                # Game confirmed — record result
                local winner_player
                winner_player=$(side_to_player "$PENDING_WINNER")
                LAST_GAME_WINNER="P${winner_player}"

                if [[ "$winner_player" == "1" ]]; then
                    (( GAMES_P1++ ))
                else
                    (( GAMES_P2++ ))
                fi

                # Check match win
                if (( GAMES_P1 >= GAMES_TO_WIN || GAMES_P2 >= GAMES_TO_WIN )); then
                    PENDING_WINNER="Player $winner_player"
                    STATE="MATCH_OVER"
                    render
                else
                    # Swap sides and start next game
                    swap_sides
                    init_game
                fi
            else
                render
            fi
            ;;
        double)
            # Undo last point (revert game-win state)
            if pop_undo; then
                STATE="PLAYING"
                PENDING_WINNER=""
                CONFIRM_GREEN=0; CONFIRM_BLUE=0
                render
            fi
            ;;
        long)
            init_match
            ;;
    esac
}

handle_match_over() {
    local button="$1" payload="$2"
    case "$payload" in
        long)
            # Long press from either button → new match
            init_match
            ;;
        # Any short/double in MATCH_OVER: ignored (could add rematch on double)
    esac
}

# ---------------------------------------------------------------------------
# 8.  CENTRAL BUTTON DISPATCHER
# ---------------------------------------------------------------------------

dispatch() {
    local button="$1"   # green | blue
    local payload="$2"  # short | double | long

    echo "[EVENT] state=$STATE  button=$button  payload=$payload" | tee -a "$LOG"

    case "$STATE" in
        RULE_RACE)     handle_rule_race    "$button" "$payload" ;;
        RULE_BO)       handle_rule_bo      "$button" "$payload" ;;
        CONFIRM_RULES) handle_confirm_rules "$button" "$payload" ;;
        PLAYING)       handle_playing      "$button" "$payload" ;;
        WIN_CONFIRM)   handle_win_confirm  "$button" "$payload" ;;
        MATCH_OVER)    handle_match_over   "$button" "$payload" ;;
        *)             echo "Unknown state: $STATE" | tee -a "$LOG" ;;
    esac
}

# ---------------------------------------------------------------------------
# 9.  MQTT EVENT LOOP
#     mosquitto_sub emits one line per message in the format:
#       button/green short
#       button/blue  long
#     We parse topic → button name and payload.
# ---------------------------------------------------------------------------

mqtt_loop() {
    echo "Starting MQTT listener on $MQTT_HOST:$MQTT_PORT …" | tee -a "$LOG"

    # Subscribe to both topics; -v prints topic before payload on same line
    mosquitto_sub \
        -h "$MQTT_HOST" \
        -p "$MQTT_PORT" \
        -t "button/green" \
        -t "button/blue" \
        -v \
    | while IFS=' ' read -r topic payload; do
        # Strip leading/trailing whitespace from payload
        payload="${payload//[$'\t\r\n ']}"

        case "$topic" in
            button/green) dispatch "green" "$payload" ;;
            button/blue)  dispatch "blue"  "$payload" ;;
            *)            echo "Unknown topic: $topic" | tee -a "$LOG" ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# 10. SIMULATION / TESTING MODE
#     When SIMULATE=1, reads button events from stdin instead of MQTT.
#     Format:  green short   /  blue double  /  green long   (one per line)
#     Useful for testing on a laptop without hardware.
# ---------------------------------------------------------------------------

simulate_loop() {
    echo "=== SIMULATION MODE ==="
    echo "Enter events as:  <green|blue> <short|double|long>"
    echo "Type 'quit' to exit."
    echo ""
    while IFS=' ' read -r button payload; do
        [[ "$button" == "quit" ]] && break
        [[ -z "$button" || -z "$payload" ]] && continue
        dispatch "$button" "$payload"
    done
}

# ---------------------------------------------------------------------------
# 11. ENTRY POINT
# ---------------------------------------------------------------------------

main() {
    echo "=== Ping-Pong Scoring System starting ===" | tee -a "$LOG"
    mkdir -p "$BMP_DIR"

    init_match   # draw initial screen

    if [[ "$SIMULATE" == "1" ]]; then
        simulate_loop
    else
        mqtt_loop
    fi
}

main "$@"
