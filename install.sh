#!/usr/bin/env bash
# ================================================================
#  CopyShop PDF Suite v3 — Installer
#  Unterstützte Distributionen:
#    Arch / CachyOS / Manjaro / EndeavourOS
#    Ubuntu / Debian / Mint / Pop!_OS
#    Fedora / RHEL / Rocky / AlmaLinux
#    openSUSE Leap / Tumbleweed
#
#  Ausführen mit:  bash install.sh
# ================================================================
set -euo pipefail

# ── Farben & Ausgabe ─────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓  ${NC}$1"; }
info() { echo -e "${BLUE}  →  ${NC}$1"; }
warn() { echo -e "${YELLOW}  !  ${NC}$1"; }
fail() { echo -e "${RED}  ✗  ${NC}$1"; exit 1; }
step() { echo ""; echo -e "${BOLD}[$1] $2${NC}"; echo ""; }

# ── Pfade ────────────────────────────────────────────────────────
INSTALL_DIR="$HOME/.local/share/copyshop_pdf_suite"
VENV_DIR="$INSTALL_DIR/venv"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/copyshop-pdf"
DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$DESKTOP_DIR/copyshop-pdf-suite.desktop"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SRC="$SCRIPT_DIR"

# Verify we're in the right place
[ -f "$APP_SRC/main.py" ] || \
    fail "main.py nicht gefunden. Installer aus dem App-Verzeichnis ausführen."

# ── Banner ───────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║      CopyShop PDF Suite v3  —  Installer         ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""

# ── Update-Erkennung ─────────────────────────────────────────────
if [ -f "$INSTALL_DIR/main.py" ]; then
    echo -e "  ${YELLOW}CopyShop PDF Suite ist bereits installiert.${NC}"
    read -rp "  Jetzt aktualisieren / neu installieren? [J/n] " REPLY
    REPLY="${REPLY:-J}"
    [[ "$REPLY" =~ ^[JjYy]$ ]] || { echo "  Abgebrochen."; exit 0; }
    echo ""
fi

# ================================================================
#  [1/5]  Distro-Erkennung & System-Pakete
# ================================================================
step "1/5" "System-Pakete installieren"

# Read /etc/os-release (standard on all modern distros)
detect_family() {
    if [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        local combined="${ID_LIKE:-} ${ID:-}"
        case "$combined" in
            *arch*)                              echo "arch"   ;;
            *debian*|*ubuntu*)                   echo "debian" ;;
            *fedora*|*rhel*|*centos*|*rocky*|*alma*) echo "fedora" ;;
            *suse*|*opensuse*)                   echo "suse"   ;;
            *)
                # Fallback: check which package manager exists
                command -v pacman &>/dev/null && echo "arch"   && return
                command -v apt    &>/dev/null && echo "debian" && return
                command -v dnf    &>/dev/null && echo "fedora" && return
                command -v zypper &>/dev/null && echo "suse"   && return
                echo "unknown"
                ;;
        esac
    else
        command -v pacman &>/dev/null && echo "arch"   && return
        command -v apt    &>/dev/null && echo "debian" && return
        command -v dnf    &>/dev/null && echo "fedora" && return
        command -v zypper &>/dev/null && echo "suse"   && return
        echo "unknown"
    fi
}

DISTRO="$(detect_family)"
info "Erkannte Distribution: ${DISTRO}"

# Helper: check if a package is installed
pkg_installed_arch()   { pacman -Qi "$1" &>/dev/null 2>&1; }
pkg_installed_debian() { dpkg -s "$1" &>/dev/null 2>&1; }
pkg_installed_rpm()    { rpm -q "$1" &>/dev/null 2>&1; }

install_missing_arch() {
    local pkgs=("$@") missing=()
    for p in "${pkgs[@]}"; do
        pkg_installed_arch "$p" || missing+=("$p")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        info "Installiere: ${missing[*]}"
        # -Syu, NICHT -Sy: ein reines "-Sy" (Datenbank aktualisieren ohne
        # System-Upgrade) erzeugt auf Arch einen "partial upgrade" und kann
        # durch nicht zusammenpassende Bibliotheken das System beschädigen.
        # -Syu hält die Pakete konsistent.
        sudo pacman -Syu --needed --noconfirm "${missing[@]}"
    fi
    for p in "${pkgs[@]}"; do ok "$p"; done
}

