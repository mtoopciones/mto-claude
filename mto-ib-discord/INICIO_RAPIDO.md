# MTO IB → Discord | Guía de inicio rápido

## ¿Qué hace este sistema?

Monitoriza tus 3 cuentas de Interactive Brokers y publica automáticamente en Discord cada operación que ejecutes, con el mismo formato visual que ya usas (apertura en naranja, cierre en azul).

---

## Estructura del proyecto

```
mto-ib-discord/
├── config.yaml          ← TU configuración (cuentas, webhooks...)
├── requirements.txt     ← Dependencias Python
├── setup.sh             ← Instalador automático para Linux
├── logs/                ← Logs del sistema
└── src/
    ├── main.py          ← Punto de entrada
    ├── config.py        ← Carga config.yaml
    ├── strategy.py      ← Detecta estrategias (PCS, IC, etc.)
    ├── metrics.py       ← Calcula prima, BP, max ganancia...
    ├── discord.py       ← Formatea y envía embeds a Discord
    ├── log_discord.py   ← Canal de log (heartbeat, errores)
    ├── ib_connector.py  ← Conexión IB Gateway + reconexión auto
    ├── position_tracker.py  ← Detecta apertura/cierre
    └── fill_collector.py    ← Agrupa patas de una misma orden
```

---

## Paso 1 – Configurar config.yaml

Abre `config.yaml` y rellena:

```yaml
accounts:
  - id: "U1234567"      # Tu ID real de subcuenta IB
    name: "Cuenta A"
    discord_webhook: "https://discord.com/api/webhooks/..."  # webhook del canal A
```

Para obtener el webhook de Discord: Canal → Editar canal → Integraciones → Webhooks → Nuevo webhook → Copiar URL.

---

## Paso 2 – Instalar en el servidor Linux

Sube la carpeta `mto-ib-discord/` al servidor y ejecuta:

```bash
bash setup.sh
```

El script instala: Python, Java, IB Gateway, IBC (auto-login), y configura los servicios systemd.

---

## Paso 3 – Configurar credenciales de IB

```bash
sudo nano /opt/ibc/config.ini
```

Cambia `TU_USUARIO_IB` y `TU_CONTRASENA_IB` por tus credenciales reales.

---

## Paso 4 – Arrancar

```bash
# Arrancar IB Gateway primero
sudo systemctl start ibgateway
sudo systemctl status ibgateway   # espera a que diga "active"

# Esperar ~60 segundos y arrancar MTO
sudo systemctl start mto-ib-discord
sudo systemctl status mto-ib-discord
```

---

## Ver logs

```bash
# En tiempo real (consola)
journalctl -u mto-ib-discord -f

# Archivo de log
tail -f logs/mto.log
```

---

## Canales Discord

| Canal | Contenido |
|-------|-----------|
| Cuenta A | Operaciones de la subcuenta A con embed completo |
| Cuenta B | Operaciones de la subcuenta B con embed completo |
| Cuenta C | Operaciones de la subcuenta C con embed completo |
| Log (canal 4) | Heartbeat cada hora + errores + confirmaciones |

---

## Estrategias detectadas automáticamente

| Código | Nombre |
|--------|--------|
| PCS | Put Credit Spread |
| CCS | Call Credit Spread |
| BPS | Bear Put Spread |
| BCS | Bull Call Spread |
| IC | Iron Condor |
| IBF | Iron Butterfly |
| SS | Short Straddle |
| SStr | Short Strangle |
| LS | Long Straddle |
| LStr | Long Strangle |
| PCal | Put Calendar |
| CCal | Call Calendar |
| PDiag | Put Diagonal |
| CDiag | Call Diagonal |
| BWB | Broken Wing Butterfly |
| SP | Short Put |
| SC | Short Call |
| LP | Long Put |
| LC | Long Call |
| Stock | Long/Short Stock |

---

## Reconexión automática

Si IB Gateway se desconecta, el sistema reintenta automáticamente cada 30 segundos. El canal de log recibe una notificación tanto en la desconexión como en la reconexión.
