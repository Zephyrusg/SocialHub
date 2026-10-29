"""YouTube OAuth/reads/uploads use mock transports only; never real Google calls."""
import json
import time
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
from datetime import timedelta
import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from pydantic import ValidationError
from app.config import Settings
from app.core import HubError
from app.database.models import Account,Credential,PublicationTarget,Publication,Audit,SiteConfiguration,utcnow
from app.services.youtube_oauth import YouTubeOAuth,YouTubeSetup,SCOPES,ACCOUNT,unpack,access_token,vault
from app.services.feature_flags import FeatureFlags
from app.services.publishing import PublishingService
from app.services.hub import Hub
from app.models_publication import PublicationRequest,YouTubeMetadata
from app.connectors.youtube import YouTubeClient,YouTubeConnector,YouTubePublishingConnector,session_url

CID='UC'+'a'*22
OTHER='UC'+'b'*22
VID='aBcdEF123_-'
CLIENT='12345-test.apps.googleusercontent.com'
SECRET='synthetic-google-secret'
SESSION='https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=synthetic-private-session'
META={'title':'Exact music release','description':'Music #PixelPapa','tags':['music','fantasy music'], 'made_for_kids':False,'contains_synthetic_media':True}

@pytest.fixture
def yt_settings(tmp_path):
    return Settings(_env_file=None,database_url='sqlite://',encryption_key=Fernet.generate_key().decode(),local_token='a'*64,
        admin_enabled=True,admin_oidc_client_id='admin',admin_oidc_client_secret='synthetic',admin_allowed_subjects=['owner'],
        oauth_issuer='https://issuer.example.test',oauth_jwks_url='https://issuer.example.test/jwks',
        youtube_enabled=True,youtube_channel_ids=[CID],youtube_write_account_ids=[ACCOUNT],youtube_publishing_enabled=True,
        youtube_shorts_enabled=True,youtube_videos_enabled=True,publishing_enabled=True,media_root=str(tmp_path/'media'))

class Google:
    def __init__(self):
        self.calls=[];self.channels=[CID];self.scope=SCOPES;self.refresh=True;self.invalid_refresh=False;self.offset=0
        self.lose_final=False;self.init_timeout=False;self.processed=True;self.video_exists=False;self.privacy='private';self.range_override=None
        self.quota=False;self.foreign=False;self.expired_session=False;self.body=None;self.token_requests=[];self.uploaded=b''
    async def __call__(self,r):
        self.calls.append((r.method,r.url.path))
        if r.url.host=='oauth2.googleapis.com':
            values=parse_qs(r.content.decode());self.token_requests.append(values)
            if values.get('grant_type')==['refresh_token'] and self.invalid_refresh:return httpx.Response(400,json={'error':'invalid_grant','error_description':'secret must not escape'})
            payload={'access_token':'synthetic-access','expires_in':3600,'scope':' '.join(self.scope)}
            if self.refresh:payload['refresh_token']='synthetic-refresh'
            return httpx.Response(200,json=payload)
        if r.url.path.endswith('/channels'):
            return httpx.Response(200,json={'items':[{'id':c,'snippet':{'title':'PixelPapa','customUrl':'@PixelPapaMusic'},'contentDetails':{'relatedPlaylists':{'uploads':'UU'+'a'*22}}} for c in self.channels]})
        if r.url.path.endswith('/playlistItems'):return httpx.Response(200,json={'items':[{'contentDetails':{'videoId':VID}}],'nextPageToken':'next_safe'})
        if r.method=='GET' and r.url.path.endswith('/videos'):
            p={'id':VID,'snippet':{'channelId':OTHER if self.foreign else CID,'title':META['title'],'description':META['description'],'tags':META['tags'],'publishedAt':utcnow().isoformat()+'Z'},
                'status':{'uploadStatus':'processed' if self.processed else 'uploaded','privacyStatus':self.privacy,'selfDeclaredMadeForKids':False,'containsSyntheticMedia':True},
                'processingDetails':{'processingStatus':'succeeded' if self.processed else 'processing'},'fileDetails':{'fileSize':str(len(self.uploaded))}}
            return httpx.Response(200,json={'items':[p]})
        if r.method=='POST':
            if self.quota:return httpx.Response(403,json={'error':{'errors':[{'reason':'quotaExceeded'}]}})
            if self.init_timeout:raise httpx.ReadTimeout('secret URL not logged')
            self.body=json.loads(r.content);self.privacy=self.body['status']['privacyStatus'];return httpx.Response(200,headers={'Location':SESSION})
        if r.method=='PUT':
            assert str(r.url)==SESSION
            if self.expired_session:return httpx.Response(404,json={'error':{'errors':[{'reason':'notFound'}]}})
            cr=r.headers['content-range']
            if cr.startswith('bytes */'):
                if self.video_exists:return httpx.Response(201,json={'id':VID})
                return httpx.Response(308,headers={'Range':self.range_override or f'bytes=0-{self.offset-1}'} if self.offset else {})
            body=await r.aread();bounds,total=cr[6:].split('/');start,end=map(int,bounds.split('-'))
            assert start==self.offset and len(body)==end-start+1 and int(r.headers['content-length'])==len(body)
            self.uploaded+=body;self.offset=end+1
            if self.offset==int(total):
                self.video_exists=True
                if self.lose_final:self.lose_final=False;raise httpx.ReadTimeout('synthetic session secret')
                return httpx.Response(201,json={'id':VID})
            return httpx.Response(308,headers={'Range':f'bytes=0-{self.offset-1}'})
        raise AssertionError('Unexpected mocked Google request')

