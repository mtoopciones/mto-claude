"""
Re-publica la tarjeta de cierre de IREN en Discord con los valores correctos.
Datos confirmados por IB:
  BOT 1 IREN Put 37 Jun-2026 @ 0.98  |  comm=0.769  |  realizedPNL=61.18
"""
import asyncio, yaml, sys, io
from datetime import datetime
sys.path.insert(0, "/root/mto-ib-discord")

# ── Datos confirmados del cierre ───────────────────────────────
SYMBOL        = "IREN"
STRIKE        = 37.0
EXPIRY        = "20260619"     # Jun-2026 (tercer viernes)
RIGHT         = "P"
ACTION        = "BUY"          # BOT = compra para cerrar put corta
FILL_PRICE    = 0.98
COMMISSION    = 0.769
REALIZED_PNL  = 61.18
QUANTITY      = 1
MULTIPLIER    = 100.0
EXEC_TIME     = datetime(2026, 5, 20, 15, 16, 15)   # hora UTC sin tz
COMPANY_NAME  = "Iris Energy"
EXCHANGE      = "SMART"
# ──────────────────────────────────────────────────────────────


async def main():
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    # Webhook: intentar encontrar la cuenta que tiene IREN
    accounts = cfg.get("accounts", [])
    if not accounts:
        print("ERROR: no hay cuentas en config.yaml")
        return

    # Usar la primera cuenta (o ajusta el índice si tienes varias)
    account = accounts[0]
    webhook_url  = account["discord_webhook"]
    account_name = account["name"]
    logo_url     = cfg.get("discord", {}).get("logo_url", "")

    print(f"Publicando en: {account_name} | {webhook_url[:60]}...")

    # ── Construir objetos de estrategia y métricas ─────────────
    from src.strategy import Leg, StrategyInfo
    from src.metrics import TradeMetrics
    from src.position_tracker import TradeEvent
    from src.discord import send_trade

    leg = Leg(
        symbol       = SYMBOL,
        sec_type     = "OPT",
        right        = RIGHT,
        strike       = STRIKE,
        expiry       = EXPIRY,
        action       = ACTION,
        quantity     = QUANTITY,
        fill_price   = FILL_PRICE,
        commission   = COMMISSION,
        account      = account.get("id", ""),
        order_id     = 0,
        exec_time    = EXEC_TIME,
        multiplier   = MULTIPLIER,
        company_name = COMPANY_NAME,
        exchange     = EXCHANGE,
        realized_pnl = REALIZED_PNL,
    )

    # "Buy Put" = cierre de una put corta (CSP)
    strategy = StrategyInfo(
        name           = "Buy Put",
        short_name     = "PUT",
        legs           = [leg],
        is_credit      = False,
        underlying     = SYMBOL,
        primary_expiry = EXPIRY,
        num_contracts  = QUANTITY,
    )

    # Calcular métricas directamente (incluye realizedPNL)
    from src.metrics import calculate as calc_metrics
    metrics = calc_metrics(strategy)

    print(f"Métricas calculadas:")
    print(f"  net_premium (bruto):      ${metrics.net_premium:,.2f}")
    print(f"  net_premium_after_comm:   ${metrics.net_premium_after_comm:,.2f}")
    print(f"  close_cost:               ${metrics.close_cost}")
    print(f"  trade_result (realizedPNL): ${metrics.trade_result}")

    # Publicar
    ok = await send_trade(
        webhook_url  = webhook_url,
        strategy     = strategy,
        metrics      = metrics,
        event_type   = TradeEvent.CLOSE,
        account_name = account_name,
        logo_url     = logo_url,
    )

    if ok:
        print("✅ Tarjeta de cierre IREN publicada en Discord")
    else:
        print("❌ Error al publicar en Discord")


asyncio.run(main())
