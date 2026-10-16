"""Parse Oracle routing once and reject transport downgrades before login."""

import os

from ...config import Config


def connection_params(driver, cfg: Config):
    options = cfg.oracle
    values = dict(
        tcp_connect_timeout=options.connect_timeout,
        config_dir=options.config_dir,
        protocol="tcps" if cfg.encrypt else "tcp",
        ssl_server_dn_match=True,
        thick_mode_dsn_passthrough=False,
    )
    if options.dsn is None:
        values.update(host=cfg.server, port=cfg.port, service_name=options.service_name, sid=options.sid)
    if options.wallet_location:
        values["wallet_location"] = options.wallet_location
    if options.wallet_password_env:
        password = os.environ.get(options.wallet_password_env)
        if not password:
            raise ValueError("Oracle wallet password reference is unavailable")
        values["wallet_password"] = password
    if options.ssl_server_cert_dn:
        values["ssl_server_cert_dn"] = options.ssl_server_cert_dn
    params = driver.ConnectParams(**values)
    if options.dsn:
        try:
            params.parse_connect_string(options.dsn)
        except Exception:  # noqa: BLE001 (redact driver/credential failures)
            raise ValueError("Oracle DSN/TNS cannot be resolved; check config_dir and connection descriptor") from None
    expected = "tcps" if cfg.encrypt else "tcp"
    protocols = params.protocol if isinstance(params.protocol, list) else [params.protocol]
    if not protocols or any(protocol != expected for protocol in protocols):
        raise ValueError("Oracle DSN transport must match encrypt: true requires TCPS; false requires TCP")
    # DSN parameters have precedence when parsing. Reapply authentication policy
    # afterwards and never pass the original DSN to connect (including Thick).
    params.set(ssl_server_dn_match=True, thick_mode_dsn_passthrough=False)
    if options.ssl_server_cert_dn:
        params.set(ssl_server_cert_dn=options.ssl_server_cert_dn)
    return params