async def connected(s,sessions,google=None):
    g=google or Google();setup=YouTubeSetup(s,sessions);setup.save('owner',0,CLIENT,SECRET)
    oauth=YouTubeOAuth(s,sessions,httpx.MockTransport(g));url,cookie=oauth.start('owner',1);q=parse_qs(urlsplit(url).query)
    assert set(q['scope'][0].split())==SCOPES and q['code_challenge_method']==['S256']
    await oauth.complete(q['state'][0],cookie,'synthetic-code')
    return g,oauth

async def publisher(s,sessions,g=None):
    g,_=await connected(s,sessions,g)
    flags=FeatureFlags(s,sessions)
    for rev,name in enumerate(['youtube_publishing','youtube_shorts','youtube_videos']):flags.save('owner',rev,name,True,acknowledged=True)
    now=[utcnow()]
    def factory(a,execute):return YouTubePublishingConnector(YouTubeConnector(s,sessions,a,httpx.MockTransport(g)),execute)
    service=PublishingService(s,sessions,factory,clock=lambda:now[0])
    asset=service.media.save(Path('tests/fixtures/reel-h264-aac-5s.mp4'),'alice')
    p=await service.prepare('alice','youtube-one',{'account_ids':[ACCOUNT],'content_type':'short','media_ids':[asset['id']],'youtube':META})
    service.approve(p['publication_id'],p['request_hash'],'owner','Publish the exact private video after review')
    async def execute():
        now[0]+=timedelta(seconds=61)
        return await service.execute('alice',['socialhub:publish'],p['publication_id'])
    return service,g,p,asset,execute,now

async def test_oauth_pkce_encrypted_flow_and_credentials(yt_settings,sessions):
    g,oauth=await connected(yt_settings,sessions)
    assert oauth.callback==yt_settings.browser_origin+'/youtube/oauth/callback'
    assert g.token_requests[0]['redirect_uri']==[oauth.callback] and g.token_requests[0]['code_verifier']
    status=YouTubeSetup(yt_settings,sessions).status('owner');assert status['connected'] and status['channel_id']==CID
    with sessions() as db:
        c=db.scalar(select(Credential).where(Credential.account_id==ACCOUNT));assert c.expires_at is None
        assert 'synthetic-refresh' not in c.ciphertext and unpack(yt_settings,c.ciphertext)['refresh_token']=='synthetic-refresh'
        assert not list(db.scalars(select(SiteConfiguration).where(SiteConfiguration.id.like('ytflow:%'))))
    assert SECRET not in json.dumps(status) and 'synthetic-refresh' not in json.dumps(status)

