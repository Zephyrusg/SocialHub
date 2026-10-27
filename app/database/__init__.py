from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def database(url):
    options = {} if url.startswith('sqlite:') else {'pool_size': 3, 'max_overflow': 2}
    engine = create_engine(url, pool_pre_ping=True, hide_parameters=True, **options)
    return engine, sessionmaker(engine, expire_on_commit=False)