install_missing_debian() {
    local pkgs=("$@") missing=()
    for p in "${pkgs[@]}"; do
        pkg_installed_debian "$p" || missing+=("$p")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        info "Installiere: ${missing[*]}"
        sudo apt-get install -y -q "${missing[@]}"
    fi
    for p in "${pkgs[@]}"; do ok "$p"; done
}

install_missing_rpm() {
    local mgr="$1"; shift
    local pkgs=("$@") missing=()
    for p in "${pkgs[@]}"; do
        pkg_installed_rpm "$p" || missing+=("$p")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        info "Installiere: ${missing[*]}"
        sudo "$mgr" install -y -q "${missing[@]}"
    fi
    for p in "${pkgs[@]}"; do ok "$p"; done
}

case "$DISTRO" in
    arch)
        install_missing_arch \
            python python-pip \
            ghostscript cups \
            tesseract tesseract-data-eng tesseract-data-deu \
            poppler
        ;;
    debian)
        info "apt update..."
        sudo apt-get update -qq
        install_missing_debian \
            python3 python3-venv python3-dev python3-pip \
            ghostscript cups \
            tesseract-ocr tesseract-ocr-eng tesseract-ocr-deu \
            poppler-utils \
            libgl1 libglib2.0-0
        ;;
    fedora)
        install_missing_rpm dnf \
            python3 python3-pip python3-devel \
            ghostscript cups \
            tesseract tesseract-langpack-eng tesseract-langpack-deu \
            poppler-utils mesa-libGL
        ;;
    suse)
        install_missing_rpm zypper \
            python3 python3-pip python3-devel \
            ghostscript cups \
            tesseract-ocr \
            tesseract-ocr-traineddata-english \
            tesseract-ocr-traineddata-german \
            poppler-tools libGL1
        ;;
    unknown)
        warn "Unbekannte Distribution."
        warn "Bitte manuell installieren: python3 ghostscript cups tesseract poppler"
        ;;
esac

# ================================================================
#  [2/5]  Virtualenv & Python-Pakete
# ================================================================
step "2/5" "Python-Umgebung einrichten (virtualenv)"

# Find the best available Python 3.
# Prefer wheel-friendly stable versions (newest stable first) over the very
# newest release: bleeding-edge Pythons (e.g. 3.14) often have NO prebuilt
# wheels yet, forcing slow source builds of cryptography/pikepdf/lxml that look
# like the installer has frozen. Fall back to 3.14 / generic python3 only if
# none of the wheel-friendly versions are present.
PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3.10 python3.14 python3; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON="$(command -v "$cmd")"
        break
    fi
done
[ -z "$PYTHON" ] && fail "Python 3 nicht gefunden. Bitte manuell installieren."
PYVER="$("$PYTHON" --version 2>&1)"
ok "Python: $PYVER"

# Create the virtualenv. Always build it cleanly with --clear: running
# `python -m venv` over an EXISTING venv only adds the new interpreter's symlink
# and rewrites pyvenv.cfg — it leaves the old python/python3 links and the old
# site-packages in place. If the chosen Python differs from the one that built
# the existing venv (e.g. switching off 3.14), that produces a corrupted
# mixed-version venv where the launcher still runs the old interpreter.
# --clear wipes the env first so it is always rebuilt on $PYTHON.
mkdir -p "$INSTALL_DIR"
if [ -d "$VENV_DIR" ]; then
    info "Vorhandene virtualenv wird sauber neu erstellt..."
else
    info "Erstelle virtualenv in $VENV_DIR ..."
fi
"$PYTHON" -m venv --clear "$VENV_DIR"
ok "Virtualenv bereit ($("$VENV_DIR/bin/python" --version 2>&1))"

info "Aktualisiere pip und wheel..."
"$VENV_DIR/bin/pip" install --upgrade --progress-bar on pip wheel

# All Python dependencies go into the venv — never touches system Python
PIP_PACKAGES=(
    PyQt6
    pypdf
    pikepdf
    Pillow
    pypdfium2
    reportlab
    img2pdf
    pdf2image
    pytesseract
    ocrmypdf
)

