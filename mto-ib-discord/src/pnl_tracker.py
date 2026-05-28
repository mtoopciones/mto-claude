"""
Seguimiento persistente de P&L mensual.
Guarda los datos en data/pnl.json y genera el reporte semanal (imagen PNG) para Discord.

Estructura del JSON:
{
  "2025-11": {
    "MTO Cuenta 10K":        {"realized_pnl": 128.06, "cash_collected": -124.40},
    "MTO Cuenta 50k":        {"realized_pnl": 290.59, "cash_collected": -395.70},
    "MTO Dividendos ETF 5K": {"realized_pnl": -96.82, "cash_collected":    0.0}
  },
  ...
}

- realized_pnl:    beneficio realizado (P&L de operaciones cerradas)
- cash_collected:  efectivo cobrado (primas netas de posiciones aún abiertas)
"""

import json
import os
from datetime import datetime
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo
from loguru import logger
import aiohttp

from .position_tracker import TradeEvent
from .pnl_image import generate_pnl_image

_MADRID = ZoneInfo("Europe/Madrid")

# ── Configuración de cuentas ──────────────────────────────────
ACCOUNT_DISPLAY: Dict[str, str] = {
    "MTO Cuenta 10K":        "10 k",
    "MTO Cuenta 50k":        "50 k",
    "MTO Dividendos ETF 5K": "Dividendos - ETF",
}
ACCOUNT_ORDER = ["MTO Cuenta 10K", "MTO Cuenta 50k", "MTO Dividendos ETF 5K"]
FIXED_ROWS    = ["Op. Sueltas"]


