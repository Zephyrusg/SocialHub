"""Durable previews and approval-bound, independently checkpointed publication targets."""
import hashlib
import json
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from app.core import HubError
from app.database.models import Publication, PublicationTarget, PublicationApproval, PublicationAsset, Account, Credential, Audit, Post, utcnow
from app.models_publication import PublicationRequest
from app.services.media_manager import MediaManager
from app.security.tokens import TokenVault
from app.connectors.meta import GraphClient, FacebookConnector, InstagramConnector
from app.connectors.meta_policy import capabilities
from app.connectors.publishing import MetaPublishingConnector
from app.connectors.youtube import YouTubePublishingConnector
from app.connectors.reels import REEL_READ_STAGES, REEL_UPLOAD_STAGES, REEL_READY_STAGES
from app.services.approval_policy import ApprovalPolicy
from app.services.write_access import require_write_accounts
from app.services.administration import require_publisher, effective_settings


def content_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


class PublishingService:
    def __init__(self, settings, sessions, connector_factory=None, clock=utcnow):
        self.settings, self.sessions, self.clock = settings, sessions, clock
        self.media = MediaManager(settings, sessions)
        self.connector_factory = connector_factory
        self.approval_policy = ApprovalPolicy(settings)

    def context(self, account_id, execute=False):
        if execute:
            require_write_accounts(self.settings, [account_id])
        from app.services.hub import Hub
        active = effective_settings(self.settings, self.sessions)
        if execute:
            if not active.publishing_enabled or (account_id != 'pixelpapa-youtube' and not active.meta_live_writes):
                raise HubError('live_writes_disabled', 'Live publishing is paused')
            self.require_platforms(active, {'account_ids':[account_id]})
        with self.sessions() as db:
            account = Hub(self.sessions, active).account(db, account_id)
            if account.is_mock:
                raise HubError('account_denied', 'The durable publisher requires explicitly connected real-account identities')
            credential = db.scalar(select(Credential).where(Credential.account_id == account_id))
            if not credential:
                raise HubError('meta_not_connected', 'Connect and verify the account before preparing content')
            if account.platform == 'youtube':
                from app.connectors.youtube import YouTubeConnector, YouTubePublishingConnector, capabilities as youtube_capabilities
                if credential.revoked:raise HubError('youtube_reauthorization_required','Reconnect the YouTube channel')
                adapter = self.connector_factory(account,execute) if self.connector_factory else YouTubePublishingConnector(YouTubeConnector(active,self.sessions,account),execute)
                return account,youtube_capabilities(),adapter
            token = TokenVault([active.encryption_key.get_secret_value()]).decrypt(credential)
            caps = capabilities(account.platform, credential.scopes, token.get('tasks', []), account.account_type)
            if self.connector_factory:
                adapter = self.connector_factory(account, execute)
            else:
                graph = GraphClient(token['access_token'], active.meta_graph_version,
                    app_secret=active.meta_app_secret.get_secret_value(), allow_writes=execute and active.meta_live_writes and active.publishing_enabled)
                connector = (FacebookConnector if account.platform == 'facebook' else InstagramConnector)(graph, account.remote_id)
                adapter = MetaPublishingConnector(connector)
            return account, caps, adapter

    async def prepare(self, principal, idempotency_key, request):
        require_publisher(self.settings, principal)
        if not principal or len(principal) > 128 or not 1 <= len(idempotency_key) <= 128:
            raise HubError('invalid_request', 'Principal and bounded idempotency key are required')
        if not isinstance(request, PublicationRequest):
            request = PublicationRequest.model_validate(request)
        data = request.model_dump()
        if data.get('youtube') is None:data.pop('youtube',None)
        if data['content_type'] == 'reel':
            if data['is_ai_generated'] is None:
                data['is_ai_generated'] = self.settings.reels_ai_generated_default
        else:
            # Preserve historical text/image/video request hashes and semantics.
            data.pop('is_ai_generated')
        data['account_ids'] = sorted(data['account_ids'])
        require_write_accounts(self.settings, data['account_ids'])
        mids=list(data['media_ids'])
        if data.get('youtube',{}).get('thumbnail_media_id'):mids.append(data['youtube']['thumbnail_media_id'])
        assets = [self.media.get(mid, principal) for mid in mids]
        for asset in assets:
            if not asset['metadata'].get('validated'):
                raise HubError('invalid_media', 'Upload media through the validated ingestion workflow')
            self.media.path(asset['id'], asset['sha256'])
        for aid in data['account_ids']:
            account, caps, adapter = self.context(aid)
            await adapter.validate(account.remote_id, data, assets)
            operation = {'publish': 'publish_post', 'update': 'update_post', 'reply': 'reply_to_comment'}[data['operation']]
            if not caps.get(operation):
                raise HubError('unsupported_operation', 'Account permissions/tasks do not support the requested operation')
        payload = {'request': data, 'assets': assets}
        if data.get('youtube'):
            payload['youtube_channel']={'id':account.remote_id,'name':account.name}
            payload['expected_short']=bool(assets[0]['metadata']['width']<=assets[0]['metadata']['height'] and assets[0]['metadata']['duration']<=180)
            payload['classification_guaranteed']=False
        digest = content_hash(payload)
        try:
            with self.sessions.begin() as db:
                existing = db.scalar(select(Publication).where(Publication.principal == principal, Publication.idempotency_key == idempotency_key))
                if existing:
                    if existing.request_hash != digest:
                        raise HubError('idempotency_conflict', 'Key is bound to a different immutable publication preview')
                    pid = existing.id
                else:
                    item = Publication(principal=principal, idempotency_key=idempotency_key, request_hash=digest, payload=payload)
                    db.add(item)
                    db.flush()
                    pid = item.id
                    for aid in data['account_ids']:
                        db.add(PublicationTarget(publication_id=pid, account_id=aid))
                    for asset in assets:
                        db.add(PublicationAsset(publication_id=pid, media_id=asset['id']))
                    db.add(Audit(subject=principal, operation='prepare_publication', outcome='success', details={'publication_id': pid, 'request_hash': digest}))
        except IntegrityError:
            with self.sessions() as db:
                existing = db.scalar(select(Publication).where(Publication.principal == principal, Publication.idempotency_key == idempotency_key))
                if not existing or existing.request_hash != digest:
                    raise HubError('idempotency_conflict', 'Concurrent preparation conflicts with this key') from None
                pid = existing.id
        return self.review(pid, principal)

    def checked(self, db, publication_id, principal):
        item = db.get(Publication, publication_id)
        if not item or (principal is not None and item.principal != principal):
            raise HubError('publication_denied', 'Publication is not owned by this authenticated principal')
        if content_hash(item.payload) != item.request_hash:
            raise HubError('preview_changed', 'Stored preview changed after preparation; execution is refused')
        return item

    def media_write_allowed(self, request, active=None):
        active = active or effective_settings(self.settings, self.sessions)
        if not request['media_ids']:
            return True
        for aid in request['account_ids']:
            if aid == 'pixelpapa-youtube':
                if not active.youtube_publishing_enabled or not getattr(active,'youtube_shorts_enabled' if request['content_type']=='short' else 'youtube_videos_enabled'):
                    return False
                if request.get('youtube',{}).get('thumbnail_media_id'):return False
            if aid == 'pixelpapa-facebook':
                if not active.facebook_media_publishing_enabled or request['content_type'] not in active.facebook_media_allowed_types:
                    return False
                if request['content_type'] != 'reel' and not active.media_delivery_enabled:
                    return False
            if aid == 'pixelpapa-instagram':
                # Adding Instagram to the account allowlist must not activate images too.
                if request['content_type'] != 'reel' or not active.instagram_reels_publishing_enabled:
                    return False
        return True

    @staticmethod
    def refreshable(target, request):
        stage = target['checkpoint'].get('stage')
        if request.get('youtube'):
            return target['state'] in {'processing','retryable','uncertain'} and stage in {'yt_processing','delivered'}
        if target['state'] in {'processing', 'retryable'} and stage in {'delivered', 'video_processing'}:
            return True
        return (request['content_type'] == 'reel'
                and ((target['state'] in {'processing', 'retryable'} and stage in REEL_READ_STAGES)
                     or (target['state'] == 'uncertain' and stage in REEL_READ_STAGES | REEL_UPLOAD_STAGES | REEL_READY_STAGES)))

    @staticmethod
    def next_action(target):
        if target.state in {'succeeded', 'failed'}:
            return 'none'
        if target.state == 'sending':
            return 'wait_for_active_claim'
        stage = target.checkpoint.get('stage')
        if stage == 'yt_upload':return 'execute_next_step'
        if stage == 'yt_processing':return 'poll_status'
        if target.state == 'uncertain':
            if stage == 'ig_reel_ready' or stage not in REEL_READ_STAGES | REEL_UPLOAD_STAGES | {'fb_reel_ready'}:
                return 'manual_reconciliation'
            return 'poll_status'
        if stage in REEL_READ_STAGES | {'delivered', 'video_processing'}:
            return 'poll_status'
        return 'execute_next_step'

    def review(self, publication_id, principal):
        active = effective_settings(self.settings, self.sessions)
        with self.sessions() as db:
            item = self.checked(db, publication_id, principal)
            targets = list(db.scalars(select(PublicationTarget).where(PublicationTarget.publication_id == publication_id).order_by(PublicationTarget.account_id)))
            approval = db.scalar(select(PublicationApproval).where(PublicationApproval.publication_id == publication_id))
            states = [t.state for t in targets]
            if states and all(s == 'succeeded' for s in states):
                state = 'succeeded'
            elif 'uncertain' in states:
                state = 'attention_required'
            elif 'failed' in states:
                state = 'partial_failure' if any(s != 'failed' for s in states) else 'failed'
            elif all(s == 'prepared' for s in states):
                state = 'prepared'
            else:
                state = 'in_progress'
            return {'publication_id': item.id, 'request_hash': item.request_hash, 'principal': item.principal,
                'preview': item.payload, 'state': state, 'requires_owner_approval': not bool(approval and approval.expires_at > self.clock()),
                'live_writes_enabled': bool(active.publishing_enabled and (active.youtube_publishing_enabled if item.payload['request'].get('youtube') else active.meta_live_writes)
                    and self.platforms_enabled(active, item.payload['request'])
                    and self.media_write_allowed(item.payload['request'], active)
                    and all(aid in self.settings.meta_write_account_ids + self.settings.youtube_write_account_ids for aid in item.payload['request']['account_ids'])),
                'approval_channel': approval.channel if approval else None,
                'independent_owner_approval': bool(approval and approval.channel in {'owner_cli', 'owner_portal'} and approval.expires_at > self.clock()),
                'targets': [{'target_id': t.id, 'account_id': t.account_id, 'state': t.state,
                    'next_action': self.next_action(t),
                    'delivery_confirmed': t.state == 'succeeded' or t.checkpoint.get('stage') == 'delivered',
                    'checkpoint': {k: v for k, v in t.checkpoint.items() if k in {'stage', 'remote_id', 'container_id', 'polls', 'offset', 'size'}},
                    'result': t.result, 'error': {'code': t.error_code, 'message': t.error_message} if t.error_code else None,
                    'attempts': t.attempts, 'retry_at': t.next_attempt_at.isoformat() + 'Z' if t.next_attempt_at else None} for t in targets]}

    def approve(self, publication_id, expected_hash, issuer, explicit_request, *, channel='owner_cli'):
        """Trusted LOCAL OWNER channel only. Deliberately not exposed through MCP/HTTP."""
        if not self.settings.publishing_enabled or not (self.settings.meta_live_writes or self.settings.youtube_publishing_enabled):
            raise HubError('live_writes_disabled', 'Owner approvals are disabled while the live publishing feature is off')
        if not issuer or not explicit_request.strip():
            raise HubError('authorization_required', 'Local owner identity and explicit request evidence are required')
        if channel not in {'owner_cli', 'owner_portal'}:
            raise HubError('authorization_required', 'An independent owner channel is required')
        with self.sessions.begin() as db:
            item = self.checked(db, publication_id, None)
            require_write_accounts(self.settings, item.payload['request']['account_ids'])
            if item.request_hash != expected_hash:
                raise HubError('preview_changed', 'Owner review hash does not match the exact preview')
            approval = db.scalar(select(PublicationApproval).where(PublicationApproval.publication_id == publication_id).with_for_update())
            if approval and approval.expires_at > self.clock():
                raise HubError('approval_exists', 'This exact publication is already approved')
            if not approval:
                approval = PublicationApproval(publication_id=item.id, principal=item.principal, request_hash=item.request_hash, issuer=issuer, evidence_hash='')
                db.add(approval)
            approval.issuer = issuer
            approval.evidence_hash = hashlib.sha256(explicit_request.encode()).hexdigest()
            approval.issued_at = self.clock()
            approval.expires_at = self.clock() + timedelta(hours=1)
            approval.channel, approval.oauth_client_id, approval.used_at = channel, None, None
            db.add(Audit(subject=issuer, operation='approve_publication', outcome='success', details={'publication_id': item.id, 'request_hash': item.request_hash, 'principal': item.principal, 'channel': channel}))
        return {'approved': True, 'publication_id': publication_id, 'expires_in_seconds': 3600}

    def revoke(self, publication_id, expected_hash, issuer):
        """Independent owner only; halts future steps but cannot undo a dispatched request."""
        with self.sessions.begin() as db:
            item = self.checked(db, publication_id, None)
            if not issuer or item.request_hash != expected_hash:
                raise HubError('preview_changed', 'Exact owner-reviewed preview is required')
            approval = db.scalar(select(PublicationApproval).where(PublicationApproval.publication_id == publication_id).with_for_update())
            if not approval:
                # A denial must also prevent later automatic policy authorization.
                approval = PublicationApproval(publication_id=item.id, principal=item.principal,
                    request_hash=item.request_hash, issuer=issuer, evidence_hash=item.request_hash, channel='owner_cli')
                db.add(approval)
            approval.expires_at = self.clock()
            db.add(Audit(subject=issuer, operation='revoke_publication_approval', outcome='success', details={'publication_id': item.id, 'request_hash': item.request_hash}))
        return {'revoked': True, 'publication_id': publication_id}

    def approval_requirements(self, publication_id, principal, context=None):
        active = effective_settings(self.settings, self.sessions)
        policy = ApprovalPolicy(active)
        with self.sessions() as db:
            item = self.checked(db, publication_id, principal)
            result = policy.requirements(principal, item.payload, context)
            grant = db.scalar(select(PublicationApproval).where(PublicationApproval.publication_id == publication_id))
            current = bool(grant and grant.principal == principal and grant.request_hash == item.request_hash
                           and grant.expires_at > self.clock())
            owner_valid = current and grant.channel in {'owner_cli', 'owner_portal'}
            policy_valid = bool(current and grant.channel == 'chatgpt_policy'
                and not result['independent_owner_required'] and grant.oauth_client_id == context.oauth_client_id
                and grant.evidence_hash == policy.fingerprint())
            result['owner_grant_valid'] = bool(owner_valid)
            result['current_grant_valid'] = bool(owner_valid or policy_valid)
            result['write_accounts_allowed'] = all(aid in self.settings.meta_write_account_ids + self.settings.youtube_write_account_ids for aid in item.payload['request']['account_ids'])
            result['media_write_allowed'] = self.media_write_allowed(item.payload['request'], active)
            result['live_writes_enabled'] = bool(result['media_write_allowed'] and result['write_accounts_allowed'] and self.platforms_enabled(active, item.payload['request']) and (active.youtube_publishing_enabled if item.payload['request'].get('youtube') else active.meta_live_writes) and active.publishing_enabled)
            if grant and not (owner_valid or policy_valid):
                result.update(independent_owner_required=True, execution_channel='independent_owner',
                              next_step='The prior grant expired, was revoked, or no longer matches policy. A fresh independent owner decision is required; do not retry or mint a replacement automatically.')
            elif owner_valid:
                result['next_step'] = 'The exact independent owner grant is already recorded. Respect native host confirmation and execute only this publication; no additional owner interaction is needed.'
            result['needs_new_owner_grant'] = result['independent_owner_required'] and not owner_valid
            return result

    def claim(self, publication_id, target_id, principal, context=None):
        active = self.execution_settings()
        policy = ApprovalPolicy(active)
        with self.sessions.begin() as db:
            item = self.checked(db, publication_id, principal)
            require_write_accounts(self.settings, item.payload['request']['account_ids'])
            self.require_platforms(active, item.payload['request'])
            approval = db.scalar(select(PublicationApproval).where(PublicationApproval.publication_id == publication_id).with_for_update())
            if approval is None and policy.eligible(principal, item.payload, context):
                approval = PublicationApproval(publication_id=item.id, principal=principal, request_hash=item.request_hash,
                    issuer='chatgpt-policy', evidence_hash=policy.fingerprint(),
                    channel='chatgpt_policy', oauth_client_id=context.oauth_client_id,
                    expires_at=self.clock() + timedelta(hours=1))
                db.add(approval)
                db.flush()
                db.add(Audit(subject=principal, operation='authorize_publication_policy', outcome='success',
                    details={'publication_id': item.id, 'request_hash': item.request_hash, 'channel': 'chatgpt_policy',
                             'oauth_client_id': context.oauth_client_id, 'policy_sha256': approval.evidence_hash,
                             'approval_mode': active.approval_mode,
                             'native_confirmation_verified': False,
                             'independent_consent_verified': False}))
            if approval and approval.channel == 'chatgpt_policy':
                if (not policy.eligible(principal, item.payload, context)
                        or approval.oauth_client_id != context.oauth_client_id
                        or approval.evidence_hash != policy.fingerprint()):
                    raise HubError('authorization_required', 'ChatGPT policy or authenticated client changed; independent owner authorization required')
            if not approval or approval.principal != principal or approval.request_hash != item.request_hash or approval.expires_at <= self.clock():
                raise HubError('authorization_required', 'A current exact approval or explicitly enabled authenticated ChatGPT policy is required')
            target = db.get(PublicationTarget, target_id)
            if not target or target.publication_id != item.id:
                raise HubError('publication_denied', 'Target does not belong to the approved preview')
            if target.state in {'succeeded', 'failed'} or (target.state=='uncertain' and not (target.account_id=='pixelpapa-youtube' and target.checkpoint.get('stage')=='yt_upload' and target.checkpoint.get('session'))):
                return None
            if target.state == 'sending':
                if target.lease_until and target.lease_until <= self.clock():
                    db.execute(update(PublicationTarget).where(PublicationTarget.id == target.id, PublicationTarget.lease_token == target.lease_token).values(state='uncertain', error_code='worker_interrupted', error_message='Prior attempt ended without a durable outcome; reconcile before retrying', lease_token=None, lease_until=None))
                return None
            if target.next_attempt_at and target.next_attempt_at > self.clock():
                return None
            if target.attempts >= (4096 if target.account_id=='pixelpapa-youtube' else 20):
                target.state, target.error_code, target.error_message = 'failed', 'attempt_limit', 'Workflow exceeded its bounded attempt budget'
                return None
            lease = str(uuid4())
            result = db.execute(update(PublicationTarget).where(PublicationTarget.id == target.id, PublicationTarget.state == target.state, PublicationTarget.attempts == target.attempts).values(state='sending', lease_token=lease, lease_until=self.clock() + timedelta(minutes=5), attempts=target.attempts + 1))
            if result.rowcount != 1:
                return None
            if approval.used_at is None:
                approval.used_at = self.clock()  # Consumed to this immutable publication, including its resumable steps.
            return {'lease': lease, 'checkpoint': dict(target.checkpoint), 'payload': item.payload, 'account_id': target.account_id, 'approval_channel': approval.channel}

    def execution_settings(self):
        active = effective_settings(self.settings, self.sessions)
        if not active.publishing_enabled or not (active.meta_live_writes or active.youtube_publishing_enabled):
            raise HubError('live_writes_disabled', 'Live publishing remains disabled; preparing a preview does not authorize public writes')
        return active

    @staticmethod
    def platforms_enabled(active, request):
        return all(active._feature_states['youtube_publishing' if aid=='pixelpapa-youtube' else 'facebook_publishing' if aid == 'pixelpapa-facebook' else 'instagram_publishing']['effective'] for aid in request['account_ids'])

    @staticmethod
    def require_platforms(active, request):
        for aid in request['account_ids']:
            name = 'youtube_publishing' if aid=='pixelpapa-youtube' else 'facebook_publishing' if aid == 'pixelpapa-facebook' else 'instagram_publishing'
            if not active._feature_states[name]['effective']:
                raise HubError('publishing_feature_disabled', 'Publishing is paused for a selected platform; the immutable preview is preserved')

    async def execute(self, principal, scopes, publication_id, context=None):
        active = self.execution_settings()
        if 'socialhub:publish' not in scopes:
            raise HubError('insufficient_scope', 'The authenticated caller requires socialhub:publish')
        require_publisher(self.settings, principal)
        current = self.review(publication_id, principal)
        require_write_accounts(self.settings, current['preview']['request']['account_ids'])
        self.require_platforms(active, current['preview']['request'])
        if not self.media_write_allowed(current['preview']['request']):
            raise HubError('facebook_media_publishing_disabled' if 'pixelpapa-facebook' in current['preview']['request']['account_ids'] else 'youtube_publishing_disabled' if current['preview']['request'].get('youtube') else 'instagram_reels_publishing_disabled', 'Media publishing is not activated for every selected target; the prepared preview is preserved')
        for target in current['targets']:
            if (self.refreshable(target, current['preview']['request'])
                    and isinstance(self.context(target['account_id'], execute=False)[2], (MetaPublishingConnector, YouTubePublishingConnector))):
                # Acknowledged work needs only status reads, not another claim,
                # source-file check, upload URL or publication attempt.
                continue
            claim = self.claim(publication_id, target['target_id'], principal, context)
            if claim is None:
                continue
            checkpoint = claim['checkpoint']
            # No network mutation may occur until the immutable payload and grant were claimed durably.
            sent = False
            try:
                account, caps, adapter = self.context(claim['account_id'], execute=True)
                payload = claim['payload']
                request = dict(payload['request'])
                operation = {'publish': 'publish_post', 'update': 'update_post', 'reply': 'reply_to_comment'}[request['operation']]
                if not caps.get(operation):
                    raise HubError('permission_denied', 'Account permission or Page task no longer permits this operation')
                for asset in payload['assets']:
                    if request.get('youtube'):
                        if asset['id']==request['media_ids'][0]:
                            request['_media_path']=str(self.media.path(asset['id'],asset['sha256'])[0])
                            self.media.reserve_delivery(asset['id'])
                            request['_media_sha256'],request['_media_size']=asset['sha256'],asset['size']
                    elif request['content_type'] == 'reel':
                        if not checkpoint or checkpoint.get('stage') in REEL_UPLOAD_STAGES:
                            request['_media_path'] = str(self.media.path(asset['id'], asset['sha256'])[0])
                            self.media.reserve_delivery(asset['id'])
                            request['_media_sha256'], request['_media_size'] = asset['sha256'], asset['size']
                    else:
                        self.media.path(asset['id'], asset['sha256'])
                        self.media.reserve_delivery(asset['id'])
                        request['media_url'] = self.media.temporary_url(asset['id'], asset['sha256'])
                await adapter.validate(account.remote_id, request, payload['assets'])
                # A disable committed while validation awaited must stop dispatch.
                fresh = self.execution_settings()
                self.require_platforms(fresh, request)
                if not self.media_write_allowed(request):
                    raise HubError('publishing_feature_disabled', 'Media publishing was paused before dispatch')
                if claim.get('approval_channel') == 'chatgpt_policy' and not ApprovalPolicy(fresh).eligible(principal, payload, context):
                    raise HubError('authorization_required', 'Direct publishing was paused before dispatch')
                sent = True  # From here an exception can mean an ambiguous external outcome.
                if account.platform=='youtube':
                    from app.services.youtube_lock import upload_lock
                    with upload_lock(self.settings.media_root):outcome = await adapter.step(account.remote_id, request, checkpoint)
                else:outcome = await adapter.step(account.remote_id, request, checkpoint)
                self.finish(publication_id, target['target_id'], principal, claim['lease'], outcome)
            except HubError as error:
                # Durable checkpoint proves a delivered object; metadata-only reads can retry safely.
                safe_read = checkpoint.get('stage') in {'delivered', 'video_processing'}
                mutation_attempted = error.diagnostics.get('mutation_attempted', not safe_read)
                uncertain = sent and mutation_attempted and (error.code in {'write_outcome_unknown', 'api_timeout', 'api_unavailable', 'invalid_api_response'} or (error.code == 'api_error' and error.diagnostics.get('provider_code') not in {10, 200, 299}))
                paused = not sent and error.code in {'live_writes_disabled', 'publishing_feature_disabled', 'authorization_required'}
                retry = paused or (not mutation_attempted and error.retryable) or error.code == 'rate_limited' or (not sent and error.retryable)
                state = 'uncertain' if uncertain else 'retryable' if retry else 'failed'
                self.finish(publication_id, target['target_id'], principal, claim['lease'],
                    {'state': state, 'checkpoint': checkpoint, 'error_code': error.code, 'error_message': error.message[:256],
                     'timeout_phase': error.diagnostics.get('timeout_phase'), 'retry_after': min(max(error.diagnostics.get('retry_after',60),1),86400) if retry else 0})
            except Exception:
                # Never expose provider exceptions or silently resend after an unexpected failure.
                self.finish(publication_id, target['target_id'], principal, claim['lease'],
                    {'state': 'uncertain' if sent else 'failed', 'checkpoint': checkpoint, 'error_code': 'internal_error', 'error_message': 'Operation failed; inspect safe operation status before any retry'})
        return await self.refresh_status(publication_id, principal)

    async def refresh_status(self, publication_id, principal):
        """GET-only delivery/processing reconciliation; never uploads or finishes a Reel."""
        preview = self.review(publication_id, principal)
        for snapshot in preview['targets']:
            if not self.refreshable(snapshot, preview['preview']['request']):
                continue
            with self.sessions() as db:
                current = db.get(PublicationTarget, snapshot['target_id'])
                if (not current or current.state != snapshot['state'] or current.lease_token is not None
                        or not self.refreshable(dict(state=current.state, checkpoint=current.checkpoint), preview['preview']['request'])):
                    continue
                if current.next_attempt_at and current.next_attempt_at > self.clock():
                    continue
                checkpoint = dict(current.checkpoint)
            try:
                account, _, adapter = self.context(snapshot['account_id'], execute=False)
                if not isinstance(adapter, (MetaPublishingConnector, YouTubePublishingConnector)):
                    continue
                if account.platform!='youtube' and preview['preview']['request']['content_type'] == 'reel' and checkpoint.get('stage') != 'delivered':
                    outcome = await adapter.reel_refresh(account.remote_id, preview['preview']['request'], checkpoint,
                        uncertain=snapshot['state'] == 'uncertain')
                else:
                    outcome = await adapter.refresh_delivery(account.remote_id, preview['preview']['request'], checkpoint)
                error = None
            except HubError as caught:
                error = caught
            except Exception:
                error = HubError('internal_error', 'Delivery was acknowledged; metadata refresh failed without a new write')
            with self.sessions.begin() as db:
                self.checked(db, publication_id, principal)
                target = db.scalar(select(PublicationTarget).where(PublicationTarget.id == snapshot['target_id']).with_for_update())
                if (not target or target.state != snapshot['state'] or target.lease_token is not None
                        or target.checkpoint != checkpoint):
                    continue
                if error is not None:
                    target.error_code, target.error_message = error.code, error.message[:256]
                    target.next_attempt_at = self.clock() + timedelta(seconds=60)
                    if error.code == 'media_processing_failed':
                        target.state, target.next_attempt_at = 'failed', None
                    # Keep the acknowledgement; a missing permalink is not a failed post.
                    continue
                if outcome['state'] == 'processing':
                    target.state = 'uncertain' if outcome.get('uncertain') else 'processing'
                    target.checkpoint = outcome['checkpoint']
                    if not outcome.get('uncertain'):
                        target.error_code, target.error_message = None, None
                    target.next_attempt_at = self.clock() + timedelta(seconds=outcome.get('retry_after', 60))
                    if checkpoint.get('stage') != target.checkpoint.get('stage'):
                        db.add(Audit(subject=preview['principal'], operation='reconcile_publication_stage', account_id=target.account_id,
                            outcome=target.state, details={'publication_id': publication_id, 'target_id': target.id,
                                'stage': target.checkpoint.get('stage'), 'mutation_attempted': False}))
                    continue
                target.state, target.result = 'succeeded', outcome['result']
                target.checkpoint = outcome.get('checkpoint', checkpoint)
                target.error_code, target.error_message, target.next_attempt_at = None, None, None
                self.remember_post(db, publication_id, target)
                db.add(Audit(subject=preview['principal'], operation='finalize_publication', account_id=target.account_id,
                    outcome='success', details={'publication_id': publication_id, 'target_id': target.id,
                        'remote_id': target.result['remote_id'], 'mutation_attempted': False}))
        return self.review(publication_id, principal)

    def remember_post(self, db, publication_id, target):
        publication = db.get(Publication, publication_id)
        if publication.payload['request']['operation'] == 'publish' and target.result.get('remote_id'):
            existing = db.scalar(select(Post).where(Post.account_id == target.account_id, Post.remote_id == target.result['remote_id']))
            if not existing:
                db.add(Post(account_id=target.account_id, remote_id=target.result['remote_id'],
                    content={'publication_id': publication_id, 'permalink': target.result.get('permalink')}))

    def finish(self, publication_id, target_id, principal, lease, outcome):
        with self.sessions.begin() as db:
            target = db.scalar(select(PublicationTarget).where(PublicationTarget.id == target_id).with_for_update())
            if not target or target.lease_token != lease or target.state != 'sending':
                raise HubError('lease_lost', 'Target claim changed; reconcile the external outcome')
            target.state = outcome['state']
            target.checkpoint = outcome.get('checkpoint', {})
            target.result = outcome.get('result', {})
            target.error_code, target.error_message = outcome.get('error_code'), outcome.get('error_message')
            target.lease_token, target.lease_until = None, None
            delay = outcome.get('retry_after', 0)
            target.next_attempt_at = self.clock() + timedelta(seconds=delay) if delay else None
            details = {'publication_id': publication_id, 'target_id': target.id, 'error_code': target.error_code,
                'stage': target.checkpoint.get('stage'),
                'remote_id': target.result.get('remote_id') or target.checkpoint.get('remote_id')}
            if target.checkpoint.get('container_id'):
                details['container_id'] = target.checkpoint['container_id']
            if target.error_code == 'api_timeout' and outcome.get('timeout_phase') in {'connect', 'read', 'write', 'pool', 'unknown'}:
                details['timeout_phase'] = outcome['timeout_phase']
            db.add(Audit(subject=principal, operation='execute_publication', account_id=target.account_id, outcome=target.state, details=details))
            if target.state == 'succeeded' and target.result.get('remote_id'):
                self.remember_post(db, publication_id, target)

    async def reconcile(self, publication_id, target_id, remote_id, issuer, *, expected_hash=None):
        """LOCAL OWNER ONLY: verify a known owned remote object; never sends a mutation."""
        from datetime import datetime, timezone
        preview = self.review(publication_id, None)
        if not issuer or len(issuer) > 128 or (expected_hash is not None and expected_hash != preview['request_hash']):
            raise HubError('reconciliation_denied', 'Trusted owner and exact reviewed hash are required')
        with self.sessions() as db:
            target = db.get(PublicationTarget, target_id)
            item = db.get(Publication, publication_id)
            if not target or target.publication_id != publication_id or target.state != 'uncertain':
                raise HubError('reconciliation_denied', 'Only an uncertain target from this publication can be reconciled')
            if target.lease_token is not None:
                raise HubError('reconciliation_denied', 'An active claim cannot be reconciled')
            approval = db.scalar(select(PublicationApproval).where(PublicationApproval.publication_id == publication_id))
            if not approval or not approval.used_at or target.attempts < 1:
                raise HubError('reconciliation_denied', 'A recorded authorized execution is required')
            aid, created, attempts = target.account_id, approval.used_at, target.attempts
            checkpoint = dict(target.checkpoint)
            finish_attempt = None
            if item.payload['request']['content_type'] == 'reel' and aid == 'pixelpapa-instagram':
                if approval.request_hash != item.request_hash:
                    raise HubError('reconciliation_denied', 'The recorded authorization does not match this immutable preview')
                for event in db.scalars(select(Audit).where(Audit.operation == 'execute_publication',
                        Audit.account_id == aid, Audit.outcome == 'uncertain').order_by(Audit.at.desc()).limit(100)):
                    detail = event.details
                    if (detail.get('publication_id') == publication_id and detail.get('target_id') == target_id
                            and detail.get('stage') == 'ig_reel_ready'
                            and detail.get('container_id') == checkpoint.get('container_id')):
                        finish_attempt = event.at
                        break
        request = preview['preview']['request']
        account, _, adapter = self.context(aid, execute=False)
        # Reconciliation is a connector read operation, not a generic remote-ID acceptance.
        if not isinstance(adapter, (MetaPublishingConnector, YouTubePublishingConnector)):
            raise HubError('unsupported_operation', 'This connector has no verified reconciliation reader')
        if request['operation'] == 'reply':
            raise HubError('manual_check_required', 'Reply outcome needs a parent-bound reply reader; no automatic resend is allowed')
        if request['operation'] == 'update' and remote_id != request['post_id']:
            raise HubError('reconciliation_denied', 'An edit can only reconcile its original owned post ID')
        instagram_reel = request['operation'] == 'publish' and request['content_type'] == 'reel' and account.platform == 'instagram'
        if request['operation'] == 'publish' and request['content_type'] not in {'text', 'link', 'image'} and not instagram_reel and account.platform!='youtube':
            raise HubError('manual_check_required', 'Video/Reel reconciliation requires verified processing evidence; no resend is allowed')
        if account.platform=='youtube':
            post = await adapter.verify_reconciliation(account.remote_id,request,remote_id,created,preview['preview']['assets'][0])
        elif instagram_reel:
            from app.services.reel_reconciliation import verified_instagram_reel
            post = await verified_instagram_reel(adapter, account.remote_id, request, checkpoint,
                                                  remote_id, created, finish_attempt)
        else:
            post = await adapter.connector.get_post(account.remote_id, remote_id)
        remote_text = post.get('message') if account.platform == 'facebook' else post.get('caption')
        if account.platform=='youtube':remote_text = ''  # Structured metadata verified by its own provider reader.
        if instagram_reel and remote_text is None:
            remote_text = ''
        if remote_text != request['text']:
            raise HubError('reconciliation_denied', 'Remote text does not match the approved publication')
        if request['content_type'] == 'image':
            photos = (post.get('attachments') or {}).get('data', [])
            if account.platform != 'facebook' or len(photos) != 1 or photos[0].get('type') != 'photo':
                raise HubError('reconciliation_denied', 'Owner-selected image outcome must contain exactly one Facebook photo')
        if request['operation'] == 'publish':
            try:
                stamp = datetime.fromisoformat((post.get('created_time') or post.get('timestamp') or post.get('snippet',{}).get('publishedAt') or '').replace('Z', '+00:00'))
                if stamp.tzinfo is None or stamp.astimezone(timezone.utc).replace(tzinfo=None) < created - timedelta(minutes=2):
                    raise ValueError()
            except ValueError:
                raise HubError('reconciliation_denied', 'A pre-existing or undated post cannot prove this publication succeeded') from None
        with self.sessions.begin() as db:
            item = self.checked(db, publication_id, preview['principal'])
            target = db.scalar(select(PublicationTarget).where(PublicationTarget.id == target_id).with_for_update())
            if (item.request_hash != preview['request_hash'] or not target or target.state != 'uncertain'
                    or target.publication_id != publication_id or target.account_id != aid
                    or target.attempts != attempts or target.lease_token is not None
                    or target.checkpoint != checkpoint):
                raise HubError('reconciliation_denied', 'Target state changed during reconciliation')
            cached = db.scalar(select(Post).where(Post.account_id == aid, Post.remote_id == remote_id))
            if cached and cached.content.get('publication_id') != publication_id:
                raise HubError('reconciliation_denied', 'Remote post is already bound to another publication')
            target.state = 'succeeded'
            target.result = {'remote_id': remote_id, 'permalink': adapter.permalink(post), 'reconciled': True}
            if account.platform=='youtube':
                target.result.update(privacy_requested=request['youtube']['visibility'],privacy_actual=post.get('status',{}).get('privacyStatus'),
                    public_visibility_confirmed=post.get('status',{}).get('privacyStatus')=='public',processing_confirmed=True)
            target.checkpoint = {'stage': 'delivered', 'remote_id': remote_id, 'reconciled': True}
            if instagram_reel or account.platform=='youtube':
                target.checkpoint = dict(checkpoint, **target.checkpoint)
            target.error_code, target.error_message, target.next_attempt_at = None, None, None
            self.remember_post(db, publication_id, target)
            details = {'publication_id': publication_id, 'remote_id': remote_id,
                'request_hash': item.request_hash, 'mutation_attempted': False, 'attempts_unchanged': True}
            if request['content_type'] == 'image':
                details['media_evidence'] = 'owner-selected remote ID, caption, execution time and single photo; remote bytes not hash-equated'
            if instagram_reel:
                details.update(container_id=checkpoint['container_id'], previous_stage=checkpoint['stage'],
                    media_evidence='PUBLISHED saved container and unique account-owned Reel with exact caption and bounded execution time; remote bytes not hash-equated')
            db.add(Audit(subject=issuer, operation='reconcile_publication', account_id=aid, outcome='success', details=details))
        return self.review(publication_id, None)
