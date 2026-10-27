from alembic import context
from app.config import Settings
from app.database import database
from app.database.models import Base

engine, _ = database(Settings().database_url.get_secret_value())
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
