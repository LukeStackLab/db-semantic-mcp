"""Existing SQL Server behavior behind the common provider boundary."""

from ...config import Config


class SQLServerBackend:
    def connect(self, cfg: Config):
        from ..connection import build_pymssql_kwargs, pymssql

        return pymssql.connect(**build_pymssql_kwargs(cfg))

    def fetch_snapshot(self, cfg: Config, conn=None):
        from ..cache.structural import fetch_snapshot_from_server

        return fetch_snapshot_from_server(cfg, conn)

    def fetch_fingerprints(self, cfg: Config, conn=None):
        from ..cache.probe import fetch_fingerprints

        return fetch_fingerprints(cfg, conn)

    def get_object_definition(self, cfg: Config, schema: str, name: str, object_type: str | None = None):
        from ..connection import fetch_one
        from ..queries.object_queries import GET_OBJECT_DEFINITION

        qualified = f"{self.quote_identifier(schema)}.{self.quote_identifier(name)}"
        row = fetch_one(cfg, GET_OBJECT_DEFINITION, (qualified,))
        return row[0] if row else None

    def get_object_dependencies(self, cfg: Config, schema: str, name: str):
        from ..connection import fetch_all
        from ..queries.object_queries import GET_OBJECT_DEPENDENCIES

        qualified = f"{self.quote_identifier(schema)}.{self.quote_identifier(name)}"
        return fetch_all(cfg, GET_OBJECT_DEPENDENCIES, (qualified,))

    def describe_result_schema(self, cursor, sql: str):
        cursor.execute(
            "SELECT column_ordinal, name, system_type_name, is_nullable, "
            "error_number, error_message FROM "
            "sys.dm_exec_describe_first_result_set(%s, NULL, 0)",
            (sql,),
        )
        columns = []
        for ordinal, name, type_name, nullable, error_number, error_message in cursor.fetchall():
            if error_number is not None or error_message:
                raise ValueError(error_message or f"SQL Server metadata error {error_number}")
            columns.append({"ordinal": ordinal, "name": name, "type": type_name, "is_nullable": bool(nullable)})
        return columns

    def convert_value(self, value, cfg: Config):
        return value

    @staticmethod
    def quote_identifier(value: str) -> str:
        return "[" + value.replace("]", "]]") + "]"

    def capabilities(self, cfg: Config) -> dict:
        return {
            "database_type": "sqlserver",
            "source": cfg.source_id,
            "definitions": True,
            "dependencies": True,
            "remote_sources": False,
            "query_timeout_semantics": "driver query timeout in seconds",
        }
