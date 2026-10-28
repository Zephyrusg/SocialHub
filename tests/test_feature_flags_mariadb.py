"""Opt-in row-lock/CAS checks against a disposable, explicitly named local MariaDB."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from app.core import HubError
from app.database.models import Base, Audit
from app.services.feature_flags import FeatureFlags
from test_feature_flags import settings


@pytest.fixture
def maria_sessions():
    value=os.environ.get('SOCIALHUB_FEATURE_TEST_DATABASE_URL')
    if not value:pytest.skip('Disposable local MariaDB URL not supplied')
    url=make_url(value)
    if (url.drivername!='mysql+pymysql' or url.host not in {'127.0.0.1','localhost'}
            or not url.database or not url.database.startswith('socialhub_feature_flags_test_')):
        pytest.fail('Only a disposable local feature-test schema is permitted')
    engine=create_engine(url,hide_parameters=True,pool_pre_ping=True)
    Base.metadata.create_all(engine)
    try:yield sessionmaker(engine,expire_on_commit=False)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.parametrize('existing', [False,True])
def test_real_mariadb_concurrent_revision_and_audit(maria_sessions,existing):
    f=FeatureFlags(settings(maria_sessions),maria_sessions)
    revision=0
    if existing:revision=f.save('owner-sub',0,'media_import',False)
    def edit(name):
        try:return f.save('owner-sub',revision,name,False)
        except HubError as error:return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(edit,['facebook_publishing','facebook_images']))
    assert sum(type(r) is int for r in results)==1 and 'configuration_conflict' in results
    assert f.status('owner-sub')['revision']==revision+1
    with maria_sessions() as db:
        audit=list(db.scalars(select(Audit).where(Audit.operation=='admin_feature_updated')))
        assert len(audit)==revision+1
        assert all(a.subject=='owner-sub' and a.at for a in audit)