@pytest.mark.parametrize('channels', [[],[OTHER],[CID,OTHER]])
async def test_wrong_or_ambiguous_channel_refused(yt_settings,sessions,channels):
    g=Google();g.channels=channels
    with pytest.raises(HubError,match='allowlisted'):await connected(yt_settings,sessions,g)
    with sessions() as db:assert db.get(Account,ACCOUNT) is None

async def test_state_cookie_replay_config_and_denial(yt_settings,sessions):
    setup=YouTubeSetup(yt_settings,sessions);setup.save('owner',0,CLIENT,SECRET);g=Google();oauth=YouTubeOAuth(yt_settings,sessions,httpx.MockTransport(g))
    url,cookie=oauth.start('owner',1);state=parse_qs(urlsplit(url).query)['state'][0]
    with pytest.raises(HubError):await oauth.complete(state,'wrong-browser-cookie'+'x'*20,'code')
    with pytest.raises(HubError):await oauth.complete(state,cookie,'code',True)
    with pytest.raises(HubError):await oauth.complete(state,cookie,'code')
    assert not g.calls
    url,cookie=oauth.start('owner',1);state=parse_qs(urlsplit(url).query)['state'][0]
    setup.save('owner',1,CLIENT,'different-synthetic-secret')
    with pytest.raises(HubError):await oauth.complete(state,cookie,'code')
    assert not g.calls

@pytest.mark.parametrize('missing',['refresh','scope'])
async def test_missing_offline_grant_or_permissions(yt_settings,sessions,missing):
    g=Google()
    if missing=='refresh':g.refresh=False
    else:g.scope={'https://www.googleapis.com/auth/youtube.upload'}
    with pytest.raises(HubError) as e:await connected(yt_settings,sessions,g)
    assert e.value.code=='youtube_permissions_missing'

async def test_refresh_expiry_revocation_and_disconnect(yt_settings,sessions):
    g,_=await connected(yt_settings,sessions)
    with sessions.begin() as db:
        c=db.scalar(select(Credential).where(Credential.account_id==ACCOUNT));d=unpack(yt_settings,c.ciphertext);d['expires_at']=0;c.ciphertext=vault(yt_settings).encrypt(d)
    await access_token(yt_settings,sessions,httpx.MockTransport(g));assert g.token_requests[-1]['grant_type']==['refresh_token']
    with sessions.begin() as db:
        c=db.scalar(select(Credential).where(Credential.account_id==ACCOUNT));d=unpack(yt_settings,c.ciphertext);d['expires_at']=0;c.ciphertext=vault(yt_settings).encrypt(d)
    g.invalid_refresh=True
    with pytest.raises(HubError) as e:await access_token(yt_settings,sessions,httpx.MockTransport(g))
    assert e.value.code=='youtube_reauthorization_required'
    with sessions() as db:assert db.scalar(select(Credential).where(Credential.account_id==ACCOUNT)).revoked
    setup=YouTubeSetup(yt_settings,sessions)
    with pytest.raises(HubError):setup.disconnect('wife')
    setup.disconnect('owner')
    with sessions() as db:assert not db.get(Account,ACCOUNT).allowlisted and not db.scalar(select(Credential).where(Credential.account_id==ACCOUNT))

async def test_read_library_ownership_and_pagination(yt_settings,sessions):
    g,_=await connected(yt_settings,sessions)
    with sessions() as db:a=db.get(Account,ACCOUNT)
    c=YouTubeConnector(yt_settings,sessions,a,httpx.MockTransport(g));r=await c.list_posts(CID,1,'safe_cursor')
    assert r['next_cursor']=='next_safe' and r['items'][0]['id']==VID
    g.foreign=True
    with pytest.raises(HubError):await c.get_post(CID,VID)
    with pytest.raises(HubError):await c.get_post(OTHER,VID)
    assert all(method=='GET' or path=='/token' for method,path in g.calls)

