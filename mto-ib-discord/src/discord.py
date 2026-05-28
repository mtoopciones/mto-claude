"""
Formatea y envía los mensajes de Discord para apertura y cierre de operaciones.
Genera imagen con card_generator; fallback a embed de texto si falla.
"""

import aiohttp, json
from datetime import datetime
from typing import Optional, List
from loguru import logger

from .strategy import StrategyInfo, Leg
from .metrics import TradeMetrics
from .position_tracker import TradeEvent
from . import card_generator


# ─── Texto previo al mensaje (con @everyone) ───────────────
_CONTENT_ICON = {
    TradeEvent.OPEN:          "🟢",
    TradeEvent.ADD:           "🟢",
    TradeEvent.CLOSE:         "🔴",
    TradeEvent.PARTIAL_CLOSE: "🟠",
    TradeEvent.ROLL:          "🔄",
    TradeEvent.UNKNOWN:       "⚪",
}
_CONTENT_LABEL = {
    TradeEvent.OPEN:          "APERTURA DE OPERACIÓN",
    TradeEvent.ADD:           "AMPLIACIÓN DE POSICIÓN",
    TradeEvent.CLOSE:         "CIERRE DE OPERACIÓN",
    TradeEvent.PARTIAL_CLOSE: "CIERRE PARCIAL",
    TradeEvent.ROLL:          "ROLL DE POSICIÓN",
    TradeEvent.UNKNOWN:       "AJUSTE",
}

def _content_text(event_type: str, strategy_name: str, ticker: str) -> str:
    icon  = _CONTENT_ICON.get(event_type, "⚪")
    label = _CONTENT_LABEL.get(event_type, event_type)
    return f"{icon}  **{label}**  ·  {strategy_name}  ·  @everyone  ·  **${ticker}**"


# ─── Colores por tipo de evento ────────────────────────────
COLOR_APERTURA = 0xE67E22   # naranja
COLOR_ADD      = 0x27AE60   # verde
COLOR_CIERRE   = 0x2980B9   # azul
COLOR_PARCIAL  = 0x8E44AD   # morado
COLOR_ROLL     = 0xF1C40F   # amarillo


# ─── Helpers de formato ────────────────────────────────────

def _fmt_date(expiry: Optional[str]) -> str:
    if not expiry or len(expiry) < 8:
        return "—"
    meses = ["ene.", "feb.", "mar.", "abr.", "may.", "jun.",
             "jul.", "ago.", "sep.", "oct.", "nov.", "dic."]
    try:
        y, m, d = int(expiry[:4]), int(expiry[4:6]), int(expiry[6:8])
        return f"{d}-{meses[m-1]}-{str(y)[2:]}"
    except Exception:
        return expiry


def _fmt_money(value: Optional[float], force_sign: bool = False) -> str:
    if value is None:
        return "—"
    if value == float("inf"):
        return "Ilimitada"
    if value == float("-inf"):
        return "−Ilimitada"
    sign = "+" if force_sign and value > 0 else ""
    return f"{sign}${value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}%".replace(".", ",")


def _put_legs(strategy: StrategyInfo) -> List[Leg]:
    return sorted(
        [l for l in strategy.legs if l.sec_type == "OPT" and l.right == "P"],
        key=lambda l: l.strike or 0, reverse=True
    )


def _call_legs(strategy: StrategyInfo) -> List[Leg]:
    return sorted(
        [l for l in strategy.legs if l.sec_type == "OPT" and l.right == "C"],
        key=lambda l: l.strike or 0
    )


def _ticker_label(strategy: StrategyInfo) -> str:
    leg = strategy.legs[0] if strategy.legs else None
    if leg and leg.company_name:
        exchange = leg.exchange or "SMART"
        return f"{leg.company_name}  ·  `{exchange}:{leg.symbol}`"
    return f"`{strategy.underlying}`"


def _exec_time_str(strategy: StrategyInfo) -> str:
    leg = strategy.legs[0] if strategy.legs else None
    if leg and leg.exec_time:
        return leg.exec_time.strftime("%H:%M:%S")
    return "—"


