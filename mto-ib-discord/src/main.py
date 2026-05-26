"""
MTO IB → Discord  |  Punto de entrada principal.
Conecta IB Gateway, escucha fills, detecta estrategias y publica en Discord.
"""

import asyncio
import sys
import os
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Tuple
from zoneinfo import ZoneInfo
from loguru import logger
from ib_insync import IB, Fill, ExecutionFilter

_MADRID = ZoneInfo("Europe/Madrid")

from . import config as cfg_module
from .ib_connector import IBConnector
from .position_tracker import PositionTracker, TradeEvent
from .fill_collector import FillCollector, _fills_to_legs
from .strategy import classify, Leg, StrategyInfo
from .metrics import calculate as calc_metrics, TradeMetrics
from .discord import send_trade, send_roll
from .log_discord import LogChannel
from .daily_reporter import DailyReporter
from .weekly_analyst import WeeklyAnalyst
from .stripe_onboarding import StripeOnboarding
from .pnl_tracker import PnlTracker
from .portfolio_tracker import PortfolioTracker
from .logbook_updater import LogbookUpdater
from .facebook_poster import build_from_config as _build_facebook
from .instagram_poster import build_from_config as _build_instagram
from .twitter_poster import build_from_config as _build_twitter
from .health_reporter import HealthReporter


# ─────────────────────────────────────────────────────────────
# Estado global del proceso
# ─────────────────────────────────────────────────────────────

cfg: dict = {}
account_map: dict = {}
position_tracker: PositionTracker = PositionTracker()
log_channel: Optional[LogChannel] = None
fill_collector: Optional[FillCollector] = None
daily_reporter: Optional[DailyReporter] = None
pnl_tracker: Optional[PnlTracker] = None
portfolio_tracker: Optional[PortfolioTracker] = None
logbook: Optional[LogbookUpdater] = None
weekly_analyst: Optional[WeeklyAnalyst] = None
ib_ref: Optional[IB] = None
facebook_poster = None
instagram_poster = None
twitter_poster = None

ROLL_WINDOW = 60  # segundos para detectar roll
_pending_close: Dict[tuple, dict] = {}  # {(account, symbol, right): {...}}
_seen_exec_ids: set = set()            # IDs ya procesados (evita duplicados en polling)


# ─────────────────────────────────────────────────────────────
# Callbacks de conexión
# ─────────────────────────────────────────────────────────────

async def on_connected(ib: IB) -> None:
    global ib_ref
    ib_ref = ib

    # Pasar referencia IB al daily reporter para cotizaciones en tiempo real
    if daily_reporter:
        daily_reporter.ib = ib

    # Cargar posiciones actuales — forzar reqPositions para vaciar caché vacía
    ib.reqPositions()          # solicita actualización a IB
    await asyncio.sleep(2)     # espera a que lleguen los datos
    await position_tracker.load_from_ib(ib)

    # Recuperar fills recientes antes de registrar el handler (evita duplicados)
    await _recover_recent_fills(ib)

    # Registrar handler de fills en vivo
    ib.execDetailsEvent += on_fill

    # Arrancar polling de respaldo (detecta fills no recibidos por evento)
    asyncio.ensure_future(_poll_fills_loop())

    # Notificar cuentas activas
    accounts = [acc["name"] for acc in cfg.get("accounts", [])]
    log_channel.set_connected_accounts(accounts)
    await log_channel.send_connected()
    logger.info("Sistema listo. Escuchando operaciones...")


async def on_disconnected(reason: str) -> None:
    global ib_ref
    if ib_ref:
        try:
            ib_ref.execDetailsEvent -= on_fill
        except Exception:
            pass
    ib_ref = None
    await log_channel.send_disconnected(reason)


# ─────────────────────────────────────────────────────────────
# Recuperación de fills recientes al reconectar
# ─────────────────────────────────────────────────────────────