@pytest.mark.parametrize('visibility',['private','unlisted','public'])
async def test_upload_through_shared_publisher_and_no_duplicate(yt_settings,sessions,visibility):
    service,g,p,asset,execute,now=await publisher(yt_settings,sessions)
    assert p['preview']['request']['youtube']['visibility']=='private'
    if visibility!='private':
        p=await service.prepare('alice','other-visibility',{'account_ids':[ACCOUNT],'content_type':'video','media_ids':[asset['id']],'youtube':dict(META,visibility=visibility)})
        service.approve(p['publication_id'],p['request_hash'],'owner','Publish the exact reviewed visibility')
        async def execute():
            now[0]+=timedelta(seconds=61);return await service.execute('alice',['socialhub:publish'],p['publication_id'])
    before=list(g.calls)
    first=await execute();assert first['targets'][0]['checkpoint']['stage']=='yt_upload' and first['targets'][0]['next_action']=='execute_next_step'
    assert SESSION not in json.dumps(first) and 'synthetic-private-session' not in json.dumps(first)
    ready=await execute();assert ready['targets'][0]['checkpoint']['stage']=='yt_processing'
    now[0]+=timedelta(seconds=31);ready=await service.refresh_status(p['publication_id'],'alice');assert ready['state']=='succeeded' and ready['targets'][0]['result']['privacy_actual']==visibility
    assert g.body['status']=={'privacyStatus':visibility,'selfDeclaredMadeForKids':False,'containsSyntheticMedia':True}
    assert g.uploaded==Path('tests/fixtures/reel-h264-aac-5s.mp4').read_bytes()
    mutations=[c for c in g.calls if c[0]!='GET' and c[1]!='/token'];await execute()
    assert mutations==[c for c in g.calls if c[0]!='GET' and c[1]!='/token']

async def test_lost_final_response_queries_original_session(yt_settings,sessions):
    service,g,p,asset,execute,now=await publisher(yt_settings,sessions);g.lose_final=True
    await execute();uncertain=await execute();assert g.video_exists and uncertain['targets'][0]['checkpoint']['stage']=='yt_upload'
    assert uncertain['targets'][0]['next_action']=='execute_next_step'
    # Status checks are true GET-only and cannot query a session using PUT.
    count=len(g.calls);await service.refresh_status(p['publication_id'],'alice');assert len(g.calls)==count
    # Restart the application object using the original saved DB checkpoint.
    service2=PublishingService(yt_settings,sessions,service.connector_factory,clock=lambda:now[0]);now[0]+=timedelta(minutes=2)
    result=await service2.execute('alice',['socialhub:publish'],p['publication_id'])
    now[0]+=timedelta(seconds=31);result=await service2.refresh_status(p['publication_id'],'alice')
    assert result['state']=='succeeded' and g.uploaded==Path('tests/fixtures/reel-h264-aac-5s.mp4').read_bytes()
    assert sum(method=='POST' and path.endswith('/videos') for method,path in g.calls)==1

async def test_init_timeout_no_new_session_or_replacement(yt_settings,sessions):
    service,g,p,_,execute,_=await publisher(yt_settings,sessions);g.init_timeout=True
    result=await execute();assert result['state']=='attention_required'
    count=len(g.calls);await execute();assert len(g.calls)==count

async def test_crash_at_saved_session_and_disabled_execution(yt_settings,sessions):
    service,g,p,_,execute,now=await publisher(yt_settings,sessions);await execute()
    with sessions.begin() as db:
        t=db.scalar(select(PublicationTarget).where(PublicationTarget.publication_id==p['publication_id']));t.state='uncertain';t.error_code='worker_interrupted'
    result=await execute();now[0]+=timedelta(seconds=31);result=await service.refresh_status(p['publication_id'],'alice');assert result['state']=='succeeded'
    flags=FeatureFlags(yt_settings,sessions);flags.save('owner',3,'youtube_publishing',False)
    count=len(g.calls)
    with pytest.raises(HubError):await execute()
    assert len(g.calls)==count

