"""Bounded YouTube Data API reads and channel-bound resumable uploads."""
import asyncio
import re
from urllib.parse import urlsplit,parse_qs
from pathlib import Path
import httpx
from app.core import HubError
from app.services.youtube_oauth import access_token, vault, unpack

API='https://www.googleapis.com/youtube/v3/'
UPLOAD='https://www.googleapis.com/upload/youtube/v3/videos'
VIDEO=re.compile(r'[A-Za-z0-9_-]{11}')


def capabilities():
    return {'list_posts':True,'get_post':True,'publish_post':True,'update_post':False,
        'reply_to_comment':False,'list_comments':False,'delete_post':False,'analytics':False}


def session_url(value):
    try:
        u=urlsplit(value)
        if (u.scheme!='https' or u.hostname!='www.googleapis.com' or u.port not in {None,443}
                or u.username or u.password or u.fragment or '\\' in value
                or u.path not in {'/upload/youtube/v3/videos','/resumable/upload/youtube/v3/videos'}
                or any(ord(c)<33 or ord(c)==127 for c in value) or not parse_qs(u.query).get('upload_id') or len(value)>8192):raise ValueError()
        return value
    except (TypeError,ValueError):raise HubError('invalid_api_response','Untrusted Google upload session; no bytes sent') from None


class YouTubeClient:
    def __init__(self,token,transport=None,allow_writes=False):self.token,self.transport,self.allow_writes=token,transport,allow_writes

    async def request(self,method,url,*,params=None,json_body=None,content=None,headers=None,session=False):
        if method!='GET' and not self.allow_writes:raise HubError('live_writes_disabled','YouTube writes are disabled')
        if session:session_url(url)
        elif not url.startswith(API) and url!=UPLOAD:raise HubError('account_denied','Unsupported Google endpoint')
        try:
            async with httpx.AsyncClient(transport=self.transport,timeout=httpx.Timeout(120,connect=10,write=60,pool=10),follow_redirects=False) as c:
                async with c.stream(method,url,params=params,json=json_body,content=content,
                        headers={'Authorization':'Bearer '+self.token,**(headers or {})}) as r:
                    body=bytearray()
                    async for chunk in r.aiter_bytes():
                        body.extend(chunk)
                        if len(body)>1024*1024:raise HubError('invalid_api_response','Google response exceeded safe bounds')
                    import json
                    try:data=json.loads(body) if body else {}
                    except ValueError:raise HubError('invalid_api_response','Google returned invalid JSON') from None
                    if r.status_code not in {200,201,308}:
                        reason=''
                        if isinstance(data,dict):reason=next(iter(data.get('error',{}).get('errors',[])),{}).get('reason','') if isinstance(data.get('error'),dict) else ''
                        retry=r.status_code in {429,500,502,503,504} or reason in {'rateLimitExceeded','userRateLimitExceeded'}
                        code='youtube_quota_exceeded' if reason in {'quotaExceeded','dailyLimitExceeded','uploadLimitExceeded'} else 'api_unavailable' if r.status_code>=500 else 'rate_limited' if retry else 'youtube_reauthorization_required' if r.status_code==401 else 'youtube_api_error'
                        raise HubError(code,'YouTube request rejected; inspect quota, consent, channel permissions or the existing upload',retry,
                            diagnostics={'mutation_attempted':method!='GET','http_status':r.status_code,'retry_after':min(max(int(r.headers.get('retry-after','60')) if r.headers.get('retry-after','60').isdigit() else 60,1),86400)})
                    if not isinstance(data,dict):raise HubError('invalid_api_response','Google response must be an object')
                    return r.status_code,dict(r.headers),data
        except HubError:raise
        except (httpx.TimeoutException,httpx.NetworkError):
            raise HubError('api_timeout','YouTube outcome unknown; query the original upload session before sending bytes',True,
                diagnostics={'mutation_attempted':method!='GET'}) from None

    async def get(self,edge,params):
        if edge not in {'channels','videos','playlistItems'}:raise HubError('unsupported_operation','YouTube read endpoint unsupported')
        return (await self.request('GET',API+edge,params=params))[2]