def _leg_prices_lines(strategy: StrategyInfo) -> str:
    lines = []
    for leg in _put_legs(strategy) + _call_legs(strategy):
        emoji = "🔴" if leg.action == "SELL" else "🟢"
        action = "SELL" if leg.action == "SELL" else "BUY "
        right  = "Put " if leg.right == "P" else "Call"
        lines.append(f"{emoji} {action} {right} {leg.strike} → `{_fmt_money(leg.fill_price)}`/acc")
    return "\n".join(lines) if lines else "—"


def _strikes_lines(puts: List[Leg], calls: List[Leg], is_close: bool = False) -> str:
    lines = []
    for put in puts:
        if is_close:
            label = "Venta" if put.action == "SELL" else "Compra"
        else:
            label = "Venta" if put.action == "SELL" else "Compra"
        lines.append(f"**Strike {label} Put:** `${put.strike:,.0f}`")
    for call in calls:
        label = "Venta" if call.action == "SELL" else "Compra"
        lines.append(f"**Strike {label} Call:** `${call.strike:,.0f}`")
    return "  ·  ".join(lines) if lines else ""


def _field(name: str, value: str, inline: bool = True) -> dict:
    return {"name": name, "value": value, "inline": inline}


def _spacer() -> dict:
    return {"name": "​", "value": "​", "inline": True}


# ─── Embed de apertura ─────────────────────────────────────

def _build_apertura_embed(strategy: StrategyInfo, metrics: TradeMetrics,
                           event_type: str, logo_url: str,
                           account_name: str) -> dict:
    puts  = _put_legs(strategy)
    calls = _call_legs(strategy)

    try:
        now = datetime.now()
        meses = ["ene.", "feb.", "mar.", "abr.", "may.", "jun.",
                 "jul.", "ago.", "sep.", "oct.", "nov.", "dic."]
        today = f"{now.day}-{meses[now.month-1]}-{str(now.year)[2:]}"
    except Exception:
        today = datetime.now().strftime("%d/%m/%Y")

    color       = COLOR_ADD if event_type == TradeEvent.ADD else COLOR_APERTURA
    event_label = "AMPLIACIÓN" if event_type == TradeEvent.ADD else "APERTURA DE OPERACIÓN"
    emoji_ev    = "➕" if event_type == TradeEvent.ADD else "🚨"

    # ── Bloque descripción principal ──
    strikes_str = _strikes_lines(puts, calls)
    be_str = ""
    if metrics.breakeven is not None:
        if metrics.breakeven_high is not None:
            be_str = f"`{_fmt_money(metrics.breakeven)}` / `{_fmt_money(metrics.breakeven_high)}`"
        else:
            be_str = f"`{_fmt_money(metrics.breakeven)}`"

    desc_lines = [
        f"**{_ticker_label(strategy)}**",
        "",
        f"📅  **{today}**  →  **{_fmt_date(strategy.primary_expiry)}**"
        f"  ·  ⏰ **{_exec_time_str(strategy)}**  ·  📆 **{metrics.dte if metrics.dte is not None else '—'} DTE**",
    ]

    if strikes_str:
        desc_lines.append(f"🎯  {strikes_str}")

    desc_lines.append(
        f"📊  **{strategy.num_contracts} contrato{'s' if strategy.num_contracts != 1 else ''}**"
        + (f"  ·  ⚖️  B.E.: {be_str}" if be_str else "")
    )

    desc_lines += [
        "",
        "**💱  Precios ejecutados**",
        _leg_prices_lines(strategy),
    ]

    description = "\n".join(desc_lines)

    # ── Campos financieros (2 columnas) ──
    fields = [
        _field("​", "**─────  💰  Detalles Financieros  ─────**", inline=False),
        _field("💵  Prima neta",     f"`{_fmt_money(metrics.net_premium_after_comm)}`"),
        _field("⚡  Buying Power",   f"`{_fmt_money(metrics.buying_power)}`"),
        _field("📊  ROI estimado",   f"`{_fmt_pct(metrics.roi_pct)}`"),
        _field("📈  Máx. ganancia",  f"`{_fmt_money(metrics.max_gain)}`"),
        _field("🛡️  Riesgo máximo", f"`{_fmt_money(metrics.max_loss)}`"),
    ]

    embed = {
        "author": {
            "name": f"{emoji_ev}  {event_label}",
            **({"icon_url": logo_url} if logo_url else {}),
        },
        "title": f"{strategy.short_name}  /  {strategy.name}",
        "description": description,
        "color": color,
        "fields": fields,
        "footer": {
            "text": account_name,
            **({"icon_url": logo_url} if logo_url else {}),
        },
    }

    if logo_url:
        embed["thumbnail"] = {"url": logo_url}

    leg = strategy.legs[0] if strategy.legs else None
    if leg and leg.exec_time:
        embed["timestamp"] = leg.exec_time.isoformat()

    return embed


