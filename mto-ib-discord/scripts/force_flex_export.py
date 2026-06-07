#!/usr/bin/env python3
"""
Fuerza la exportación del Excel desde IB Flex Query de forma inmediata.
Ejecutar desde la raíz del proyecto:
    python scripts/force_flex_export.py
"""

import asyncio
import sys
import os

# Añadir el directorio raíz al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config as cfg_module
from src.flex_logbook_exporter import build_from_config
from loguru import logger


async def main():
    logger.info("=== FORCE FLEX EXPORT ===")

    cfg = cfg_module.load("config.yaml")

    # Construir mapeo account_id → nombre
    account_names = {
        acc["id"]: acc["name"]
        for acc in cfg.get("accounts", [])
        if acc.get("id") and acc.get("name")
    }
    logger.info(f"Cuentas: {account_names}")

    exporter = build_from_config(cfg)
    if not exporter:
        logger.error("FlexLogbookExporter no pudo inicializarse — revisa ib_flex en config.yaml")
        return

    discord_webhook = cfg.get("discord", {}).get("logbook_export_webhook", "")
    if not discord_webhook:
        logger.warning("logbook_export_webhook no configurado — el Excel se subirá a Dropbox pero no a Discord")

    await exporter.export_and_publish(discord_webhook)
    logger.info("=== EXPORT COMPLETADO ===")


if __name__ == "__main__":
    asyncio.run(main())
