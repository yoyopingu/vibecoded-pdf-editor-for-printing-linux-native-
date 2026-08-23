#!/usr/bin/env bash
# ================================================================
#  Deinstallation — the app name comes from tools/branding.py
#  Entfernt alle vom Installer (install.sh) angelegten Dateien.
#
#  System-Pakete (ghostscript, cups, tesseract, poppler, python)
#  werden NICHT entfernt — sie sind systemweit installiert und ggf.
#  von anderer Software genutzt.
#
#  Ausführen mit:  bash uninstall.sh        (fragt vor dem Löschen)
#              oder bash uninstall.sh -y     (ohne Rückfrage)
# ================================================================
# Bewusst KEIN "set -e": das Aufräumen soll auch dann weiterlaufen,
# wenn eine einzelne Datei bereits fehlt.
set -uo pipefail

# ── Farben & Ausgabe ─────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓  ${NC}$1"; }
info() { echo -e "${BLUE}  →  ${NC}$1"; }
warn() { echo -e "${YELLOW}  !  ${NC}$1"; }

# ── Pfade (müssen mit install.sh übereinstimmen) ─────────────────
INSTALL_DIR="$HOME/.local/share/copyshop_pdf_suite"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/copyshop-pdf"
DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$DESKTOP_DIR/copyshop-pdf-suite.desktop"
CONFIG_DIR="$HOME/.config/CopyShop"
LEGACY_LOG="$HOME/copyshop_crash.log"   # alter Log-Ort (vor der Umstellung)

DESKTOP_ICONS=(
    "$HOME/Desktop/copyshop-pdf-suite.desktop"
    "$HOME/Schreibtisch/copyshop-pdf-suite.desktop"
)
RC_FILES=("$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile")
PATH_MARKER="# CopyShop: add ~/.local/bin to PATH"

# Display name only — every path above stays as it is, because they are what
# the installed copy actually put on disk. See tools/branding.py.
_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="$(python3 -c "import sys; sys.path.insert(0, '$_SRC'); from tools.branding import APP_NAME; print(APP_NAME)" 2>/dev/null || echo "CopyShop PDF Suite")"
_banner() {
    local text="$1" width=50 len left right
    len=${#text}
    left=$(( (width - len) / 2 )); [ "$left" -lt 0 ] && left=0
    right=$(( width - len - left )); [ "$right" -lt 0 ] && right=0
    printf "${BOLD}║%*s%s%*s║${NC}\n" "$left" "" "$text" "$right" ""
}

# ── Banner ───────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
_banner "$APP_NAME v3  —  Deinstallation"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""

# ── Übersicht: was wird entfernt? ────────────────────────────────
found_any=0
echo "  Folgendes wird entfernt:"
for p in "$INSTALL_DIR" "$LAUNCHER" "$DESKTOP_FILE" "${DESKTOP_ICONS[@]}" \
         "$CONFIG_DIR" "$LEGACY_LOG"; do
    if [ -e "$p" ] || [ -L "$p" ]; then
        echo -e "    ${RED}✗${NC} $p"
        found_any=1
    fi
done
for rc in "${RC_FILES[@]}"; do
    if [ -f "$rc" ] && grep -qF "$PATH_MARKER" "$rc"; then
        echo -e "    ${RED}✗${NC} PATH-Eintrag in $rc"
        found_any=1
    fi
done

if [ "$found_any" -eq 0 ]; then
    echo ""
    ok "Nichts gefunden — ${APP_NAME} scheint bereits deinstalliert zu sein."
    exit 0
fi

echo ""
warn "System-Pakete (ghostscript, cups, tesseract, poppler, python) bleiben erhalten."
echo ""

# ── Bestätigung ──────────────────────────────────────────────────
ASSUME_YES=0
[[ "${1:-}" == "-y" || "${1:-}" == "--yes" ]] && ASSUME_YES=1
if [ "$ASSUME_YES" -eq 0 ]; then
    read -rp "  Wirklich deinstallieren? [j/N] " REPLY
    REPLY="${REPLY:-N}"
    [[ "$REPLY" =~ ^[JjYy]$ ]] || { echo "  Abgebrochen."; exit 0; }
fi
echo ""

# ── Laufende Instanz beenden ─────────────────────────────────────
if pkill -f "copyshop_pdf_suite/main.py" 2>/dev/null; then
    info "Laufende Instanz beendet."
    sleep 1
fi

# ── Dateien entfernen ────────────────────────────────────────────
remove_path() {
    local p="$1"
    if [ -e "$p" ] || [ -L "$p" ]; then
        if rm -rf "$p"; then
            ok "Entfernt: $p"
        else
            warn "Konnte nicht entfernen: $p"
        fi
    fi
}

remove_path "$INSTALL_DIR"
remove_path "$LAUNCHER"
remove_path "$DESKTOP_FILE"
for icon in "${DESKTOP_ICONS[@]}"; do remove_path "$icon"; done
remove_path "$CONFIG_DIR"
remove_path "$LEGACY_LOG"

# ── PATH-Einträge aus den Shell-RC-Dateien entfernen ─────────────
# Löscht die Markerzeile und die direkt folgende export-Zeile.
# Custom sed-Trenner '|', damit die Slashes im Marker nicht escaped
# werden müssen.
for rc in "${RC_FILES[@]}"; do
    if [ -f "$rc" ] && grep -qF "$PATH_MARKER" "$rc"; then
        if sed -i "\|$PATH_MARKER|{N;d;}" "$rc"; then
            ok "PATH-Eintrag entfernt aus: $rc"
        else
            warn "PATH-Eintrag in $rc bitte manuell entfernen."
        fi
    fi
done

# ── Desktop-Datenbank aktualisieren ──────────────────────────────
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

# ── Fertig ───────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  ${GREEN}Deinstallation abgeschlossen.${NC}${BOLD}                   ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Falls ${BOLD}~/.local/bin${NC} nur wegen ${APP_NAME} im PATH war, wirkt die"
echo -e "  Änderung nach einem Terminal-Neustart."
echo ""