info "Installiere Python-Pakete (beim ersten Mal etwas länger)..."
warn "Verwendeter Python: $PYVER"
warn "Sehr neue Python-Versionen haben evtl. noch keine fertigen Wheels —"
warn "in dem Fall wird aus dem Quellcode gebaut (dauert länger, braucht Compiler)."
for p in "${PIP_PACKAGES[@]}"; do
    info "pip install $p ..."
    # Guard: try prebuilt wheels first so we never SILENTLY drop into a long
    # source build when the chosen Python has no matching wheel.
    if "$VENV_DIR/bin/pip" install --progress-bar on --only-binary=:all: "$p"; then
        ok "$p (wheel)"
    else
        warn "Kein passendes Wheel für $p unter $PYVER — baue aus Quellcode (kann mehrere Minuten dauern)..."
        "$VENV_DIR/bin/pip" install --progress-bar on "$p"
        ok "$p (aus Quellcode gebaut)"
    fi
done

# ================================================================
#  [3/5]  App-Dateien installieren
# ================================================================
step "3/5" "App-Dateien installieren"

info "Kopiere nach $INSTALL_DIR ..."

# Prefer rsync (preserves permissions, skips unchanged files on update)
if command -v rsync &>/dev/null; then
    rsync -a --delete \
        --exclude '__pycache__/' \
        --exclude '*.pyc' \
        --exclude '.git/' \
        --exclude 'venv/' \
        "$APP_SRC/" "$INSTALL_DIR/"
else
    cp -r "$APP_SRC/." "$INSTALL_DIR/"
    find "$INSTALL_DIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
    find "$INSTALL_DIR" -name '*.pyc' -delete 2>/dev/null || true
fi

# Ensure plugins directory exists
mkdir -p "$INSTALL_DIR/plugins"
ok "App-Dateien kopiert"

# ================================================================
#  [4/5]  Launcher, Icon & Desktop-Integration
# ================================================================
step "4/5" "Starter und Desktop-Symbol einrichten"

# Launcher script
mkdir -p "$BIN_DIR"
cat > "$LAUNCHER" << LAUNCHER_SCRIPT
#!/usr/bin/env bash
# CopyShop PDF Suite — Launcher (generiert von install.sh)
exec "$VENV_DIR/bin/python" "$INSTALL_DIR/main.py" "\$@"
LAUNCHER_SCRIPT
chmod +x "$LAUNCHER"
ok "Launcher: $LAUNCHER"

# Ensure ~/.local/bin is in PATH
if [[ ":${PATH}:" != *":$BIN_DIR:"* ]]; then
    for rcfile in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
        if [ -f "$rcfile" ]; then
            echo '' >> "$rcfile"
            echo '# CopyShop: add ~/.local/bin to PATH' >> "$rcfile"
            echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rcfile"
        fi
    done
    warn "~/.local/bin zum PATH hinzugefügt. Terminal neu starten oder: source ~/.bashrc"
fi

# Generate app icon using Pillow (in the venv)
info "Erstelle App-Icon..."
ICON_PATH="$INSTALL_DIR/icon.png"
INSTALL_DIR_ESC="$INSTALL_DIR"   # captured for the heredoc below
"$VENV_DIR/bin/python" - "$ICON_PATH" << 'ICON_PY' || warn "Icon-Generierung fehlgeschlagen (kein Fehler im Betrieb)"
import sys, os
try:
    from PIL import Image, ImageDraw, ImageFont
    path = sys.argv[1]
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 252, 252], radius=40, fill="#e94560")
    # Smaller red rectangle for depth
    d.rounded_rectangle([24, 24, 232, 232], radius=30, fill="#c73652")
    # White "CS" lettering
    font = None
    for fpath in [
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    ]:
        if os.path.exists(fpath):
            font = ImageFont.truetype(fpath, 100)
            break
    if font is None:
        font = ImageFont.load_default()
    d.text((128, 128), "CS", fill="white", font=font, anchor="mm")
    img.save(path)
    print(f"  → {path}")
except Exception as e:
    print(f"  skipped: {e}")
    sys.exit(1)
ICON_PY