# ─── Embed de cierre ───────────────────────────────────────

def _build_cierre_embed(strategy: StrategyInfo, metrics: TradeMetrics,
                         event_type: str, logo_url: str,
                         account_name: str,
                         open_premium: Optional[float] = None) -> dict:
    puts  = _put_legs(strategy)
    calls = _call_legs(strategy)

    try:
        now = datetime.now()
        meses = ["ene.", "feb.", "mar.", "abr.", "may.", "jun.",
                 "jul.", "ago.", "sep.", "oct.", "nov.", "dic."]
        today = f"{now.day}-{meses[now.month-1]}-{str(now.year)[2:]}"
    except Exception:
        today = datetime.now().strftime("%d/%m/%Y")

    is_partial  = event_type == TradeEvent.PARTIAL_CLOSE
    color       = COLOR_PARCIAL if is_partial else COLOR_CIERRE
    emoji_ev    = "📉" if is_partial else "📊"
    event_label = "CIERRE PARCIAL" if is_partial else "CIERRE DE OPERACIÓN"
    c_label     = f"{strategy.num_contracts} contrato{'s' if strategy.num_contracts != 1 else ''} cerrado{'s' if strategy.num_contracts != 1 else ''}"
    if is_partial:
        c_label += " (parcial)"

    strikes_str = _strikes_lines(puts, calls, is_close=True)

    desc_lines = [
        f"**{_ticker_label(strategy)}**",
        "",
        f"📅  **{today}**  ·  Vencía: **{_fmt_date(strategy.primary_expiry)}**"
        f"  ·  ⏰ **{_exec_time_str(strategy)}**",
    ]
    if strikes_str:
        desc_lines.append(f"🎯  {strikes_str}")
    desc_lines.append(f"📊  **{c_label}**")
    desc_lines += [
        "",
        "**💱  Precios ejecutados**",
        _leg_prices_lines(strategy),
    ]

    description = "\n".join(desc_lines)

    result_val  = _fmt_money(metrics.trade_result, force_sign=True) if metrics.trade_result is not None else "—"
    result_disp = f"**`{result_val}`**"

    fields = [
        _field("​", "**─────  💰  Resultado de la Operación  ─────**", inline=False),
        _field("💵  Prima recibida", f"`{_fmt_money(open_premium)}`"),
        _field("💸  Coste cierre",   f"`{_fmt_money(metrics.close_cost)}`"),
        _field("🏆  Resultado",      result_disp),
    ]

    embed = {
        "author": {
            "name": f"{emoji_ev}  {event_label}",
            **({"icon_url": logo_url} if logo_url else {}),
        },
        "title": f"{strategy.short_name}  /  {strategy.name}",
        "description": description,
        "color": color,
        "fields": fields,
        "footer": {
            "text": account_name,
            **({"icon_url": logo_url} if logo_url else {}),
        },
    }

    if logo_url:
        embed["thumbnail"] = {"url": logo_url}

    leg = strategy.legs[0] if strategy.legs else None
    if leg and leg.exec_time:
        embed["timestamp"] = leg.exec_time.isoformat()

    return embed


