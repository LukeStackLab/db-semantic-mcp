"""Lazy provider selection; importing this module never imports an Oracle driver."""

from ...config import Config
from .base import Backend


def backend_for(cfg: Config) -> Backend:
    if cfg.database_type == "oracle":
        from .oracle import OracleBackend

        return OracleBackend()
    from .sqlserver import SQLServerBackend

    return SQLServerBackend()