class YouTubeConnector:
    platform='youtube'
    def __init__(self,settings,sessions,account,transport=None):
        self.settings,self.sessions,self.account,self.transport=settings,sessions,account,transport

    async def client(self,writes=False):
        data=await access_token(self.settings,self.sessions,self.transport)
        return YouTubeClient(data['access_token'],self.transport,writes)

    def check(self,remote):
        if not self.settings.youtube_enabled or remote!=self.account.remote_id or remote not in self.settings.youtube_channel_ids:
            raise HubError('account_denied','YouTube channel is not authorized')

    async def channel(self,remote_id):
        self.check(remote_id);c=await self.client()
        result=await c.get('channels',{'part':'snippet,contentDetails,statistics','mine':'true','maxResults':50})
        items=result.get('items',[])
        if result.get('nextPageToken') or len(items)!=1 or items[0].get('id')!=remote_id:
            raise HubError('youtube_channel_denied','Google did not return the exact authorized channel')
        return items[0]

    async def get_post(self,remote_id,post_id):
        self.check(remote_id)
        if not VIDEO.fullmatch(post_id):raise HubError('post_not_found','Invalid YouTube video ID')
        c=await self.client();r=await c.get('videos',{'part':'snippet,status,contentDetails,processingDetails','id':post_id})
        items=r.get('items',[])
        if len(items)!=1 or items[0].get('id')!=post_id or items[0].get('snippet',{}).get('channelId')!=remote_id:
            raise HubError('post_not_found','Video does not belong to the allowlisted channel')
        return dict(items[0],permalink_url='https://www.youtube.com/watch?v='+post_id)

    async def list_posts(self,remote_id,limit=25,cursor=None):
        self.check(remote_id)
        if not 1<=limit<=50 or (cursor and (len(cursor)>512 or not re.fullmatch('[A-Za-z0-9_=-]+',cursor))):
            raise HubError('invalid_request','Invalid YouTube pagination')
        c=await self.client();data=await access_token(self.settings,self.sessions,self.transport)
        playlist=data.get('uploads_playlist')
        if not playlist:raise HubError('youtube_reauthorization_required','Reconnect to verify the uploads playlist')
        params={'part':'contentDetails','playlistId':playlist,'maxResults':limit}
        if cursor:params['pageToken']=cursor
        page=await c.get('playlistItems',params);ids=[p.get('contentDetails',{}).get('videoId') for p in page.get('items',[])]
        if any(not isinstance(i,str) or not VIDEO.fullmatch(i) for i in ids):raise HubError('invalid_api_response','Invalid YouTube library response')
        items=(await c.get('videos',{'part':'snippet,status,contentDetails','id':','.join(ids)})).get('items',[]) if ids else []
        if any(p.get('snippet',{}).get('channelId')!=remote_id for p in items):raise HubError('account_denied','Unowned video in library')
        return {'items':[dict(p,permalink_url='https://www.youtube.com/watch?v='+p['id']) for p in items], 'next_cursor':page.get('nextPageToken')}