# ─── Envío ─────────────────────────────────────────────────

async def send_trade(webhook_url: str, strategy: StrategyInfo, metrics: TradeMetrics,
                     event_type: str, account_name: str = "", logo_url: str = "",
                     open_premium: Optional[float] = None) -> bool:

    content = _content_text(event_type, strategy.name, strategy.underlying)

    # Intentar generar imagen
    image_bytes = card_generator.generate(strategy, metrics, event_type, logo_url, account_name)

    if image_bytes:
        return await _post_image(webhook_url, image_bytes, event_type, content=content)

    # Fallback: embed de texto
    is_open = event_type in (TradeEvent.OPEN, TradeEvent.ADD)
    if is_open:
        embed = _build_apertura_embed(strategy, metrics, event_type, logo_url, account_name)
    else:
        embed = _build_cierre_embed(strategy, metrics, event_type, logo_url, account_name, open_premium)
    return await _post_json(webhook_url, {"content": content, "embeds": [embed]})


async def send_roll(webhook_url: str,
                    close_strategy: StrategyInfo, close_metrics: TradeMetrics,
                    open_strategy: StrategyInfo, open_metrics: TradeMetrics,
                    account_name: str = "", logo_url: str = "") -> bool:

    content = _content_text(TradeEvent.ROLL, open_strategy.name, open_strategy.underlying)

    # Intentar generar imagen de roll
    image_bytes = card_generator.generate_roll(
        close_strategy, close_metrics, open_strategy, open_metrics, logo_url, account_name
    )
    if image_bytes:
        return await _post_image(webhook_url, image_bytes, TradeEvent.ROLL, content=content)

    # Fallback: dos embeds (cierre + apertura) con etiquetas de roll
    close_embed = _build_cierre_embed(close_strategy, close_metrics, TradeEvent.CLOSE, logo_url, account_name)
    open_embed  = _build_apertura_embed(open_strategy, open_metrics, TradeEvent.OPEN, logo_url, account_name)
    close_embed["author"]["name"] = f"🔄  ROLL — CIERRE  /  {close_strategy.underlying}"
    open_embed["author"]["name"]  = f"🔄  ROLL — APERTURA  /  {open_strategy.underlying}"
    close_embed["color"] = COLOR_ROLL
    open_embed["color"]  = COLOR_ROLL
    return await _post_json(webhook_url, {"content": content, "embeds": [close_embed, open_embed]})


async def _post_image(url: str, image_bytes: bytes, event_type: str,
                      content: str = "") -> bool:
    color_map = {
        TradeEvent.OPEN:          0xE67E22,
        TradeEvent.ADD:           0x27AE60,
        TradeEvent.CLOSE:         0x2980B9,
        TradeEvent.PARTIAL_CLOSE: 0x8E44AD,
        TradeEvent.ROLL:          0xF1C40F,
    }
    color = color_map.get(event_type, 0xE67E22)
    payload_dict: dict = {"embeds": [{"image": {"url": "attachment://trade.png"}, "color": color}]}
    if content:
        payload_dict["content"] = content
    payload = json.dumps(payload_dict)
    try:
        async with aiohttp.ClientSession() as session:
            form = aiohttp.FormData()
            form.add_field("file", image_bytes, filename="trade.png", content_type="image/png")
            form.add_field("payload_json", payload)
            async with session.post(url, data=form) as resp:
                if resp.status in (200, 204):
                    return True
                text = await resp.text()
                logger.error(f"Discord webhook error {resp.status}: {text}")
                return False
    except Exception as e:
        logger.error(f"Error enviando imagen a Discord: {e}")
        return False


async def _post_json(url: str, payload: dict) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status in (200, 204):
                    return True
                text = await resp.text()
                logger.error(f"Discord webhook error {resp.status}: {text}")
                return False
    except Exception as e:
        logger.error(f"Error enviando a Discord: {e}")
        return False


# Mantener compatibilidad interna
async def _post_webhook(url: str, payload: dict) -> bool:
    return await _post_json(url, payload)
