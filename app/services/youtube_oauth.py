"""Independent Google authorization and encrypted channel-bound credentials."""
import base64
import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlencode
import httpx
from sqlalchemy import select, update, delete
from sqlalchemy.exc import IntegrityError
from app.core import HubError
from app.database.models import SiteConfiguration, Account, Credential, Audit, utcnow
from app.security.tokens import TokenVault

SCOPES = {'https://www.googleapis.com/auth/youtube.readonly', 'https://www.googleapis.com/auth/youtube.upload'}
TOKEN_URL = 'https://oauth2.googleapis.com/token'
ACCOUNT = 'pixelpapa-youtube'


def vault(settings):
    return TokenVault([settings.encryption_key.get_secret_value()])


def unpack(settings, ciphertext):
    try:return json.loads(vault(settings).cipher.decrypt(ciphertext.encode()))
    except Exception:raise HubError('configuration_invalid', 'Encrypted YouTube configuration could not be verified') from None


class YouTubeSetup:
    def __init__(self, settings, sessions):self.settings, self.sessions = settings, sessions

    def authorize(self, subject):
        if not self.settings.admin_enabled or subject not in self.settings.admin_allowed_subjects:
            raise HubError('admin_denied', 'SocialHub administrator required')

    def configuration(self):
        with self.sessions() as db:
            row = db.get(SiteConfiguration, 'youtube')
            return (row.revision, unpack(self.settings, row.ciphertext)) if row else (0, {})

    def status(self, subject):
        self.authorize(subject)
        revision, values = self.configuration()
        with self.sessions() as db:
            a = db.get(Account, ACCOUNT);c = db.scalar(select(Credential).where(Credential.account_id == ACCOUNT))
            token = unpack(self.settings, c.ciphertext) if c and not c.revoked else {}
            connected = bool(a and a.allowlisted and not a.is_mock and c and not c.revoked
                and a.remote_id in self.settings.youtube_channel_ids and SCOPES <= set(c.scopes))
            return {'revision':revision, 'client_id':values.get('client_id',''), 'secret_configured':bool(values.get('client_secret')),
                'enabled':self.settings.youtube_enabled, 'connected':connected, 'channel_id':a.remote_id if a else '',
                'name':a.name if a else 'YouTube channel', 'handle':token.get('handle',''), 'avatar':token.get('avatar',''),
                'allowlisted_channels':self.settings.youtube_channel_ids,
                'warning': ('Reconnect: authorization revoked or permissions missing.' if c and not connected else
                    'Access token will refresh automatically; Google may require renewed consent.' if connected and token.get('expires_at',0) <= time.time() else '')}

    def save(self, subject, revision, client_id, client_secret=None):
        self.authorize(subject)
        if (type(revision) is not int or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}\.apps\.googleusercontent\.com',client_id)
                or (client_secret is not None and (not re.fullmatch(r'[A-Za-z0-9_-]{8,256}',client_secret)))):
            raise HubError('invalid_youtube_setup', 'Enter Google Web Application client credentials privately')
        current, values = self.configuration()
        if current != revision:raise HubError('configuration_conflict','YouTube settings changed; reload')
        if values.get('client_id') != client_id and not client_secret:
            raise HubError('invalid_youtube_setup', 'A changed Google client needs its matching secret')
        previous=values
        values = {'client_id':client_id,'client_secret':client_secret or values.get('client_secret','')}
        if values==previous:return current
        if not values['client_secret']:raise HubError('invalid_youtube_setup','A Google client secret is required')
        try:
            with self.sessions.begin() as db:
                row = db.get(SiteConfiguration,'youtube')
                if not row:
                    db.add(SiteConfiguration(id='youtube',revision=0,ciphertext=vault(self.settings).encrypt({})));db.flush()
                changed = db.execute(update(SiteConfiguration).where(SiteConfiguration.id=='youtube',SiteConfiguration.revision==revision)
                    .values(revision=revision+1,ciphertext=vault(self.settings).encrypt(values)))
                if changed.rowcount != 1:raise HubError('configuration_conflict','Concurrent Google settings edit refused')
                db.execute(update(Credential).where(Credential.account_id==ACCOUNT).values(revoked=True))
                db.add(Audit(subject=subject,operation='admin_youtube_setup_updated',outcome='success',details={'revision':revision+1}))
        except IntegrityError:raise HubError('configuration_conflict','Concurrent Google initialization refused') from None

    def disconnect(self, subject):
        self.authorize(subject)
        with self.sessions.begin() as db:
            config=db.scalar(select(SiteConfiguration).where(SiteConfiguration.id=='youtube').with_for_update())
            if config:config.revision+=1  # Invalidate pending/exchanging browser flows as well.
            db.execute(delete(SiteConfiguration).where(SiteConfiguration.id.like('ytflow:%')))
            db.execute(delete(Credential).where(Credential.account_id==ACCOUNT))
            a=db.get(Account,ACCOUNT)
            if a:a.allowlisted=False
            db.add(Audit(subject=subject,operation='admin_youtube_disconnect',outcome='success',details={}))
        # Local disconnect deliberately does not revoke Google's grant, which
        # might affect other clients/channels belonging to this project.