class PnlTracker:
    def __init__(self, data_file: str = "data/pnl.json"):
        self.data_file   = data_file
        self.log_channel = None   # asignado desde main.py
        self._data: Dict[str, Dict[str, Dict[str, float]]] = {}
        self._load()

    # ── Persistencia ──────────────────────────────────────────

    def _load(self) -> None:
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                logger.info(f"PnL tracker: {len(self._data)} meses de historial cargados")
        except Exception as e:
            logger.error(f"Error cargando pnl.json: {e}")
            self._data = {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error guardando pnl.json: {e}")

    # ── Registro de operaciones ────────────────────────────────

    def _month_key(self) -> str:
        return datetime.now(_MADRID).strftime("%Y-%m")

    def _ensure(self, month: str, account: str) -> None:
        self._data.setdefault(month, {})
        self._data[month].setdefault(account, {"realized_pnl": 0.0, "cash_collected": 0.0})

    def record_trade(self, event_type: str, strategy, metrics, account_name: str) -> None:
        month = self._month_key()
        self._ensure(month, account_name)
        prem = metrics.net_premium_after_comm or 0.0

        if event_type in (TradeEvent.OPEN, TradeEvent.ADD):
            # Prima recibida al abrir → efectivo cobrado
            self._data[month][account_name]["cash_collected"] += prem

        elif event_type in (TradeEvent.CLOSE, TradeEvent.PARTIAL_CLOSE):
            if metrics.trade_result is not None:
                # P&L real disponible
                self._data[month][account_name]["realized_pnl"]   += metrics.trade_result
                open_prem = metrics.trade_result - prem
                self._data[month][account_name]["cash_collected"]  -= open_prem
            else:
                # Aproximación hasta que se implemente open_premium
                self._data[month][account_name]["realized_pnl"] += prem

        self._save()

    def record_roll(self, open_strategy, open_metrics, account_name: str) -> None:
        month = self._month_key()
        self._ensure(month, account_name)
        prem = open_metrics.net_premium_after_comm or 0.0
        self._data[month][account_name]["cash_collected"] += prem
        self._save()

    # ── Construcción de columnas rolling ──────────────────────

    def _get_rolling(
        self, n: int = 6
    ) -> Tuple[List[str], List[str], Dict[str, List[float]], Dict[str, List[float]]]:
        """
        Devuelve (headers, accounts, realized_table, cash_table).
        Columnas: más reciente IZQUIERDA → más antiguo DERECHA.
        Si hay > n meses, los más antiguos se fusionan como "Acumulado" (última columna).
        """
        all_months = sorted(self._data.keys())
        if not all_months:
            return [], [], {}, {}

        known   = [a for a in ACCOUNT_ORDER
                   if a in {acc for md in self._data.values() for acc in md}]
        unknown = sorted({acc for md in self._data.values() for acc in md} - set(ACCOUNT_ORDER))
        accounts = known + unknown

        def _get(m: str, acc: str, key: str) -> float:
            return self._data.get(m, {}).get(acc, {}).get(key, 0.0)

        if len(all_months) <= n:
            display  = list(reversed(all_months))
            headers  = [_fmt_month(m) for m in display]
            realized = {acc: [_get(m, acc, "realized_pnl")   for m in display] for acc in accounts}
            cash     = {acc: [_get(m, acc, "cash_collected") for m in display] for acc in accounts}
        else:
            individual = all_months[-(n - 1):]   # los n-1 más recientes (columnas individuales)
            merged     = all_months[:-(n - 1)]   # el resto → columna "Acumulado"

            display_ind = list(reversed(individual))
            headers = [_fmt_month(m) for m in display_ind] + ["Acumulado"]

            realized, cash = {}, {}
            for acc in accounts:
                realized[acc] = (
                    [_get(m, acc, "realized_pnl")   for m in display_ind]
                    + [sum(_get(m, acc, "realized_pnl")   for m in merged)]
                )
                cash[acc] = (
                    [_get(m, acc, "cash_collected") for m in display_ind]
                    + [sum(_get(m, acc, "cash_collected") for m in merged)]
                )

        return headers, accounts, realized, cash

    # ── Envío del reporte semanal ──────────────────────────────

    async def send_weekly_report(self, webhook_url: str) -> None:
        fecha = datetime.now(_MADRID).strftime("%d/%m/%Y")
        try:
            headers, accounts, realized_tbl, cash_tbl = self._get_rolling(6)

            if not headers or not accounts:
                await _post_webhook(webhook_url, {"embeds": [{
                    "author":      {"name": f"📅  REPORTE SEMANAL P&L  —  {fecha}"},
                    "description": "_Sin datos registrados todavía._",
                    "color":       0x27AE60,
                }]})
                logger.info("Reporte semanal P&L enviado (sin datos)")
                if self.log_channel:
                    await self.log_channel.send_info(
                        "📅 Reporte semanal P&L publicado (sin operaciones todavía)"
                    )
                return

            img_bytes = generate_pnl_image(
                headers        = headers,
                accounts       = accounts,
                display_names  = ACCOUNT_DISPLAY,
                fixed_rows     = FIXED_ROWS,
                realized_table = realized_tbl,
                cash_table     = cash_tbl,
            )

            content = f"📅 **REPORTE SEMANAL P&L  —  {fecha}**"
            if img_bytes:
                ok = await _post_image(webhook_url, img_bytes, content)
            else:
                ok = await _post_webhook(webhook_url, {
                    "content": content + "\n_(imagen no disponible — Pillow no instalado)_"
                })

            if ok:
                logger.info("Reporte semanal P&L enviado")
                if self.log_channel:
                    await self.log_channel.send_info(
                        f"📅 Reporte semanal P&L publicado correctamente ({fecha})"
                    )
            else:
                raise RuntimeError("El webhook devolvió error al publicar la imagen")

        except Exception as e:
            logger.error(f"Error en send_weekly_report: {e}")
            if self.log_channel:
                await self.log_channel.send_error(
                    f"No se pudo publicar el reporte semanal P&L ({fecha}): {e}"
                )


# ── Helpers numéricos ──────────────────────────────────────────

def _fmt_month(m: str) -> str:
    """'2025-11' → 'nov-25'"""
    try:
        dt    = datetime.strptime(m, "%Y-%m")
        meses = ["ene", "feb", "mar", "abr", "may", "jun",
                 "jul", "ago", "sep", "oct", "nov", "dic"]
        return f"{meses[dt.month - 1]}-{str(dt.year)[2:]}"
    except Exception:
        return m


# ── Webhook ────────────────────────────────────────────────────

async def _post_webhook(url: str, payload: dict) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status not in (200, 204):
                    text = await resp.text()
                    logger.error(f"Webhook P&L error {resp.status}: {text}")
                    return False
                return True
    except Exception as e:
        logger.error(f"Error posting webhook: {e}")
        return False


async def _post_image(url: str, img_bytes: bytes, content: str = "") -> bool:
    """Publica la imagen como adjunto multipart/form-data en Discord."""
    try:
        data = aiohttp.FormData()
        data.add_field(
            "payload_json",
            json.dumps({"content": content}),
            content_type="application/json",
        )
        data.add_field(
            "file",
            img_bytes,
            filename="pnl_semanal.png",
            content_type="image/png",
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as resp:
                if resp.status not in (200, 204):
                    text = await resp.text()
                    logger.error(f"Webhook imagen P&L error {resp.status}: {text}")
                    return False
                return True
    except Exception as e:
        logger.error(f"Error posting image: {e}")
        return False
