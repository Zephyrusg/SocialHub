"""Only these user-authorized Meta identities may be connected."""
PAGE_ID = '1398970469971456'
INSTAGRAM_ID = '17841414669840071'
INSTAGRAM_USERNAME = 'pixelpapamusic'
REQUIRED_READ = {'pages_show_list', 'pages_read_engagement', 'pages_read_user_content', 'instagram_basic'}
REQUESTED_SCOPES = sorted(REQUIRED_READ | {'business_management', 'pages_manage_posts', 'pages_manage_engagement', 'instagram_content_publish', 'instagram_manage_comments'})


def capabilities(platform, scopes, tasks, account_type):
    scopes, tasks = set(scopes), set(tasks)
    manage = bool(tasks & {'MANAGE', 'PROFILE_PLUS_FULL_CONTROL'})
    read_task = manage or bool(tasks & {'ANALYZE', 'MODERATE', 'CREATE_CONTENT', 'PROFILE_PLUS_ANALYZE', 'PROFILE_PLUS_MODERATE', 'PROFILE_PLUS_CREATE_CONTENT'})
    create_task = manage or bool(tasks & {'CREATE_CONTENT', 'PROFILE_PLUS_CREATE_CONTENT'})
    moderate_task = manage or bool(tasks & {'MODERATE', 'PROFILE_PLUS_MODERATE'})
    basic = read_task and 'pages_read_engagement' in scopes
    if platform == 'instagram':
        basic = basic and 'instagram_basic' in scopes
    comments = basic and ('instagram_manage_comments' in scopes if platform == 'instagram' else 'pages_read_user_content' in scopes)
    return {'list_posts': basic, 'get_post': basic, 'list_comments': comments,
            'publish_post': create_task and ('pages_manage_posts' if platform == 'facebook' else 'instagram_content_publish') in scopes,
            'update_post': platform == 'facebook' and create_task and 'pages_manage_posts' in scopes,
            'reply_to_comment': moderate_task and ('pages_manage_engagement' if platform == 'facebook' else 'instagram_manage_comments') in scopes,
            'schedule_post': False, 'analytics': False,
            'stories': platform == 'instagram' and account_type == 'business' and create_task and 'instagram_content_publish' in scopes,
            'live_writes_enabled': False}
