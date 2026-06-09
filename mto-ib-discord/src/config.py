import sys
from pathlib import Path
from typing import Dict, List
import yaml
from loguru import logger


def load(path: str = "config.yaml") -> dict:
    cfg_path = Path(path)
    if not cfg_path.exists():
        logger.error(f"No se encuentra config.yaml en: {cfg_path.absolute()}")
        sys.exit(1)

    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    errors = []

    ib = cfg.get("ib", {})
    if not ib.get("host"):
        errors.append("ib.host no definido")
    if not ib.get("port"):
        errors.append("ib.port no definido")

    accounts: List[dict] = cfg.get("accounts", [])
    if not accounts:
        errors.append("No hay cuentas definidas en 'accounts'")
    for acc in accounts:
        if "<CAMBIAR>" in acc.get("id", ""):
            errors.append(f"Cuenta '{acc.get('name')}': id no configurado")
        if "<CAMBIAR>" in acc.get("discord_webhook", ""):
            errors.append(f"Cuenta '{acc.get('name')}': discord_webhook no configurado")

    discord = cfg.get("discord", {})
    if "<CAMBIAR>" in discord.get("log_webhook", ""):
        errors.append("discord.log_webhook no configurado")

    if errors:
        for e in errors:
            logger.error(f"Config: {e}")
        sys.exit(1)


def account_map(cfg: dict) -> Dict[str, dict]:
    """Devuelve dict {account_id: account_config}"""
    return {a["id"]: a for a in cfg.get("accounts", [])}