async def token_request(values, transport=None):
    try:
        async with httpx.AsyncClient(transport=transport,timeout=15,follow_redirects=False) as client:
            async with client.stream('POST',TOKEN_URL,data=values) as r:
                body=bytearray()
                async for chunk in r.aiter_bytes():
                    body.extend(chunk)
                    if len(body)>64*1024:raise ValueError()
                data=json.loads(body)
        if r.status_code != 200 or not isinstance(data,dict) or not isinstance(data.get('access_token'),str):
            raise HubError('youtube_reauthorization_required' if data.get('error') in {'invalid_grant','invalid_client'} else 'youtube_oauth_failed',
                'Google authorization failed; reconnect locally without sharing credentials')
        if (not 1 <= int(data.get('expires_in',0)) <= 86400
                or not 1 <= len(data['access_token']) <= 8192 or any(ord(c)<33 or ord(c)>126 for c in data['access_token'])
                or ('refresh_token' in data and (not isinstance(data['refresh_token'],str) or not 1<=len(data['refresh_token'])<=8192))):raise ValueError()
        return data
    except HubError:raise
    except Exception:raise HubError('youtube_oauth_unavailable','Google token exchange unavailable; start a new connection',True,diagnostics={'mutation_attempted':False}) from None


class YouTubeOAuth:
    def __init__(self, settings, sessions, transport=None):
        self.settings,self.sessions,self.transport=settings,sessions,transport
        self.setup=YouTubeSetup(settings,sessions)

    @property
    def callback(self):return self.settings.browser_origin+'/youtube/oauth/callback'

    def policy(self):
        return hashlib.sha256(json.dumps([self.settings.browser_origin, sorted(self.settings.admin_allowed_subjects),
            sorted(self.settings.youtube_channel_ids)],sort_keys=True).encode()).hexdigest()

    def start(self, subject, revision):
        self.setup.authorize(subject)
        current,config=self.setup.configuration()
        if not self.settings.youtube_enabled or not self.settings.youtube_channel_ids:
            raise HubError('youtube_disabled','Configure the exact channel allowlist before connecting')
        if revision != current or not config.get('client_secret'):raise HubError('configuration_conflict','Save Google configuration and reload')
        state,browser,verifier=(secrets.token_urlsafe(32) for _ in range(3))
        key='ytflow:'+hashlib.sha256(state.encode()).hexdigest()[:24]
        payload={'state':hashlib.sha256(state.encode()).hexdigest(),'browser':hashlib.sha256(browser.encode()).hexdigest(),
            'verifier':verifier,'subject':subject,'revision':revision,'policy':self.policy(),'expires':time.time()+600}
        with self.sessions.begin() as db:
            # Bounded encrypted flow storage with opportunistic expiry cleanup.
            for row in db.scalars(select(SiteConfiguration).where(SiteConfiguration.id.like('ytflow:%'))):
                if unpack(self.settings,row.ciphertext)['expires'] < time.time():db.delete(row)
            if len(list(db.scalars(select(SiteConfiguration.id).where(SiteConfiguration.id.like('ytflow:%'))))) >= 16:
                raise HubError('rate_limited','Too many pending YouTube logins')
            db.add(SiteConfiguration(id=key,revision=0,ciphertext=vault(self.settings).encrypt(payload)))
        query={'client_id':config['client_id'],'redirect_uri':self.callback,'response_type':'code','scope':' '.join(sorted(SCOPES)),
            'state':state,'access_type':'offline','prompt':'consent select_account','code_challenge_method':'S256',
            'code_challenge':base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')}
        return 'https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(query),browser

    async def complete(self, state, browser, code, denied=False):
        if not all(isinstance(v,str) and 20 <= len(v) <= 1024 for v in [state,browser]):
            raise HubError('invalid_oauth_state','Start a new YouTube connection')
        key='ytflow:'+hashlib.sha256(state.encode()).hexdigest()[:24]
        with self.sessions.begin() as db:
            row=db.scalar(select(SiteConfiguration).where(SiteConfiguration.id==key).with_for_update())
            if not row:raise HubError('invalid_oauth_state','Unknown or consumed Google login')
            flow=unpack(self.settings,row.ciphertext)
            if (flow['expires'] < time.time() or not secrets.compare_digest(flow['state'],hashlib.sha256(state.encode()).hexdigest())
                    or not secrets.compare_digest(flow['browser'],hashlib.sha256(browser.encode()).hexdigest()) or flow['policy']!=self.policy()):
                raise HubError('invalid_oauth_state','Expired or mismatched Google login')
            if db.execute(delete(SiteConfiguration).where(SiteConfiguration.id==key,SiteConfiguration.revision==row.revision)).rowcount!=1:
                raise HubError('invalid_oauth_state','Consumed Google login')
        self.setup.authorize(flow['subject'])
        revision,config=self.setup.configuration()
        if not self.settings.youtube_enabled or flow['revision']!=revision or denied or not isinstance(code,str) or not 1 <= len(code) <= 2048:
            raise HubError('youtube_oauth_failed','Login denied or configuration changed; reconnect')
        tokens=await token_request({'client_id':config['client_id'],'client_secret':config['client_secret'],'code':code,
            'code_verifier':flow['verifier'],'grant_type':'authorization_code','redirect_uri':self.callback},self.transport)
        if not SCOPES <= set(tokens.get('scope','').split()) or not tokens.get('refresh_token'):
            raise HubError('youtube_permissions_missing','Both YouTube read/upload scopes and offline consent are required; reconnect')
        from app.connectors.youtube import YouTubeClient
        channels=await YouTubeClient(tokens['access_token'],transport=self.transport).get('channels',{'part':'snippet,contentDetails','mine':'true','maxResults':50})
        items=channels.get('items',[])
        # No guessing by display name/email. Google/Brand Account selection must
        # yield exactly one channel, matching the deployment's real channel ID.
        if len(items)!=1 or channels.get('nextPageToken') or items[0].get('id') not in self.settings.youtube_channel_ids:
            raise HubError('youtube_channel_denied','Select the allowlisted YouTube/Brand channel and reconnect')
        channel=items[0];snippet=channel.get('snippet',{});cid=channel['id']
        saved={'access_token':tokens['access_token'],'refresh_token':tokens['refresh_token'],'expires_at':time.time()+int(tokens['expires_in']),
            'channel_id':cid,'uploads_playlist':channel.get('contentDetails',{}).get('relatedPlaylists',{}).get('uploads'),
            'handle':snippet.get('customUrl',''),'avatar':snippet.get('thumbnails',{}).get('default',{}).get('url',''),'configuration_revision':revision}
        with self.sessions.begin() as db:
            configrow=db.scalar(select(SiteConfiguration).where(SiteConfiguration.id=='youtube').with_for_update())
            if not configrow or configrow.revision!=revision:raise HubError('configuration_conflict','Google client changed during login')
            self.setup.authorize(flow['subject'])
            a=db.get(Account,ACCOUNT)
            if a and a.remote_id!=cid:raise HubError('youtube_channel_denied','Disconnect the previous channel before changing identity')
            if not a:a=Account(id=ACCOUNT,platform='youtube',remote_id=cid,name=snippet.get('title','YouTube'),account_type='channel',allowlisted=True,is_mock=False);db.add(a)
            else:a.name=snippet.get('title','YouTube');a.allowlisted=True
            db.flush();c=db.scalar(select(Credential).where(Credential.account_id==ACCOUNT))
            if not c:c=Credential(account_id=ACCOUNT);db.add(c)
            c.ciphertext=vault(self.settings).encrypt(saved);c.scopes=sorted(SCOPES);c.revoked=False;c.expires_at=None;c.refreshed_at=utcnow()
            db.add(Audit(subject=flow['subject'],operation='youtube_connected',account_id=ACCOUNT,outcome='success',details={}))
        return cid


async def access_token(settings,sessions,transport=None):
    """Serialize refresh with client edits/disconnect; never log provider responses."""
    setup=YouTubeSetup(settings,sessions)
    failure=None
    with sessions.begin() as db:
        configrow=db.scalar(select(SiteConfiguration).where(SiteConfiguration.id=='youtube').with_for_update())
        c=db.scalar(select(Credential).where(Credential.account_id==ACCOUNT).with_for_update())
        if not configrow or not c or c.revoked or not SCOPES <= set(c.scopes):
            raise HubError('youtube_reauthorization_required','Connect the allowlisted YouTube channel')
        data=unpack(settings,c.ciphertext);config=unpack(settings,configrow.ciphertext)
        if data.get('channel_id') not in settings.youtube_channel_ids or data.get('configuration_revision')!=configrow.revision:
            raise HubError('youtube_channel_denied','YouTube identity or client configuration changed')
        if data['expires_at'] <= time.time()+60:
            try:
                fresh=await token_request({'grant_type':'refresh_token','refresh_token':data['refresh_token'],
                    'client_id':config['client_id'],'client_secret':config['client_secret']},transport)
                if 'scope' in fresh and not SCOPES <= set(fresh['scope'].split()):raise HubError('youtube_reauthorization_required','Google permissions changed')
                data.update(access_token=fresh['access_token'],expires_at=time.time()+int(fresh['expires_in']))
                if fresh.get('refresh_token'):data['refresh_token']=fresh['refresh_token']
                c.ciphertext=vault(settings).encrypt(data);c.refreshed_at=utcnow()
            except HubError as error:
                if error.code=='youtube_reauthorization_required':c.revoked=True
                failure=error
    if failure:raise failure
    return data
