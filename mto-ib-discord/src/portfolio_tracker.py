"""
Seguimiento semanal de evolución de cartera.
- Snapshot de NAV por cuenta tomado cada sábado desde IB Gateway
- Composición actual (Acciones / Opciones / Efectivo / Devengos) por cuenta
- Genera imagen PNG y la publica en Discord

Estructura de data/portfolio.json:
{
  "snapshots": [
    {
      "date":     "01/10/2025",
      "accounts": {"MTO Cuenta 10K": 10000.00, "MTO Cuenta 50k": 50000.00, ...},
      "total":    65000.00
    },
    ...
  ],
  "composition": {
    "MTO Cuenta 10K": {"Acciones": 4177.00, "Opciones": -698.61, "Efectivo": 6455.47},
    ...
  }
}
"""

import json
import os
import asyncio
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo
from loguru import logger
import aiohttp

from .portfolio_image import generate_portfolio_image

_MADRID = ZoneInfo("Europe/Madrid")

ACCOUNT_ORDER   = ["MTO Cuenta 10K", "MTO Cuenta 50k", "MTO Dividendos ETF 5K"]
ACCOUNT_DISPLAY = {
    "MTO Cuenta 10K":        "10K",
    "MTO Cuenta 50k":        "50k",
    "MTO Dividendos ETF 5K": "Div-ETF 5K",
}

IB_TAGS = "NetLiquidation,StockMarketValue,OptionMarketValue,TotalCashValue,AccruedDividend"