async def _recover_recent_fills(ib: IB) -> None:
    """
    Al arrancar/reconectar:
    - Fills de los últimos 30 min → se publican en Discord (y se registran en daily reporter)
    - Fills del resto del día de hoy → solo se registran en daily reporter (sin Discord)
    Esto garantiza que el reporte post-mercado siempre tenga el día completo,
    aunque el bot se haya reiniciado varias veces.
    """
    from zoneinfo import ZoneInfo as _ZI
    from datetime import time as _time, date as _date

    try:
        logger.info("Consultando fills del día...")
        fills: List[Fill] = await ib.reqExecutionsAsync(ExecutionFilter())

        now          = datetime.now()
        cutoff_30m   = now - timedelta(minutes=30)

        # Inicio del día de trading en hora ET (medianoche ET → UTC naive)
        _ET = _ZI("America/New_York")
        today_et     = datetime.now(_ET).date()
        day_start    = (
            datetime.combine(today_et, _time(0, 0), tzinfo=_ET)
            .astimezone(_ZI("UTC"))
            .replace(tzinfo=None)
        )

        # Filtrar solo fills de hoy
        today_fills = [
            f for f in fills
            if f.time and f.time.replace(tzinfo=None) >= day_start
        ]

        if not today_fills:
            logger.info("No hay fills de hoy")
            return

        # Agrupar por (account, orderId) y ordenar cronológicamente
        orders: dict = {}
        for fill in today_fills:
            key = (fill.execution.acctNumber, fill.execution.orderId)
            orders.setdefault(key, []).append(fill)

        def _order_time(order_fills):
            t = order_fills[0].time
            return t.replace(tzinfo=None) if t else datetime.min

        sorted_orders = sorted(orders.values(), key=_order_time)

        recent_count  = 0
        history_count = 0

        for order_fills in sorted_orders:
            legs = _fills_to_legs(order_fills)
            if not legs:
                continue
            t = order_fills[0].time
            order_dt = t.replace(tzinfo=None) if t else datetime.min

            if order_dt > cutoff_30m:
                # Reciente: publicar en Discord (también registra en daily reporter)
                await process_order(legs)
                recent_count += 1
            else:
                # Histórico de hoy: solo registrar en daily reporter
                await _record_in_reporter(legs)
                history_count += 1

            # Marcar todos los execIds de esta orden como procesados
            for f in order_fills:
                if f.execution and f.execution.execId:
                    _seen_exec_ids.add(f.execution.execId)

        logger.info(
            f"Recuperación: {recent_count} órdenes publicadas en Discord, "
            f"{history_count} registradas solo en daily reporter"
        )

    except Exception as e:
        logger.error(f"Error recuperando fills: {e}")


async def _record_in_reporter(legs: List[Leg]) -> None:
    """Registra una orden del día en el daily reporter sin publicar en Discord."""
    if not daily_reporter or not legs:
        return
    account_info = account_map.get(legs[0].account)
    if not account_info:
        return
    try:
        strategy = classify(legs)
        metrics  = calc_metrics(strategy)
        # Heurística: prima neta positiva = apertura/roll, negativa = cierre
        prem = metrics.net_premium_after_comm or 0.0
        event_type = TradeEvent.OPEN if prem >= 0 else TradeEvent.CLOSE
        daily_reporter.record_trade(event_type, strategy, metrics, account_info["name"])
        logger.debug(f"  → Daily reporter (histórico): {event_type} {strategy.short_name} {strategy.underlying}")
    except Exception as e:
        logger.error(f"Error registrando orden histórica en daily reporter: {e}")


# ─────────────────────────────────────────────────────────────
# Procesamiento de fills
# ─────────────────────────────────────────────────────────────

def on_fill(trade, fill: Fill) -> None:
    """Llamado por ib_insync en cada fill individual."""
    try:
        exec_id = fill.execution.execId if fill.execution else None
        if exec_id and exec_id in _seen_exec_ids:
            return  # ya procesado por el polling
        fill_collector.handle_fill(fill)
        # Marcar como procesado para que el polling de respaldo no lo reprocese
        if exec_id:
            _seen_exec_ids.add(exec_id)
    except Exception as e:
        logger.error(f"on_fill error: {e}")


