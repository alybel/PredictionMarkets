"""Migration must run cleanly against an empty database."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_migration_runs_against_empty_db(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")

    inspector = inspect(create_engine(db_url))
    tables = set(inspector.get_table_names())
    assert {"markets", "index_rules"} <= tables
    assert {"eui_snapshot_runs", "eui_market_snapshots", "eui_classifications", "eui_compositions", "eui_index_values", "eui_events"} <= tables
    snapshot_cols = {c["name"] for c in inspector.get_columns("eui_market_snapshots")}
    assert {"snapshot_date", "source", "external_id", "price", "spread", "depth_usd", "volume_24h_usd", "liquidity_score", "eligible", "exclusion_reason"} <= snapshot_cols

    market_cols = {c["name"] for c in inspector.get_columns("markets")}
    assert {"source", "external_id", "title", "category", "price", "volume", "liquidity", "end_date", "url", "tags", "fetched_at"} <= market_cols

    rule_cols = {c["name"] for c in inspector.get_columns("index_rules")}
    assert {"index_name", "categories", "keywords", "min_liquidity", "min_price", "max_price", "created_at"} <= rule_cols


def test_migration_backfills_price_corridor_on_existing_rules(tmp_path, monkeypatch):
    """Rules created before revision 0003 get the default corridor."""
    from sqlalchemy import text

    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "0002")

    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO index_rules (index_name, categories, keywords, min_liquidity, created_at) "
                "VALUES ('legacy', '[]', '[]', 0.0, '2026-09-17 00:00:00')"
            )
        )
    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        row = conn.execute(text("SELECT min_price, max_price FROM index_rules WHERE index_name = 'legacy'")).one()
    assert row.min_price == 0.03
    assert row.max_price == 0.97


def test_migration_lifts_low_liquidity_rules_to_global_floor(tmp_path, monkeypatch):
    """Rules created before revision 0004 rise to 10 000 USD; higher ones stay."""
    from sqlalchemy import text

    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "0003")

    engine = create_engine(db_url)
    with engine.begin() as conn:
        for name, liq in (("low", 1000.0), ("high", 25000.0)):
            conn.execute(
                text(
                    "INSERT INTO index_rules (index_name, categories, keywords, min_liquidity, min_price, max_price, created_at) "
                    f"VALUES ('{name}', '[]', '[]', {liq}, 0.03, 0.97, '2026-09-17 00:00:00')"
                )
            )
    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT index_name, min_liquidity FROM index_rules")).all())
    assert rows == {"low": 10000.0, "high": 25000.0}