class PortfolioTracker:
    def __init__(self, data_file: str, get_ib, account_configs: List[dict]):
        """
        get_ib          : callable() → IB instance or None
        account_configs : lista de dicts con 'id' y 'name'
        """
        self.data_file       = data_file
        self.get_ib          = get_ib
        self.account_configs = account_configs
        self.log_channel     = None   # asignado desde main.py
        self._data: dict     = {"snapshots": [], "composition": {}}
        self._load()

    # ── Persistencia ──────────────────────────────────────────

    def _load(self) -> None:
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                n = len(self._data.get("snapshots", []))
                logger.info(f"Portfolio tracker: {n} snapshots históricos cargados")
        except Exception as e:
            logger.error(f"Error cargando portfolio.json: {e}")
            self._data = {"snapshots": [], "composition": {}}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error guardando portfolio.json: {e}")

    # ── Snapshot desde IB ─────────────────────────────────────

    async def _fetch_from_ib(self) -> Optional[dict]:
        """
        Obtiene NAV y composición de todas las cuentas desde IB Gateway.
        Usa accountValues(acc_id) por subcuenta — ib_insync lo mantiene en caché
        automáticamente tras la conexión (reqAccountUpdates interno en connect).
        Si el caché está vacío, hace una suscripción explícita por cuenta.
        """
        ib = self.get_ib()
        if not ib:
            logger.error("Portfolio: IB no conectado")
            return None

        WANTED_TAGS = {
            "NetLiquidation", "StockMarketValue",
            "OptionMarketValue", "TotalCashValue",
            "AccruedDividend", "AccruedCash",   # IB usa ambas según el tipo de devengo
        }

        nav: Dict[str, float]        = {}
        composition: Dict[str, dict] = {}

        for cfg in self.account_configs:
            acc_id   = cfg["id"]
            acc_name = cfg["name"]

            # 1. Leer caché (disponible si el bot ya hizo reqAccountUpdates en connect)
            vals_list = ib.accountValues(acc_id)

            # 2. Si el caché está vacío, suscribirse explícitamente
            if not vals_list:
                try:
                    await ib.reqAccountUpdatesAsync(acc_id)
                    await asyncio.sleep(2)
                    vals_list = ib.accountValues(acc_id)
                except Exception as e:
                    logger.error(f"Portfolio: error reqAccountUpdates {acc_id}: {e}")
                    continue

            # 3. Filtrar los tags que necesitamos (base currency o USD)
            vals: Dict[str, float] = {}
            for av in vals_list:
                if av.tag not in WANTED_TAGS:
                    continue
                if av.currency not in ("USD", "BASE", ""):
                    continue
                try:
                    vals[av.tag] = float(av.value)
                except (ValueError, TypeError):
                    pass

            if not vals:
                logger.warning(f"Portfolio: sin datos para {acc_name} ({acc_id})")
                continue

            nav[acc_name] = vals.get("NetLiquidation", 0.0)

            instr: Dict[str, float] = {}
            for tag, label in [
                ("StockMarketValue",  "Acciones"),
                ("OptionMarketValue", "Opciones"),
                ("TotalCashValue",    "Efectivo"),
            ]:
                v = vals.get(tag, 0.0)
                if abs(v) > 0.01:
                    instr[label] = v

            # Devengos = AccruedDividend + AccruedCash (IB puede usar cualquiera de las dos)
            devengos = vals.get("AccruedDividend", 0.0) + vals.get("AccruedCash", 0.0)
            if abs(devengos) > 0.01:
                instr["Devengos"] = devengos

            composition[acc_name] = instr

        if not nav:
            logger.error("Portfolio: sin datos de ninguna cuenta IB")
            return None

        return {"nav": nav, "composition": composition}

    # ── Envío del reporte ─────────────────────────────────────

    async def send_weekly_report(self, webhook_url: str) -> None:
        fecha = datetime.now(_MADRID).strftime("%d/%m/%Y")
        try:
            # 1. Obtener datos actuales de IB
            ib_data = await self._fetch_from_ib()
            if not ib_data:
                raise RuntimeError("No se pudo obtener datos de IB Gateway")

            nav         = ib_data["nav"]
            composition = ib_data["composition"]
            total_nav   = sum(nav.values())

            # 2. Calcular diferencial respecto al snapshot anterior
            snaps = self._data.get("snapshots", [])
            if snaps:
                prev_total = snaps[-1].get("total", 0)
                diff_pct   = ((total_nav - prev_total) / prev_total * 100) if prev_total else None
            else:
                diff_pct = None

            # 3. Añadir snapshot de hoy (si la fecha no existe ya)
            today_str = fecha
            if not snaps or snaps[-1].get("date") != today_str:
                new_snap = {
                    "date":     today_str,
                    "accounts": dict(nav),
                    "total":    total_nav,
                }
                if diff_pct is not None:
                    new_snap["diff_pct"] = round(diff_pct, 2)
                self._data["snapshots"].append(new_snap)

            # 4. Actualizar composición actual
            self._data["composition"] = composition
            self._save()

            # Validar que los totales de composición cuadren con NetLiquidation
            for acc_name, nav_val in nav.items():
                comp_total = sum(composition.get(acc_name, {}).values())
                diff = abs(nav_val - comp_total)
                if diff > 1.0:
                    logger.warning(
                        f"Portfolio: {acc_name} NAV={nav_val:.2f} pero "
                        f"suma composición={comp_total:.2f} (diff={diff:.2f})"
                    )

            # 5. Generar imagen
            year = datetime.now(_MADRID).year
            img_bytes = generate_portfolio_image(
                snapshots     = self._data["snapshots"],
                composition   = composition,
                account_order = ACCOUNT_ORDER,
                display_names = ACCOUNT_DISPLAY,
                year          = year,
            )

            # 6. Publicar en Discord
            content = f"📈 **EVOLUCIÓN CARTERA {year}  —  {fecha}**"
            if img_bytes:
                ok = await _post_image(webhook_url, img_bytes, content)
            else:
                ok = await _post_text(webhook_url,
                    content + "\n_(Pillow no disponible — imagen no generada)_")

            if ok:
                logger.info("Reporte evolución cartera enviado")
                if self.log_channel:
                    await self.log_channel.send_info(
                        f"📈 Reporte evolución cartera publicado correctamente ({fecha})"
                    )
            else:
                raise RuntimeError("Webhook devolvió error")

        except Exception as e:
            logger.error(f"Error en send_weekly_report (portfolio): {e}")
            if self.log_channel:
                await self.log_channel.send_error(
                    f"No se pudo publicar el reporte de evolución cartera ({fecha}): {e}"
                )


# ── Discord helpers ───────────────────────────────────────────

async def _post_image(url: str, img_bytes: bytes, content: str = "") -> bool:
    try:
        data = aiohttp.FormData()
        data.add_field("payload_json", json.dumps({"content": content}),
                       content_type="application/json")
        data.add_field("file", img_bytes,
                       filename="cartera.png", content_type="image/png")
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as resp:
                if resp.status not in (200, 204):
                    logger.error(f"Portfolio webhook error {resp.status}: {await resp.text()}")
                    return False
                return True
    except Exception as e:
        logger.error(f"Portfolio _post_image error: {e}")
        return False


async def _post_text(url: str, content: str) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={"content": content}) as resp:
                return resp.status in (200, 204)
    except Exception as e:
        logger.error(f"Portfolio _post_text error: {e}")
        return False
