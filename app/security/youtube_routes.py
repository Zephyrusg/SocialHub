"""Administrator-owned Google OAuth callback, separate from Authelia login."""
import asyncio
from html import escape
from urllib.parse import parse_qs
from starlette.responses import HTMLResponse, RedirectResponse
from app.core import HubError
from app.security.origins import browser_request
from app.services.administration import AdminSessions
from app.services.youtube_oauth import YouTubeSetup,YouTubeOAuth
from app.security.admin_routes import HEADERS,COOKIE,page

FLOW_COOKIE='__Secure-socialhub-youtube'


def register_youtube_routes(mcp,settings,sessions):
    admin=AdminSessions(settings,sessions);setup=YouTubeSetup(settings,sessions)
    async def form(request,fields):
        if not browser_request(request,settings) or request.headers.get('origin')!=settings.browser_origin or request.headers.get('content-type','').split(';')[0]!='application/x-www-form-urlencoded':
            raise HubError('admin_denied','Trusted administrator form required')
        subject=await asyncio.to_thread(admin.authenticate,request.cookies.get(COOKIE))
        data=bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data)>4096:raise HubError('admin_denied','Form too large')
        try:
            values=parse_qs(data.decode(),keep_blank_values=True,strict_parsing=True,max_num_fields=5)
            if set(values)!=fields or any(len(v)!=1 for v in values.values()):raise ValueError()
            values={k:v[0] for k,v in values.items()};admin.check_csrf(request.cookies.get(COOKIE),values['csrf'])
            if 'revision' in values:
                if not values['revision'].isascii() or not values['revision'].isdigit() or len(values['revision'])>10:raise ValueError()
                values['revision']=int(values['revision'])
        except (ValueError,UnicodeError):raise HubError('admin_denied','Invalid administrator form') from None
        return subject,values

    def failure(error):
        code=error.code if isinstance(error,HubError) else 'youtube_unavailable'
        # No provider-supplied error text, URLs, authorization codes or secrets.
        return page('<section class="card">'+escape(code)+': YouTube connection could not complete. Review local settings and select the allowlisted channel.<p><a class="button secondary" href="/admin/connections">Return to Connections</a></p></section>',403 if code=='admin_denied' else 400,section='connections',title='YouTube connection')

    @mcp.custom_route('/admin/youtube/settings',methods=['POST'])
    async def save(request):
        try:
            subject,v=await form(request,{'csrf','revision','client_id','client_secret'})
            await asyncio.to_thread(setup.save,subject,v['revision'],v['client_id'],v['client_secret'] or None)
            return RedirectResponse('/admin/connections?saved=youtube',303,headers=HEADERS)
        except Exception as e:return failure(e)

    @mcp.custom_route('/admin/youtube/connect',methods=['POST'])
    async def connect(request):
        try:
            subject,v=await form(request,{'csrf','revision'})
            url,browser=await asyncio.to_thread(YouTubeOAuth(settings,sessions).start,subject,v['revision'])
            r=page('<section class="card"><h2>Connect YouTube</h2><p>Select your allowlisted YouTube channel or Brand Account in Google.</p><a class="button primary" href="'+escape(url,quote=True)+'">Continue to Google</a></section>',section='connections',title='Connect YouTube')
            r.set_cookie(FLOW_COOKIE,browser,max_age=600,path='/youtube/oauth',secure=True,httponly=True,samesite='lax')
            return r
        except Exception as e:return failure(e)

    @mcp.custom_route('/admin/youtube/disconnect',methods=['POST'])
    async def disconnect(request):
        try:
            subject,_=await form(request,{'csrf'})
            await asyncio.to_thread(setup.disconnect,subject)
            return RedirectResponse('/admin/connections?saved=youtube',303,headers=HEADERS)
        except Exception as e:return failure(e)

    @mcp.custom_route('/youtube/oauth/callback',methods=['GET'])
    async def callback(request):
        try:
            if not browser_request(request,settings) or len(request.query_params.getlist('state'))!=1 or len(request.query_params.getlist('code'))>1:
                raise HubError('invalid_oauth_state','Invalid callback')
            await YouTubeOAuth(settings,sessions).complete(request.query_params.get('state'),request.cookies.get(FLOW_COOKIE),request.query_params.get('code'),denied='error' in request.query_params)
            r=HTMLResponse('YouTube channel verified and connected. This did not enable publishing. Return to SocialHub Connections.',headers=HEADERS)
        except Exception as e:r=failure(e)
        r.delete_cookie(FLOW_COOKIE,path='/youtube/oauth',secure=True,httponly=True,samesite='lax');return r

    @mcp.custom_route('/admin/youtube/avatar',methods=['GET'])
    async def avatar(request):
        from urllib.parse import urlsplit
        from io import BytesIO
        import httpx
        from PIL import Image
        from starlette.responses import Response
        try:
            if not browser_request(request,settings):raise HubError('admin_denied','Trusted origin required')
            subject=await asyncio.to_thread(admin.authenticate,request.cookies.get(COOKIE))
            status=await asyncio.to_thread(setup.status,subject);url=status['avatar'];u=urlsplit(url)
            if (not status['connected'] or u.scheme!='https' or u.hostname not in {'yt3.googleusercontent.com','yt3.ggpht.com'}
                    or u.port not in {None,443} or u.username or u.password or u.fragment):
                return Response(status_code=404,headers=HEADERS)
            data=bytearray()
            async with httpx.AsyncClient(timeout=10,follow_redirects=False) as c:
                async with c.stream('GET',url) as r:
                    if r.status_code!=200:return Response(status_code=404,headers=HEADERS)
                    async for chunk in r.aiter_bytes():
                        data.extend(chunk)
                        if len(data)>128*1024:raise ValueError()
            with Image.open(BytesIO(data)) as image:
                if image.format not in {'JPEG','PNG','WEBP'} or max(image.size)>1024:raise ValueError()
                fmt=image.format;image.verify()
            return Response(bytes(data),media_type={'JPEG':'image/jpeg','PNG':'image/png','WEBP':'image/webp'}[fmt],headers=HEADERS)
        except Exception:return Response(status_code=404,headers=HEADERS)