class YouTubePublishingConnector:
    platform='youtube'
    def __init__(self,connector,execute=False):self.connector,self.execute_enabled=connector,execute

    async def validate(self,account_id,request,assets):
        self.connector.check(account_id)
        if request['operation']!='publish' or request['content_type'] not in {'video','short'} or not request.get('youtube') or request.get('text'):
            raise HubError('unsupported_operation','YouTube requires structured video metadata, not a caption/edit/reply')
        c=await self.connector.client();identity=await c.get('channels',{'part':'id','mine':'true','maxResults':50})
        if identity.get('nextPageToken') or [a.get('id') for a in identity.get('items',[])] != [account_id]:
            raise HubError('youtube_channel_denied','The Google token no longer identifies the reviewed channel')
        a=assets[0];m=a['metadata']
        if a['mime_type']!='video/mp4' or m.get('video_codec')!='h264' or set(m.get('audio_codecs',[]))-{'aac'}:
            raise HubError('invalid_media','YouTube requires validated H.264 MP4 with optional AAC audio')
        if request['content_type']=='short' and not (m['width']<=m['height'] and 0<m['duration']<=180):
            raise HubError('invalid_media','Expected Shorts must be square/vertical and no longer than three minutes; classification belongs to YouTube')
        if request['youtube'].get('thumbnail_media_id'):
            raise HubError('unsupported_operation','Custom thumbnail uploads are not implemented; omit the thumbnail')

    async def refresh_delivery(self,account_id,request,checkpoint):
        if checkpoint.get('stage') not in {'yt_processing','delivered'} or not checkpoint.get('remote_id'):
            raise HubError('manual_check_required','No acknowledged YouTube video ID; query the original session through execute_next_step')
        p=await self.connector.get_post(account_id,checkpoint['remote_id']);state=dict(checkpoint)
        upload=p.get('status',{}).get('uploadStatus');processing=p.get('processingDetails',{}).get('processingStatus')
        if upload in {'failed','rejected','deleted'} or processing in {'failed','terminated'}:
            raise HubError('media_processing_failed','YouTube processing failed; do not upload a replacement automatically')
        if upload!='processed' or processing not in {None,'succeeded'}:
            return {'state':'processing','checkpoint':state,'retry_after':30}
        state['stage']='delivered'
        return {'state':'succeeded','checkpoint':state,'result':{'remote_id':p['id'],'permalink':p['permalink_url'],
            'privacy_requested':request['youtube']['visibility'],'privacy_actual':p.get('status',{}).get('privacyStatus'),
            'public_visibility_confirmed':p.get('status',{}).get('privacyStatus')=='public','processing_confirmed':True,
            'thumbnail_pending':bool(request['youtube'].get('thumbnail_media_id')) and not state.get('thumbnail_done')}}

    async def step(self,account_id,request,checkpoint):
        self.connector.check(account_id)
        if checkpoint.get('stage') in {'yt_processing','delivered'}:return await self.refresh_delivery(account_id,request,checkpoint)
        if not self.execute_enabled:raise HubError('live_writes_disabled','YouTube writes disabled')
        c=await self.connector.client(writes=True);state=dict(checkpoint);size=request['_media_size']
        if not state:
            meta=request['youtube'];body={'snippet':{'title':meta['title'],'description':meta['description'],'tags':meta['tags'],'categoryId':'10'},
                'status':{'privacyStatus':meta['visibility'],'selfDeclaredMadeForKids':meta['made_for_kids'],'containsSyntheticMedia':meta['contains_synthetic_media']}}
            _,headers,_=await c.request('POST',UPLOAD,params={'uploadType':'resumable','part':'snippet,status'},json_body=body,
                headers={'X-Upload-Content-Type':'video/mp4','X-Upload-Content-Length':str(size)})
            location=session_url(headers.get('location'))
            return {'state':'processing','checkpoint':{'stage':'yt_upload','session':vault(self.connector.settings).encrypt({'url':location}),
                'offset':0,'size':size,'chunk_bytes':self.connector.settings.youtube_upload_chunk_bytes,'sha256':request['_media_sha256']},'retry_after':0}
        if state.get('stage')!='yt_upload' or state.get('sha256')!=request['_media_sha256'] or state.get('size')!=size:
            raise HubError('manual_check_required','Saved upload does not match the immutable source; no new session will be created')
        url=session_url(unpack(self.connector.settings,state['session'])['url'])
        # Always recover server acknowledgement first. This PUT is an execution
        # operation (not exposed through a read-only MCP status tool).
        try:
            status,headers,data=await c.request('PUT',url,session=True,content=b'',headers={'Content-Length':'0','Content-Range':f'bytes */{size}'})
            if status in {200,201}:return self.completed(data,state)
            offset=self.offset(headers,size)
            if offset<int(state.get('offset',0)):raise HubError('invalid_api_response','YouTube acknowledged byte progress regressed')
            state['offset']=offset
            wait=headers.get('retry-after','0')
            if wait.isdigit() and int(wait)>0:return {'state':'processing','checkpoint':state,'retry_after':min(int(wait),3600)}
            if offset>=size:raise HubError('write_outcome_unknown','All bytes acknowledged but video ID missing; inspect original session')
            end=min(offset+state['chunk_bytes'],size)
            path=Path(request['_media_path'])
            async def chunks():
                with path.open('rb') as f:
                    f.seek(offset);remaining=end-offset
                    while remaining:
                        chunk=await asyncio.to_thread(f.read,min(64*1024,remaining))
                        if not chunk:raise HubError('invalid_media','Media was truncated during upload')
                        remaining-=len(chunk);yield chunk
            status,headers,data=await c.request('PUT',url,session=True,content=chunks(),headers={
                'Content-Type':'video/mp4','Content-Length':str(end-offset),'Content-Range':f'bytes {offset}-{end-1}/{size}'})
            if status in {200,201}:return self.completed(data,state)
            acknowledged=self.offset(headers,size)
            if not offset<=acknowledged<=end:raise HubError('invalid_api_response','Invalid acknowledged upload range')
            state['offset']=acknowledged
            return {'state':'processing','checkpoint':state,'retry_after':0}
        except HubError as e:
            if e.code in {'api_timeout','api_unavailable','rate_limited','invalid_api_response'} or (e.code=='youtube_api_error' and e.diagnostics.get('http_status') in {500,502,503,504}):
                # Retain the ORIGINAL session even after a lost final response.
                # A future explicit execute queries it before resuming any bytes.
                return {'state':'processing','error_code':e.code,'error_message':'Query the saved YouTube upload session before continuing; never create a replacement',
                    'retry_after':max(min(5*2**min(int(state.get('failures',0)),6),300),e.diagnostics.get('retry_after',0)),'checkpoint':dict(state,failures=min(int(state.get('failures',0))+1,20))}
            if e.code=='youtube_api_error' and e.diagnostics.get('http_status') in {404,410}:
                return {'state':'uncertain','checkpoint':dict(state,session_expired=True),
                    'error_code':'youtube_upload_session_expired','error_message':'Saved upload session expired; reconcile the existing outcome manually before any replacement'}
            raise

    @staticmethod
    def offset(headers,size):
        value=headers.get('range')
        if value is None:return 0
        match=re.fullmatch(r'bytes=0-([0-9]+)',value)
        if not match or not 0<=int(match[1])<size:raise HubError('invalid_api_response','Invalid acknowledged Google byte range')
        return int(match[1])+1

    @staticmethod
    def completed(data,state):
        rid=data.get('id')
        if not isinstance(rid,str) or not VIDEO.fullmatch(rid):raise HubError('write_outcome_unknown','Upload response lacks a video ID; retain the original session')
        # Session is no longer needed once the final response is acknowledged.
        return {'state':'processing','checkpoint':{'stage':'yt_processing','remote_id':rid,'offset':state['size'],'size':state['size']},'retry_after':15}


    @staticmethod
    def permalink(info):return info.get('permalink_url')

    async def verify_reconciliation(self,account_id,request,remote_id,started,asset):
        from datetime import datetime,timezone,timedelta
        from app.database.models import utcnow
        p=await self.connector.get_post(account_id,remote_id)
        c=await self.connector.client()
        details=await c.get('videos',{'id':remote_id,'part':'fileDetails'})
        items=details.get('items',[]);meta=request['youtube'];snippet=p.get('snippet',{});status=p.get('status',{})
        try:stamp=datetime.fromisoformat(snippet['publishedAt'].replace('Z','+00:00')).astimezone(timezone.utc).replace(tzinfo=None)
        except (KeyError,TypeError,ValueError):raise HubError('reconciliation_denied','YouTube video has no usable upload timestamp') from None
        if (len(items)!=1 or items[0].get('id')!=remote_id or str(items[0].get('fileDetails',{}).get('fileSize'))!=str(asset['size'])
                or snippet.get('title')!=meta['title'] or snippet.get('description','')!=meta['description'] or snippet.get('tags',[])!=meta['tags']
                or status.get('selfDeclaredMadeForKids')!=meta['made_for_kids'] or status.get('containsSyntheticMedia')!=meta['contains_synthetic_media']
                or stamp<started-timedelta(minutes=2) or stamp>utcnow()+timedelta(minutes=5) or status.get('uploadStatus')!='processed'):
            raise HubError('reconciliation_denied','Owner-selected YouTube video does not match the exact metadata, file size and execution time')
        return p