async def test_feature_defaults_upper_bounds_and_authorization(yt_settings,sessions):
    flags=FeatureFlags(yt_settings,sessions);rows={r['name']:r for r in flags.status('owner')['features']}
    assert rows['youtube_publishing']['available'] and not rows['youtube_publishing']['effective']
    assert not rows['youtube_scheduling']['implemented'] and not rows['youtube_thumbnails']['available']
    with pytest.raises(HubError):flags.save('wife',0,'youtube_publishing',True,acknowledged=True)
    with pytest.raises(HubError):flags.save('owner',0,'youtube_publishing',True)
    yt_settings.youtube_publishing_enabled=False
    with pytest.raises(HubError):flags.save('owner',0,'youtube_publishing',True,acknowledged=True)

@pytest.mark.parametrize('patch',[{'title':''},{'title':'<bad>'},{'description':'x'*5001},{'tags':['x'*501]}, {'visibility':'friends'},{'made_for_kids':None},{'contains_synthetic_media':'yes'},{'publishAt':'tomorrow'}])
def test_metadata_validation(patch):
    with pytest.raises(ValidationError):YouTubeMetadata.model_validate(dict(META,**patch))

@pytest.mark.parametrize('url',['http://www.googleapis.com/upload/youtube/v3/videos?upload_id=x','https://evil.test/upload/youtube/v3/videos?upload_id=x','https://www.googleapis.com.evil.test/upload/youtube/v3/videos?upload_id=x','https://www.googleapis.com/youtube/v3/videos?upload_id=x','https://user:pass@www.googleapis.com/upload/youtube/v3/videos?upload_id=x'])
def test_upload_ssrf_origin_restriction(url):
    with pytest.raises(HubError):session_url(url)

