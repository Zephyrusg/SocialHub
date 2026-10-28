from datetime import timedelta
from pathlib import Path
import os,time
import pytest
from sqlalchemy import select
from app.core import HubError
from app.database.models import Media,Publication,PublicationAsset,PublicationTarget,utcnow
from app.services.media_budget import MediaBudget
from app.services.media_manager import MediaManager
from test_attachment_ingestion import media_settings,JPEG


def test_storage_quota_and_free_space_checked_before_allocation(media_settings,sessions,monkeypatch):
    media_settings.media_max_bytes=100
    media_settings.media_storage_max_bytes=150
    budget=MediaBudget(media_settings,sessions);budget.root.mkdir()
    (budget.root/'existing').write_bytes(b'x'*60)
    with pytest.raises(HubError) as error:
        with budget.upload():pass
    assert error.value.code=='media_quota_exceeded' and not list(budget.root.glob('.upload-*'))
    (budget.root/'existing').unlink()
    monkeypatch.setattr('app.services.media_budget.shutil.disk_usage',lambda p:type('Disk',(),{'free':0})())
    with pytest.raises(HubError) as error:
        with budget.upload():pass
    assert error.value.code=='media_storage_full' and not list(budget.root.glob('.upload-*'))


def test_cross_instance_lock_covers_receive_and_validation(media_settings,sessions):
    first,second=MediaBudget(media_settings,sessions),MediaBudget(media_settings,sessions)
    with first.upload() as path:
        with pytest.raises(HubError) as error:
            with second.upload():pass
        assert error.value.code=='media_busy' and path.exists()
    with second.upload() as path:assert path.exists()


def test_expiry_removes_bytes_retains_referenced_history_and_reimport_restores(media_settings,sessions,tmp_path):
    manager=MediaManager(media_settings,sessions);source=tmp_path/'original';source.write_bytes(JPEG)
    asset=manager.save(source,'alice');path=Path(media_settings.media_root)/asset['id']
    with sessions.begin() as db:
        item=db.get(Media,asset['id']);item.expires_at=utcnow()-timedelta(seconds=1)
        p=Publication(principal='alice',idempotency_key='history',request_hash='a'*64,payload={'immutable':'kept'});db.add(p);db.flush()
        db.add(PublicationAsset(publication_id=p.id,media_id=asset['id']))
    result=MediaBudget(media_settings,sessions).cleanup()
    assert result['expired_media']==1 and not path.exists()
    with pytest.raises(HubError) as error:manager.get(asset['id'],'alice')
    assert error.value.code=='media_expired'
    assert MediaBudget(media_settings,sessions).cleanup()['expired_media']==0
    assert manager.save(source,'alice')==asset
    assert manager.path(asset['id'],asset['sha256'])[0].read_bytes()==JPEG
    with sessions() as db:assert db.scalar(select(PublicationAsset)).media_id==asset['id']


def test_active_send_lease_prevents_deletion_but_not_indefinitely(media_settings,sessions,tmp_path):
    manager=MediaManager(media_settings,sessions);source=tmp_path/'image';source.write_bytes(JPEG);asset=manager.save(source,'alice')
    with sessions.begin() as db:
        db.get(Media,asset['id']).expires_at=utcnow()-timedelta(seconds=1)
        p=Publication(principal='alice',idempotency_key='active',request_hash='a'*64,payload={});db.add(p);db.flush()
        db.add(PublicationAsset(publication_id=p.id,media_id=asset['id']))
        t=PublicationTarget(publication_id=p.id,account_id='unused',state='sending',lease_until=utcnow()+timedelta(minutes=1));db.add(t);db.flush();tid=t.id
    assert MediaBudget(media_settings,sessions).cleanup()['expired_media']==0
    with sessions.begin() as db:db.get(PublicationTarget,tid).lease_until=utcnow()-timedelta(seconds=1)
    assert MediaBudget(media_settings,sessions).cleanup()['expired_media']==1


def test_delivery_reservation_does_not_change_preview_hash_fields(media_settings,sessions,tmp_path):
    manager=MediaManager(media_settings,sessions);source=tmp_path/'image';source.write_bytes(JPEG);asset=manager.save(source,'alice')
    with sessions.begin() as db:db.get(Media,asset['id']).expires_at=utcnow()+timedelta(seconds=20)
    manager.reserve_delivery(asset['id'])
    assert manager.get(asset['id'],'alice')==asset
    with sessions() as db:assert db.get(Media,asset['id']).expires_at>utcnow()+timedelta(minutes=59)


def test_stale_staging_and_orphan_bytes_cleaned_but_no_symlink_follow(media_settings,sessions,tmp_path):
    from uuid import uuid4
    budget=MediaBudget(media_settings,sessions);budget.root.mkdir()
    old=budget.root/'.upload-interrupted';old.write_bytes(b'old');orphan=budget.root/str(uuid4());orphan.write_bytes(b'orphan')
    for path in [old,orphan]:os.utime(path,(time.time()-1000,time.time()-1000))
    outside=tmp_path/'keep';outside.write_bytes(b'secret');link=budget.root/str(uuid4());link.symlink_to(outside)
    result=budget.cleanup()
    assert result['stale_files']==2 and not old.exists() and not orphan.exists()
    assert outside.read_bytes()==b'secret' and link.is_symlink()


def test_pixel_budget_rejects_before_decode(media_settings,sessions,tmp_path):
    media_settings.media_image_max_pixels=1
    source=tmp_path/'image';source.write_bytes(JPEG)
    with pytest.raises(HubError):MediaManager(media_settings,sessions).save(source,'alice')


async def test_idle_asgi_lifetime_cleans_files_after_upload_flags_are_disabled(media_settings,sessions,tmp_path):
    from pydantic import SecretStr
    from app.main import create_server
    import asyncio
    manager=MediaManager(media_settings,sessions);source=tmp_path/'image';source.write_bytes(JPEG);asset=manager.save(source,'alice')
    # Persist the retention marker as admission would; after restart all upload
    # flags may be off, and no MCP requests may occur for hours.
    (Path(media_settings.media_root)/'.retention-enabled').touch(mode=0o600)
    with sessions.begin() as db:db.get(Media,asset['id']).expires_at=utcnow()-timedelta(seconds=1)
    s=media_settings.model_copy(update={'database_url':SecretStr(str(sessions.kw['bind'].url)),
        'media_ingestion_enabled':False,'media_attachment_probe_enabled':False,'media_cleanup_interval_seconds':.01})
    app=create_server(s).streamable_http_app()
    async with app.router.lifespan_context(app):
        for _ in range(50):
            if not (Path(media_settings.media_root)/asset['id']).exists():break
            await asyncio.sleep(.01)
        assert not (Path(media_settings.media_root)/asset['id']).exists()
