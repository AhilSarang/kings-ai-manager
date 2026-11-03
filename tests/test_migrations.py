from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def _cfg(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = url
    cfg.attributes["skip_logging"] = True
    return cfg


def test_migrated_to_head(database_url):
    eng = create_engine(database_url)
    with eng.connect() as c:
        version = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    eng.dispose()
    assert version is not None


def test_models_match_migrations(database_url):
    """Fails if a model changed without a matching migration."""
    command.check(_cfg(database_url))