if [ -f "$ICON_PATH" ]; then
    ICON_FIELD="$ICON_PATH"
    ok "Icon erstellt: $ICON_PATH"
else
    ICON_FIELD="application-pdf"
    warn "Kein benutzerdefiniertes Icon — verwende System-Icon"
fi

# Desktop entry
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_FILE" << DESKTOP_ENTRY
[Desktop Entry]
Version=1.0
Type=Application
Name=CopyShop PDF Suite
GenericName=PDF-Editor
Comment=Professionelles PDF-Werkzeug für Copyshops und Druckvorstufe
Exec=$LAUNCHER %F
Icon=$ICON_FIELD
Terminal=false
Categories=Office;Graphics;
MimeType=application/pdf;
StartupNotify=true
Keywords=PDF;Drucken;Copyshop;Druckvorstufe;CMYK;
DESKTOP_ENTRY
chmod +x "$DESKTOP_FILE"
ok "App-Menü-Eintrag erstellt"

# Desktop shortcut (tries German and English folder names)
for DESK in "$HOME/Desktop" "$HOME/Schreibtisch"; do
    if [ -d "$DESK" ]; then
        cp "$DESKTOP_FILE" "$DESK/copyshop-pdf-suite.desktop"
        chmod +x "$DESK/copyshop-pdf-suite.desktop"
        # Mark as trusted on KDE/GNOME so the icon is clickable immediately
        gio set "$DESK/copyshop-pdf-suite.desktop" \
            metadata::trusted true 2>/dev/null || true
        ok "Desktop-Symbol erstellt: $DESK/"
        break
    fi
done

# Refresh desktop database so the app appears in the app menu immediately
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

# ================================================================
#  [5/5]  Installationstest
# ================================================================
step "5/5" "Installation überprüfen"

# Test all Python imports
"$VENV_DIR/bin/python" << 'PYCHECK'
import sys
checks = [
    ("PyQt6.QtWidgets", "Qt6 GUI"),
    ("pypdf",           "pypdf"),
    ("pikepdf",         "pikepdf"),
    ("PIL",             "Pillow"),
    ("pypdfium2",       "pypdfium2"),
    ("reportlab",       "reportlab"),
    ("img2pdf",         "img2pdf"),
    ("pdf2image",       "pdf2image"),
    ("pytesseract",     "pytesseract"),
    ("ocrmypdf",        "ocrmypdf"),
]
failed = False
for mod, label in checks:
    try:
        __import__(mod)
        print(f"  \033[32m  ✓  \033[0m{label}")
    except Exception as e:
        print(f"  \033[31m  ✗  \033[0m{label}: {e}")
        failed = True
sys.exit(1 if failed else 0)
PYCHECK

# Check external tools
for tool in gs tesseract lp; do
    if command -v "$tool" &>/dev/null; then
        ok "$tool $(command -v $tool)"
    else
        case "$tool" in
            gs)         warn "ghostscript nicht gefunden — GS-Drucknormalisierung nicht verfügbar" ;;
            tesseract)  warn "tesseract nicht gefunden — OCR nicht verfügbar" ;;
            lp)         warn "lp nicht gefunden — CUPS/Drucker-Unterstützung nicht aktiv
             Arch: sudo pacman -S cups && sudo systemctl enable --now cups
             Debian: sudo apt install cups && sudo systemctl enable --now cups" ;;
        esac
    fi
done

# ================================================================
#  Fertig
# ================================================================
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  ${GREEN}  Installation abgeschlossen!${NC}${BOLD}                 ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Terminal:     ${BOLD}copyshop-pdf${NC}"
echo -e "  App-Menü:     Office → CopyShop PDF Suite"
echo -e "  Desktop:      Doppelklick auf das Icon"
echo -e "  Datei öffnen: ${BOLD}copyshop-pdf datei.pdf${NC}"
echo ""

read -rp "  Jetzt starten? [J/n] " REPLY
REPLY="${REPLY:-J}"
if [[ "$REPLY" =~ ^[JjYy]$ ]]; then
    info "Starte CopyShop PDF Suite..."
    nohup "$LAUNCHER" &>/dev/null &
    ok "Gestartet!"
fi
echo ""
