#!/bin/bash
# ============================================================
# MTO IB → Discord  |  Setup para servidor Linux (Ubuntu/Debian)
# Ejecutar como: bash setup.sh
# ============================================================

set -e

MTO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$MTO_DIR/venv"
SERVICE_NAME="mto-ib-discord"
IBC_DIR="/opt/ibc"
IBGW_DIR="/opt/ibgateway"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[MTO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

info "================================================="
info " MTO IB → Discord  |  Instalación"
info " Directorio: $MTO_DIR"
info "================================================="

# ── 1. Paquetes del sistema ────────────────────────────────
info "Actualizando sistema e instalando dependencias..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-pip python3-venv \
    default-jdk \
    xvfb \
    wget curl unzip \
    2>/dev/null

PYTHON=$(which python3)
PY_VERSION=$($PYTHON --version 2>&1)
info "Python: $PY_VERSION"
JAVA_VERSION=$(java -version 2>&1 | head -1)
info "Java: $JAVA_VERSION"

# ── 2. Entorno virtual Python ─────────────────────────────
info "Creando entorno virtual Python..."
$PYTHON -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r "$MTO_DIR/requirements.txt"
info "Dependencias Python instaladas correctamente"

# ── 3. IB Gateway ─────────────────────────────────────────
info "================================================="
info " Instalación de IB Gateway"
info "================================================="

if [ -d "$IBGW_DIR" ]; then
    warn "IB Gateway ya está instalado en $IBGW_DIR. Saltando."
else
    IBGW_INSTALLER="ibgateway-stable-standalone-linux-x64.sh"
    IBGW_URL="https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/$IBGW_INSTALLER"

    info "Descargando IB Gateway..."
    wget -q "$IBGW_URL" -O "/tmp/$IBGW_INSTALLER"
    chmod +x "/tmp/$IBGW_INSTALLER"

    info "Instalando IB Gateway en $IBGW_DIR..."
    sudo "/tmp/$IBGW_INSTALLER" -q -dir "$IBGW_DIR"
    rm "/tmp/$IBGW_INSTALLER"
    info "IB Gateway instalado"
fi

# ── 4. IBC (automatiza login de IB Gateway) ───────────────
info "================================================="
info " Instalación de IBC (auto-login de IB Gateway)"
info "================================================="

if [ -d "$IBC_DIR" ]; then
    warn "IBC ya está instalado en $IBC_DIR. Saltando."
else
    IBC_VERSION="3.18.0"
    IBC_ZIP="IBCLinux-$IBC_VERSION.zip"
    IBC_URL="https://github.com/IbcAlpha/IBC/releases/download/$IBC_VERSION/$IBC_ZIP"

    info "Descargando IBC $IBC_VERSION..."
    wget -q "$IBC_URL" -O "/tmp/$IBC_ZIP"
    sudo mkdir -p "$IBC_DIR"
    sudo unzip -q "/tmp/$IBC_ZIP" -d "$IBC_DIR"
    sudo chmod +x "$IBC_DIR"/*.sh
    rm "/tmp/$IBC_ZIP"
    info "IBC instalado en $IBC_DIR"
fi

# ── 5. Configurar IBC ─────────────────────────────────────
IBC_CFG="/opt/ibc/config.ini"
if [ ! -f "$IBC_CFG" ]; then
    info "Creando configuración IBC..."
    sudo tee "$IBC_CFG" > /dev/null <<'EOF'
# IBC Configuration - editar con tu usuario y contraseña de IB
IbLoginId=TU_USUARIO_IB
IbPassword=TU_CONTRASENA_IB
TradingMode=live
IbDir=/opt/ibgateway
AcceptIncomingConnectionAction=accept
AcceptNonBrokerageAccountWarning=yes
LogToConsole=yes
EOF
    warn "Edita /opt/ibc/config.ini con tus credenciales de IB antes de continuar"
fi

# ── 6. Script de arranque de IB Gateway ───────────────────
IBGW_START_SCRIPT="/usr/local/bin/start-ibgateway.sh"
sudo tee "$IBGW_START_SCRIPT" > /dev/null <<EOF
#!/bin/bash
# Arranca IB Gateway en modo headless con pantalla virtual
export DISPLAY=:99
Xvfb :99 -screen 0 1024x768x24 &
sleep 2
/opt/ibc/gatewaystart.sh \\
    --gateway \\
    --ibcPath /opt/ibc \\
    --ibcIni /opt/ibc/config.ini \\
    --javaBin \$(which java) \\
    --ibPath /opt/ibgateway
EOF
sudo chmod +x "$IBGW_START_SCRIPT"
info "Script de arranque IB Gateway: $IBGW_START_SCRIPT"

# ── 7. Systemd: IB Gateway ────────────────────────────────
sudo tee "/etc/systemd/system/ibgateway.service" > /dev/null <<EOF
[Unit]
Description=IB Gateway
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
ExecStart=/usr/local/bin/start-ibgateway.sh
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal
User=root

[Install]
WantedBy=multi-user.target
EOF

# ── 8. Systemd: MTO ───────────────────────────────────────
sudo tee "/etc/systemd/system/$SERVICE_NAME.service" > /dev/null <<EOF
[Unit]
Description=MTO IB Discord Notifier
After=network.target ibgateway.service
Requires=ibgateway.service
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=$MTO_DIR
ExecStart=$VENV_DIR/bin/python -m src.main
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal
User=$(whoami)
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ibgateway.service
sudo systemctl enable $SERVICE_NAME.service

info "================================================="
info " Instalación completada"
info "================================================="
echo ""
echo -e "${YELLOW}PASOS SIGUIENTES:${NC}"
echo ""
echo "1. Edita la configuración de MTO:"
echo "   nano $MTO_DIR/config.yaml"
echo "   (IDs de cuentas IB, webhooks de Discord)"
echo ""
echo "2. Edita las credenciales de IB Gateway:"
echo "   sudo nano /opt/ibc/config.ini"
echo "   (usuario y contraseña de Interactive Brokers)"
echo ""
echo "3. Arranca IB Gateway:"
echo "   sudo systemctl start ibgateway"
echo "   sudo systemctl status ibgateway"
echo ""
echo "4. Arranca MTO (espera 60s a que IB Gateway cargue):"
echo "   sudo systemctl start $SERVICE_NAME"
echo "   sudo systemctl status $SERVICE_NAME"
echo ""
echo "5. Ver logs en tiempo real:"
echo "   journalctl -u $SERVICE_NAME -f"
echo "   tail -f $MTO_DIR/logs/mto.log"
echo ""
echo -e "${GREEN}¡Todo listo!${NC}"