async def test_resumable_chunks_persist_acknowledged_progress(yt_settings,sessions,tmp_path):
    g,_=await connected(yt_settings,sessions)
    with sessions() as db:a=db.get(Account,ACCOUNT)
    c=YouTubePublishingConnector(YouTubeConnector(yt_settings,sessions,a,httpx.MockTransport(g)),True)
    yt_settings.youtube_upload_chunk_bytes=256*1024
    source=tmp_path/'stream-source';source.write_bytes(b'x'*(600*1024))
    import hashlib
    req={'youtube':dict(META,visibility='private'),'_media_path':str(source),'_media_size':source.stat().st_size,'_media_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
    first=await c.step(CID,req,{})
    state=first['checkpoint'];assert state['offset']==0 and state['chunk_bytes']==256*1024
    for expected in [256*1024,512*1024,600*1024]:
        # Simulate object/application replacement between each persisted step.
        c=YouTubePublishingConnector(YouTubeConnector(yt_settings,sessions,a,httpx.MockTransport(g)),True)
        out=await c.step(CID,req,state);state=out['checkpoint'];assert state['offset']==expected
    assert state['stage']=='yt_processing' and g.uploaded==source.read_bytes()
    assert sum(m=='POST' for m,p in g.calls if p!='/token')==1

async def test_expired_session_stays_uncertain_and_never_restarts(yt_settings,sessions):
    service,g,p,_,execute,_=await publisher(yt_settings,sessions);await execute();g.expired_session=True
    r=await execute();assert r['state']=='attention_required' and r['targets'][0]['error']['code']=='youtube_upload_session_expired'
    assert r['targets'][0]['next_action']=='manual_reconciliation'
    count=len(g.calls);await execute();assert len(g.calls)==count
    assert sum(m=='POST' for m,path in g.calls if path!='/token')==1

async def test_processing_pending_then_private_actual_visibility(yt_settings,sessions):
    service,g,p,_,execute,now=await publisher(yt_settings,sessions);g.processed=False
    await execute();await execute();now[0]+=timedelta(minutes=1)
    count=len(g.calls);r=await service.refresh_status(p['publication_id'],'alice')
    assert r['state']=='in_progress' and r['targets'][0]['next_action']=='poll_status'
    assert all(m=='GET' for m,path in g.calls[count:])
    g.processed=True;g.privacy='private';now[0]+=timedelta(minutes=1)
    r=await service.refresh_status(p['publication_id'],'alice');assert r['state']=='succeeded' and not r['targets'][0]['result']['public_visibility_confirmed']

async def test_quotas_and_provider_error_sanitization(yt_settings,sessions):
    service,g,p,_,execute,_=await publisher(yt_settings,sessions);g.quota=True
    r=await execute();assert r['targets'][0]['error']['code']=='youtube_quota_exceeded' and r['state']=='failed'
    assert 'synthetic-access' not in json.dumps(r) and 'synthetic-refresh' not in json.dumps(r)
    with pytest.raises(HubError) as e:
        await YouTubeClient('synthetic-access',httpx.MockTransport(lambda r:httpx.Response(429,json={'error':{'errors':[{'reason':'rateLimitExceeded'}]}},headers={'Retry-After':'120'}))).get('channels',{'mine':'true'})
    assert e.value.retryable and e.value.diagnostics['retry_after']==120

@pytest.mark.parametrize('kind,metadata',[('short',{'width':1280,'height':720,'duration':10}),('short',{'width':720,'height':1280,'duration':181})])
async def test_unsupported_short_media_before_any_upload(yt_settings,sessions,kind,metadata):
    g,_=await connected(yt_settings,sessions)
    with sessions() as db:a=db.get(Account,ACCOUNT)
    c=YouTubePublishingConnector(YouTubeConnector(yt_settings,sessions,a,httpx.MockTransport(g)))
    with pytest.raises(HubError) as e:await c.validate(CID,{'operation':'publish','content_type':kind,'text':'','youtube':META},[{'mime_type':'video/mp4','metadata':dict(metadata,video_codec='h264',audio_codecs=['aac'])}])
    assert e.value.code=='invalid_media' and not any(m=='POST' for m,path in g.calls if path!='/token')

async def test_disclosure_metadata_and_idempotency_bind_preview(yt_settings,sessions):
    service,g,p,asset,execute,now=await publisher(yt_settings,sessions)
    with pytest.raises(HubError) as e:
        await service.prepare('alice','youtube-one',{'account_ids':[ACCOUNT],'content_type':'short','media_ids':[asset['id']],'youtube':dict(META,contains_synthetic_media=False)})
    assert e.value.code=='idempotency_conflict'
    assert p['preview']['youtube_channel']['id']==CID and p['preview']['classification_guaranteed'] is False
    assert p['preview']['expected_short'] is True
    with sessions.begin() as db:
        pub=db.get(Publication,p['publication_id']);payload=dict(pub.payload);payload['request']=dict(payload['request'],youtube=dict(META,visibility='public'));pub.payload=payload
    with pytest.raises(HubError) as e:await execute()
    assert e.value.code=='preview_changed'

async def test_owner_reconciliation_is_get_only_and_checks_ownership(yt_settings,sessions):
    service,g,p,asset,execute,now=await publisher(yt_settings,sessions)
    await execute();g.lose_final=True;await execute()
    with sessions.begin() as db:
        t=db.scalar(select(PublicationTarget).where(PublicationTarget.publication_id==p['publication_id']));t.state='uncertain';tid=t.id
    before=len(g.calls)
    r=await service.reconcile(p['publication_id'],tid,VID,'operator-test',expected_hash=p['request_hash'])
    assert r['state']=='succeeded' and r['targets'][0]['attempts']==2
    assert all(m=='GET' for m,path in g.calls[before:])

async def test_setup_cas_authorization_and_no_secret_echo(yt_settings,sessions):
    setup=YouTubeSetup(yt_settings,sessions)
    with pytest.raises(HubError):setup.save('wife',0,CLIENT,SECRET)
    setup.save('owner',0,CLIENT,SECRET)
    with pytest.raises(HubError):setup.save('owner',0,CLIENT,SECRET)
    setup.save('owner',1,CLIENT,None)
    status=setup.status('owner');assert status['secret_configured'] and SECRET not in json.dumps(status)

async def test_flow_expiry_and_policy_change(yt_settings,sessions):
    setup=YouTubeSetup(yt_settings,sessions);setup.save('owner',0,CLIENT,SECRET)
    g=Google();oauth=YouTubeOAuth(yt_settings,sessions,httpx.MockTransport(g));url,cookie=oauth.start('owner',1);state=parse_qs(urlsplit(url).query)['state'][0]
    with sessions.begin() as db:
        row=db.scalar(select(SiteConfiguration).where(SiteConfiguration.id.like('ytflow:%')));data=unpack(yt_settings,row.ciphertext);data['expires']=0;row.ciphertext=vault(yt_settings).encrypt(data)
    with pytest.raises(HubError):await oauth.complete(state,cookie,'code')
    assert not g.calls


def test_global_upload_lock_and_bounds(tmp_path,yt_settings):
    from app.services.youtube_lock import upload_lock
    with upload_lock(tmp_path):
        with pytest.raises(HubError):
            with upload_lock(tmp_path):pass
    with pytest.raises(ValidationError):Settings.model_validate(dict(yt_settings.model_dump(),youtube_upload_chunk_bytes=123))
    with pytest.raises(ValidationError):Settings.model_validate(dict(yt_settings.model_dump(),youtube_channel_ids=['@PixelPapaMusic']))

@pytest.mark.parametrize('status',[500,503,504])
async def test_lost_initial_session_never_blindly_retries_server_error(yt_settings,sessions,status):
    service,g,p,_,execute,_=await publisher(yt_settings,sessions)
    original=g.__call__
    async def fail(r):
        if r.method=='POST' and r.url.host=='www.googleapis.com':
            g.calls.append((r.method,r.url.path));return httpx.Response(status,json={'error':{'errors':[{'reason':'backendError'}]}})
        return await original(r)
    transport=httpx.MockTransport(fail)
    service.connector_factory=lambda a,execute:YouTubePublishingConnector(YouTubeConnector(yt_settings,sessions,a,transport),execute)
    r=await execute();assert r['state']=='attention_required'
    calls=len(g.calls);await execute();assert len(g.calls)==calls

async def test_disconnect_invalidates_pending_oauth_and_inflight_revision(yt_settings,sessions):
    g,oauth=await connected(yt_settings,sessions)
    url,cookie=oauth.start('owner',1);state=parse_qs(urlsplit(url).query)['state'][0]
    setup=YouTubeSetup(yt_settings,sessions);setup.disconnect('owner')
    assert setup.configuration()[0]==2
    count=len(g.calls)
    with pytest.raises(HubError):await oauth.complete(state,cookie,'synthetic-code')
    assert len(g.calls)==count

async def test_longer_youtube_video_policy_does_not_expand_meta_subset(yt_settings,sessions):
    yt_settings.media_max_video_seconds=7200
    g,_=await connected(yt_settings,sessions)
    with sessions() as db:a=db.get(Account,ACCOUNT)
    yt=YouTubePublishingConnector(YouTubeConnector(yt_settings,sessions,a,httpx.MockTransport(g)))
    assets=[{'mime_type':'video/mp4','size':1000,'metadata':{'validated':True,'width':1280,'height':720,'duration':900,'video_codec':'h264','audio_codecs':['aac']}}]
    await yt.validate(CID,{'operation':'publish','content_type':'video','text':'','youtube':META},assets)
    from app.connectors.meta import GraphClient,FacebookConnector
    from app.connectors.publishing import MetaPublishingConnector
    from app.connectors.meta_policy import PAGE_ID
    fb=MetaPublishingConnector(FacebookConnector(GraphClient('synthetic','v26.0'),PAGE_ID))
    with pytest.raises(HubError) as error:await fb.validate(PAGE_ID,{'operation':'publish','content_type':'video','text':'','media_ids':['asset']},assets)
    assert error.value.code=='invalid_media'


def test_youtube_policy_extension_preserves_existing_meta_grants(yt_settings):
    from app.services.approval_policy import ApprovalPolicy
    policy=ApprovalPolicy(yt_settings)
    meta={'request':{'account_ids':['pixelpapa-facebook']}}
    youtube={'request':{'account_ids':[ACCOUNT]}}
    legacy=policy.fingerprint(meta);yt=policy.fingerprint(youtube)
    yt_settings.youtube_write_account_ids=[]
    assert policy.fingerprint(meta)==legacy
    assert policy.fingerprint(youtube)!=yt
