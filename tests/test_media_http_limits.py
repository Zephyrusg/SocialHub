import asyncio
from pathlib import Path
import httpx
from cryptography.fernet import Fernet
from pydantic import SecretStr
import pytest
from app.config import Settings
from app.database import database
from app.database.models import Base
from app.main import create_server


async def test_receive_deadline_cleans_partial_file_and_frees_admission(tmp_path):
    url=f'sqlite:///{tmp_path}/http.db';engine,_=database(url);Base.metadata.create_all(engine)
    s=Settings(_env_file=None,database_url=url,encryption_key=Fernet.generate_key().decode(),local_token='a'*64,
        media_root=str(tmp_path/'media'),media_ingestion_enabled=True,media_receive_timeout_seconds=1)
    app=create_server(s).streamable_http_app()
    async def drip():
        yield b'partial'
        await asyncio.Event().wait()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost:8787',headers={'Authorization':'Bearer '+'a'*64}) as client:
            response=await client.post('/media/upload',content=drip())
            assert response.status_code==408 and not list((tmp_path/'media').glob('.upload-*'))
            response=await client.post('/media/upload',content=Path('tests/fixtures/portrait.jpg').read_bytes())
            assert response.status_code==200


async def test_legacy_http_ingestion_default_off_is_independent_of_signed_delivery(tmp_path):
    url=f'sqlite:///{tmp_path}/http.db';engine,_=database(url);Base.metadata.create_all(engine)
    s=Settings(_env_file=None,database_url=url,encryption_key=Fernet.generate_key().decode(),local_token='a'*64,
        media_root=str(tmp_path/'media'),media_ingestion_enabled=False,media_delivery_enabled=True)
    app=create_server(s).streamable_http_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost:8787') as client:
            response=await client.post('/media/upload',content=b'no allocation')
            assert response.status_code==404 and not (tmp_path/'media').exists()