async def _poll_fills_loop() -> None:
    """
    Polling de respaldo cada 2 minutos.
    Detecta fills que no llegan por evento (órdenes manuales / sesión reiniciada).
    """
    POLL_INTERVAL = 120  # segundos
    LOOKBACK      = timedelta(minutes=35)  # ventana de búsqueda

    await asyncio.sleep(POLL_INTERVAL)  # espera inicial antes del primer poll

    while True:
        try:
            if not ib_ref or not ib_ref.isConnected():
                await asyncio.sleep(POLL_INTERVAL)
                continue

            from zoneinfo import ZoneInfo as _ZI
            from datetime import time as _time

            fills: List[Fill] = await ib_ref.reqExecutionsAsync(ExecutionFilter())
            now    = datetime.now()
            cutoff = now - LOOKBACK

            # Agrupar por (account, orderId)
            orders: dict = {}
            for f in fills:
                exec_id = f.execution.execId if f.execution else None
                if not exec_id or exec_id in _seen_exec_ids:
                    continue
                t = f.time.replace(tzinfo=None) if f.time else datetime.min
                if t < cutoff:
                    # Demasiado antiguo — marcamos como visto sin publicar
                    _seen_exec_ids.add(exec_id)
                    continue
                key = (f.execution.acctNumber, f.execution.orderId)
                orders.setdefault(key, []).append(f)

            if orders:
                logger.info(f"Polling: {len(orders)} orden(es) nuevas detectadas")
                for order_fills in orders.values():
                    legs = _fills_to_legs(order_fills)
                    if legs:
                        await process_order(legs)
                    # Marcar todos los execIds como procesados
                    for f in order_fills:
                        if f.execution and f.execution.execId:
                            _seen_exec_ids.add(f.execution.execId)

        except Exception as e:
            logger.error(f"Poll fills loop error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


async def process_order(legs: List[Leg]) -> None:
    """Llamado cuando todos los fills de una orden están listos."""
    if not legs:
        return

    account_id = legs[0].account
    account_info = account_map.get(account_id)
    if not account_info:
        logger.warning(f"Cuenta {account_id} no configurada, ignorando operación")
        return

    # Clasificar estrategia
    strategy: StrategyInfo = classify(legs)

    # Pata principal para el tracker (primera vendida, o primera si no hay)
    primary_leg = next((l for l in legs if l.action == "SELL"), legs[0])

    # SC → CC: en España no se pueden abrir Naked Calls.
    # Si hay ≥100 acciones del subyacente en cartera, es una Covered Call.
    if strategy.short_name == "SC":
        stock_qty = position_tracker.get_position(
            primary_leg.account, primary_leg.symbol, "STK", "", 0.0, ""
        )
        if stock_qty >= 100:
            strategy = StrategyInfo(
                name           = "Covered Call",
                short_name     = "CC",
                legs           = strategy.legs,
                is_credit      = True,
                underlying     = strategy.underlying,
                primary_expiry = strategy.primary_expiry,
                num_contracts  = strategy.num_contracts,
            )
            logger.info(f"  → SC reclasificado como CC ({int(stock_qty)} acciones de {primary_leg.symbol} en cartera)")

    logger.info(f"Operación detectada: {strategy.name} en {strategy.underlying} "
                f"| Cuenta: {account_info['name']}")
    signed_delta = (
        -primary_leg.quantity if primary_leg.action == "SELL" else primary_leg.quantity
    )

    # Determinar tipo de evento ANTES de actualizar posición
    event_type = position_tracker.determine_trade_event(
        account=primary_leg.account,
        symbol=primary_leg.symbol,
        sec_type=primary_leg.sec_type,
        right=primary_leg.right,
        strike=primary_leg.strike,
        expiry=primary_leg.expiry,
        delta=signed_delta,
    )

    logger.info(f"  → {event_type} | {strategy.num_contracts} contratos")

    # Actualizar posición INMEDIATAMENTE (crítico para detección de roll)
    new_qty = _calc_new_position(primary_leg, signed_delta)
    position_tracker.update(
        account=primary_leg.account,
        symbol=primary_leg.symbol,
        sec_type=primary_leg.sec_type,
        right=primary_leg.right,
        strike=primary_leg.strike,
        expiry=primary_leg.expiry,
        new_qty=new_qty,
    )

    # Calcular métricas
    metrics = calc_metrics(strategy)

    # ── Lógica de roll ────────────────────────────────────────
    # Un roll es: CLOSE + OPEN del mismo subyacente, mismo tipo de opción
    # (ambos puts O ambos calls) y direcciones opuestas (uno compra, otro vende)
    roll_key     = _make_roll_key(primary_leg.account, primary_leg.symbol, legs)
    is_full_close = event_type == TradeEvent.CLOSE
    is_open_event = event_type in (TradeEvent.OPEN, TradeEvent.ADD)

    if is_open_event and roll_key in _pending_close:
        pending = _pending_close[roll_key]
        # Verificar direcciones opuestas: close y open deben tener delta contrario
        # (si cerré una posición corta, el delta del cierre es positivo;
        #  si abro una nueva posición corta, el delta de la apertura es negativo)
        directions_ok = (pending["signed_delta"] * signed_delta) < 0
        if directions_ok:
            _pending_close.pop(roll_key)
            task = pending.get("task")
            if task and not task.done():
                task.cancel()
            logger.info(f"  → ROLL detectado en {primary_leg.symbol} ({roll_key[2]})")
            await _publish_roll(
                account_info=account_info,
                close_strategy=pending["strategy"],
                close_metrics=pending["metrics"],
                open_strategy=strategy,
                open_metrics=metrics,
            )
        else:
            # Mismo tipo pero misma dirección → no es un roll, publicar todo
            pending_entry = _pending_close.pop(roll_key)
            task = pending_entry.get("task")
            if task and not task.done():
                task.cancel()
            logger.info(f"  → Misma dirección, no es roll. Publicando cierre y apertura por separado.")
            await _publish_trade(pending_entry["account_info"], pending_entry["strategy"],
                                 pending_entry["metrics"], pending_entry["event_type"])
            await _publish_trade(account_info, strategy, metrics, event_type)

    elif is_full_close:
        # Cierre completo: esperar ROLL_WINDOW por si llega apertura (roll)
        if roll_key in _pending_close:
            old = _pending_close[roll_key]
            if old.get("task") and not old["task"].done():
                old["task"].cancel()

        _pending_close[roll_key] = {
            "strategy":    strategy,
            "metrics":     metrics,
            "account_info": account_info,
            "event_type":  event_type,
            "signed_delta": signed_delta,
            "task": asyncio.ensure_future(
                _expire_pending_close(roll_key, account_info, strategy, metrics, event_type)
            ),
        }
        logger.info(f"  → Cierre en buffer ({ROLL_WINDOW}s) para detectar roll en {primary_leg.symbol} ({roll_key[2]})")

    else:
        # Apertura, incremento, cierre parcial → publicar directamente
        await _publish_trade(account_info, strategy, metrics, event_type)


async def _expire_pending_close(
    roll_key: tuple,
    account_info: dict,
    strategy: StrategyInfo,
    metrics: "TradeMetrics",
    event_type: str,
) -> None:
    """Publica el cierre si transcurre ROLL_WINDOW sin apertura del mismo subyacente."""
    await asyncio.sleep(ROLL_WINDOW)
    entry = _pending_close.pop(roll_key, None)
    if entry:
        logger.info(f"  → Ventana roll expirada para {roll_key[1]}, publicando cierre")
        await _publish_trade(account_info, strategy, metrics, event_type)


async def _publish_trade(
    account_info: dict,
    strategy: "StrategyInfo",
    metrics: "TradeMetrics",
    event_type: str,
) -> None:
    discord_cfg = cfg.get("discord", {})
    logo_url    = discord_cfg.get("logo_url", "")
    webhook_url = account_info["discord_webhook"]

    ok = await send_trade(
        webhook_url=webhook_url,
        strategy=strategy,
        metrics=metrics,
        event_type=event_type,
        account_name=account_info["name"],
        logo_url=logo_url,
    )
    if ok:
        logger.info(f"  → Publicado en Discord ({account_info['name']})")
    else:
        await log_channel.send_error(
            f"No se pudo publicar en Discord: {strategy.name} {strategy.underlying} "
            f"| {account_info['name']}"
        )

    await log_channel.send_trade_confirmation(
        account_name=account_info["name"],
        event_type=event_type,
        strategy_short=strategy.short_name,
        symbol=strategy.underlying,
        contracts=strategy.num_contracts,
        net_premium=metrics.net_premium_after_comm,
    )

    if daily_reporter:
        daily_reporter.record_trade(event_type, strategy, metrics, account_info["name"])
    if pnl_tracker:
        pnl_tracker.record_trade(event_type, strategy, metrics, account_info["name"])
    if logbook:
        await _logbook_record_trade(event_type, strategy, metrics, account_info["name"])


def _parse_expiry(expiry_str: Optional[str]) -> datetime:
    """Parsea la fecha de vencimiento IB en varios formatos posibles."""
    if not expiry_str:
        return datetime.now()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y%m"):
        try:
            return datetime.strptime(expiry_str, fmt)
        except ValueError:
            continue
    try:
        return datetime.strptime(expiry_str[:8], "%Y%m%d")
    except Exception:
        return datetime.now()


def _primary_leg(strategy: "StrategyInfo", is_credit: bool) -> "Leg":
    """
    Devuelve la pata principal a registrar en el logbook:
    – crédito  → pata SELL (la que genera la prima cobrada)
    – débito   → pata BUY  (la que define el spread de deuda)
    Para estrategias multi-pata (IC, IBUT…) devuelve la primera SELL/BUY encontrada.
    """
    opt_legs = [l for l in strategy.legs if l.sec_type == "OPT"]
    all_legs = opt_legs or strategy.legs
    if is_credit:
        return next((l for l in all_legs if l.action == "SELL"), all_legs[0])
    else:
        return next((l for l in all_legs if l.action == "BUY"), all_legs[0])


async def _logbook_record_trade(
    event_type:   str,
    strategy:     "StrategyInfo",
    metrics:      "TradeMetrics",
    account_name: str,
) -> None:
    """
    Escribe UNA FILA POR PATA de opción en el logbook Excel.
    Cada pata aporta su propio strike, right, fill_price y comisión.
    """
    from .position_tracker import TradeEvent
    try:
        if not strategy.legs:
            return

        opt_legs = [l for l in strategy.legs if l.sec_type == "OPT"]
        if not opt_legs:
            return

        trade_dt = datetime.now(_MADRID)

        if event_type in (TradeEvent.OPEN, TradeEvent.ADD):
            for leg in opt_legs:
                await logbook.record_open(
                    symbol       = strategy.underlying,
                    strategy     = strategy.short_name or strategy.name,
                    account_name = account_name,
                    strike       = float(leg.strike or 0),
                    expiry       = _parse_expiry(leg.expiry),
                    right        = leg.right or "PUT",
                    action       = leg.action,                          # "BUY" o "SELL"
                    quantity     = strategy.num_contracts,
                    premium      = round(abs(leg.fill_price or 0), 4), # fill_price ya es $/acción
                    commission   = round(abs(leg.commission or 0), 2),
                    trade_date   = trade_dt,
                )

        elif event_type in (TradeEvent.CLOSE, TradeEvent.PARTIAL_CLOSE):
            for leg in opt_legs:
                await logbook.record_close(
                    symbol       = strategy.underlying,
                    account_name = account_name,
                    strike       = float(leg.strike or 0),
                    expiry       = _parse_expiry(leg.expiry),
                    right        = leg.right or "PUT",
                    action       = leg.action,                          # "BUY" o "SELL"
                    quantity     = strategy.num_contracts,
                    premium      = round(abs(leg.fill_price or 0), 4),
                    commission   = round(abs(leg.commission or 0), 2),
                    close_date   = trade_dt,
                    close_status = "Vendida",
                    open_status  = "Cerrada",
                )
    except Exception as e:
        logger.error(f"Logbook record_trade error: {e}")


async def _publish_roll(
    account_info: dict,
    close_strategy: "StrategyInfo",
    close_metrics: "TradeMetrics",
    open_strategy: "StrategyInfo",
    open_metrics: "TradeMetrics",
) -> None:
    discord_cfg = cfg.get("discord", {})
    logo_url    = discord_cfg.get("logo_url", "")
    webhook_url = account_info["discord_webhook"]

    ok = await send_roll(
        webhook_url=webhook_url,
        close_strategy=close_strategy,
        close_metrics=close_metrics,
        open_strategy=open_strategy,
        open_metrics=open_metrics,
        account_name=account_info["name"],
        logo_url=logo_url,
    )
    if ok:
        logger.info(f"  → Roll publicado en Discord ({account_info['name']})")
    else:
        await log_channel.send_error(
            f"No se pudo publicar roll en Discord: {open_strategy.underlying} "
            f"| {account_info['name']}"
        )

    await log_channel.send_trade_confirmation(
        account_name=account_info["name"],
        event_type=TradeEvent.ROLL,
        strategy_short=open_strategy.short_name,
        symbol=open_strategy.underlying,
        contracts=open_strategy.num_contracts,
        net_premium=open_metrics.net_premium_after_comm,
    )

    if daily_reporter:
        daily_reporter.record_roll(open_strategy, open_metrics, account_info["name"])
    if pnl_tracker:
        pnl_tracker.record_roll(open_strategy, open_metrics, account_info["name"])
    if logbook:
        await _logbook_record_roll(
            account_info["name"], close_strategy, close_metrics,
            open_strategy, open_metrics
        )


async def _logbook_record_roll(
    account_name:   str,
    close_strategy: "StrategyInfo",
    close_metrics:  "TradeMetrics",
    open_strategy:  "StrategyInfo",
    open_metrics:   "TradeMetrics",
) -> None:
    """
    Registra un roll en el logbook: UNA FILA POR PATA en cierre y en apertura,
    todas con el mismo número de referencia de roll en columna AF.
    """
    try:
        roll_ref = await logbook.get_next_roll_ref()
        trade_dt = datetime.now(_MADRID)

        # ── Cierre: una fila por pata ──
        for leg in [l for l in close_strategy.legs if l.sec_type == "OPT"]:
            await logbook.record_close(
                symbol       = close_strategy.underlying,
                account_name = account_name,
                strike       = float(leg.strike or 0),
                expiry       = _parse_expiry(leg.expiry),
                right        = leg.right or "PUT",
                action       = leg.action,
                quantity     = close_strategy.num_contracts,
                premium      = round(abs(leg.fill_price or 0), 4),
                commission   = round(abs(leg.commission or 0), 2),
                close_date   = trade_dt,
                close_status = "Vendida",
                open_status  = "Cerrada",
                roll_ref     = roll_ref,
            )

        # ── Apertura: una fila por pata ──
        for leg in [l for l in open_strategy.legs if l.sec_type == "OPT"]:
            await logbook.record_open(
                symbol       = open_strategy.underlying,
                strategy     = open_strategy.short_name or open_strategy.name,
                account_name = account_name,
                strike       = float(leg.strike or 0),
                expiry       = _parse_expiry(leg.expiry),
                right        = leg.right or "PUT",
                action       = leg.action,
                quantity     = open_strategy.num_contracts,
                premium      = round(abs(leg.fill_price or 0), 4),
                commission   = round(abs(leg.commission or 0), 2),
                trade_date   = trade_dt,
                roll_ref     = roll_ref,
            )
    except Exception as e:
        logger.error(f"Logbook record_roll error: {e}")


def _make_roll_key(account: str, symbol: str, legs: List[Leg]) -> tuple:
    """
    Clave de roll: (account, symbol, right).
    'right' resume el tipo de opción: "P", "C", "CP" (IC), o "STK".
    Solo se detecta roll si el cierre y la apertura comparten la misma clave.
    """
    opt_legs = [l for l in legs if l.sec_type == "OPT"]
    if opt_legs:
        rights = "".join(sorted(set(l.right for l in opt_legs if l.right)))
    else:
        rights = "STK"
    return (account, symbol, rights)


def _calc_new_position(leg: Leg, delta: float) -> float:
    prev = position_tracker.get_position(
        leg.account, leg.symbol, leg.sec_type,
        leg.right or "", leg.strike or 0.0, leg.expiry or ""
    )
    return prev + delta


# ─────────────────────────────────────────────────────────────
# Configuración de logging en archivo
# ─────────────────────────────────────────────────────────────

def setup_logging(log_cfg: dict) -> None:
    level    = log_cfg.get("level", "INFO")
    log_file = log_cfg.get("file", "logs/mto.log")
    max_size = log_cfg.get("max_size_mb", 50)
    backups  = log_cfg.get("backup_count", 10)

    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    logger.remove()
    logger.add(sys.stdout, level=level, colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
    logger.add(log_file, level=level, rotation=f"{max_size} MB",
               retention=backups, encoding="utf-8",
               format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}")


# ─────────────────────────────────────────────────────────────
# Arranque
# ─────────────────────────────────────────────────────────────

async def main_async() -> None:
    global cfg, account_map, log_channel, fill_collector, daily_reporter, weekly_analyst, pnl_tracker, portfolio_tracker, logbook, facebook_poster, instagram_poster, twitter_poster

    cfg         = cfg_module.load("config.yaml")
    account_map = cfg_module.account_map(cfg)
    setup_logging(cfg.get("logging", {}))

    logger.info("=" * 50)
    logger.info("MTO IB → Discord  |  Iniciando sistema")
    logger.info("=" * 50)

    discord_cfg        = cfg.get("discord", {})
    heartbeat_interval = discord_cfg.get("heartbeat_interval", 3600)
    debounce           = discord_cfg.get("fill_debounce_seconds", 3)
    account_names      = [a["name"] for a in cfg.get("accounts", [])]

    log_channel = LogChannel(
        webhook_url=discord_cfg["log_webhook"],
        heartbeat_interval=heartbeat_interval,
        account_names=account_names,
    )

    fill_collector = FillCollector(
        debounce_seconds=debounce,
        on_order_complete=process_order,
    )

    connector = IBConnector(
        cfg=cfg,
        on_connected=on_connected,
        on_disconnected=on_disconnected,
    )

    # ── P&L tracker (persistente en JSON) ───────────────────────
    pnl_data_file = cfg.get("pnl", {}).get("data_file", "data/pnl.json")
    pnl_tracker   = PnlTracker(data_file=pnl_data_file)
    pnl_tracker.log_channel = log_channel

    # ── Portfolio tracker ────────────────────────────────────────
    portfolio_webhook = discord_cfg.get("portfolio_webhook", "")
    if portfolio_webhook:
        portfolio_tracker = PortfolioTracker(
            data_file       = cfg.get("pnl", {}).get("portfolio_file", "data/portfolio.json"),
            get_ib          = lambda: ib_ref,
            account_configs = cfg.get("accounts", []),
        )
        portfolio_tracker.log_channel = log_channel

    # ── Logbook Excel (Dropbox API) ──────────────────────────────
    lb_cfg = cfg.get("logbook", {})
    if lb_cfg.get("enabled") and lb_cfg.get("dropbox_refresh_token", "").strip():
        logbook = LogbookUpdater(
            dropbox_app_key       = lb_cfg["dropbox_app_key"],
            dropbox_app_secret    = lb_cfg["dropbox_app_secret"],
            dropbox_refresh_token = lb_cfg["dropbox_refresh_token"],
            dropbox_path          = lb_cfg["dropbox_path"],
            index_file            = lb_cfg.get("index_file", "data/logbook_index.json"),
        )
        logbook.log_channel = log_channel
        logger.info("Logbook Excel activado (Dropbox API)")
    else:
        logger.info("Logbook Excel desactivado (configurar token en config.yaml)")

    # ── Daily reporter + weekly webhook ─────────────────────────
    daily_report_url  = discord_cfg.get("daily_report_webhook", "")
    weekly_report_url = discord_cfg.get("weekly_report_webhook", "")
    if daily_report_url:
        daily_reporter = DailyReporter(webhook_url=daily_report_url)
        daily_reporter.log_channel       = log_channel
        daily_reporter.premarket_webhook = discord_cfg.get("premarket_webhook", "")
        daily_reporter.pnl_tracker       = pnl_tracker
        daily_reporter.weekly_webhook    = weekly_report_url
        daily_reporter.portfolio_tracker      = portfolio_tracker
        daily_reporter.portfolio_webhook      = portfolio_webhook
        daily_reporter.logbook_updater        = logbook
        daily_reporter.logbook_export_webhook = discord_cfg.get("logbook_export_webhook", "")

        # ── Redes sociales ───────────────────────────────────────
        fb = _build_facebook(cfg)
        if fb:
            if await fb.setup():
                facebook_poster = fb
        ig = _build_instagram(cfg)
        if ig:
            if await ig.setup():
                instagram_poster = ig
        tw = _build_twitter(cfg)
        if tw:
            twitter_poster = tw
            logger.info("TwitterPoster: listo (@MtoOpciones)")

        daily_reporter.facebook_poster  = facebook_poster
        daily_reporter.instagram_poster = instagram_poster
        daily_reporter.twitter_poster   = twitter_poster
        daily_reporter.social_cfg       = cfg

        await daily_reporter.start()

    # ── Weekly analyst (lunes 12:15 Madrid) ─────────────────────
    if daily_report_url:
        weekly_analyst = WeeklyAnalyst(
            daily_report_webhook=daily_report_url,
            log_channel=log_channel,
        )
        await weekly_analyst.start()

    # ── Stripe onboarding ────────────────────────────────────────
    if cfg.get("stripe", {}).get("api_key"):
        stripe_onboarding = StripeOnboarding(cfg=cfg, log_channel=log_channel)
        await stripe_onboarding.start()

    # ── Health reporter (email diario 09:00 Madrid) ──────────────
    health_reporter = HealthReporter(
        cfg           = cfg,
        get_ib        = lambda: ib_ref,
        get_positions = lambda: len(position_tracker._positions) if position_tracker else 0,
    )
    await health_reporter.start()

    await log_channel.start()
    await connector.start()

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Deteniendo sistema...")
        await connector.stop()
        await log_channel.stop()


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Proceso terminado por el usuario")


if __name__ == "__main__":
    main()
