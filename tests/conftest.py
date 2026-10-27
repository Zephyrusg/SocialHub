import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database.models import Base
from app.services.hub import Hub


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path}/test.db')
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def hub(sessions):
    value = Hub(sessions)
    value.seed_mocks()
    return value
