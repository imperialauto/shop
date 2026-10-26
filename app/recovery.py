"""Read-only runtime audit. Never accepts SQL, modifies schema or creates users."""
import os
from sqlalchemy import inspect, text
from app.database import engine, Base


def read_only_enabled():
    return os.getenv("APP_READ_ONLY", "true").strip().lower() != "false"


def database_audit(include_counts=True):
    from app import models  # register expected tables; does not create them
    result = {"connected": False, "driver": engine.url.get_backend_name(),
              "host": engine.url.host or "local", "database": engine.url.database,
              "missing_tables": [], "missing_columns": {}, "tables": {}}
    try:
        with engine.connect() as conn:
            if conn.dialect.name == "postgresql":
                conn.execute(text("SET TRANSACTION READ ONLY"))
                conn.execute(text("SET LOCAL statement_timeout = '3000ms'"))
            conn.execute(text("SELECT 1"))
            result["connected"] = True
            schema = inspect(conn)
            existing = set(schema.get_table_names())
            for name, model in sorted(Base.metadata.tables.items()):
                if name not in existing:
                    result["missing_tables"].append(name)
                    continue
                columns = {c["name"] for c in schema.get_columns(name)}
                missing = sorted(set(model.columns.keys()) - columns)
                if missing:
                    result["missing_columns"][name] = missing
                if include_counts:
                    # Table name is from application metadata, never request input.
                    quoted = conn.dialect.identifier_preparer.quote(name)
                    result["tables"][name] = {"rows": conn.execute(text(f"SELECT COUNT(*) FROM {quoted}")).scalar_one()}
            conn.rollback()
    except Exception as exc:
        # Credentials, connection strings and database error text never enter logs.
        result["error_type"] = type(exc).__name__
    return result
