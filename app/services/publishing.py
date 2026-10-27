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
        with self.sessions() as db:
            account = Hub(self.sessions, active).account(db, account_id)
            if account.is_mock:
                raise HubError('account_denied', 'The durable publisher requires explicitly connected real-account identities')
            credential = db.scalar(select(Credential).where(Credential.account_id == account_id))
            if not credential:
                raise HubError('meta_not_connected', 'Connect and verify the account before preparing content')
            token = TokenVault([active.encryption_key.get_secret_value()]).decrypt(credential)
            caps = capabilities(account.platform, credential.scopes, token.get('tasks', []), account.account_type)
            if self.connector_factory:
                adapter = self.connector_factory(account, execute)
            else:
                graph = GraphClient(token['access_token'], active.meta_graph_version,
                    app_secret=active.meta_app_secret.get_secret_value(), allow_writes=execute and self.settings.meta_live_writes and self.settings.publishing_enabled)
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
        data['account_ids'] = sorted(data['account_ids'])
        require_write_accounts(self.settings, data['account_ids'])
        assets = [self.media.get(mid, principal) for mid in data['media_ids']]
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

    def review(self, publication_id, principal):
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
                'live_writes_enabled': bool(self.settings.meta_live_writes and self.settings.publishing_enabled
                    and all(aid in self.settings.meta_write_account_ids for aid in item.payload['request']['account_ids'])),
                'approval_channel': approval.channel if approval else None,
                'independent_owner_approval': bool(approval and approval.channel in {'owner_cli', 'owner_portal'} and approval.expires_at > self.clock()),
                'targets': [{'target_id': t.id, 'account_id': t.account_id, 'state': t.state,
                    'checkpoint': {k: v for k, v in t.checkpoint.items() if k in {'stage', 'remote_id', 'container_id', 'polls'}},
                    'result': t.result, 'error': {'code': t.error_code, 'message': t.error_message} if t.error_code else None,
                    'attempts': t.attempts, 'retry_at': t.next_attempt_at.isoformat() + 'Z' if t.next_attempt_at else None} for t in targets]}

    def approve(self, publication_id, expected_hash, issuer, explicit_request, *, channel='owner_cli'):
        """Trusted LOCAL OWNER channel only. Deliberately not exposed through MCP/HTTP."""
        if not self.settings.meta_live_writes or not self.settings.publishing_enabled:
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
        with self.sessions() as db:
            item = self.checked(db, publication_id, principal)
            result = self.approval_policy.requirements(principal, item.payload, context)
            grant = db.scalar(select(PublicationApproval).where(PublicationApproval.publication_id == publication_id))
            current = bool(grant and grant.principal == principal and grant.request_hash == item.request_hash
                           and grant.expires_at > self.clock())
            owner_valid = current and grant.channel in {'owner_cli', 'owner_portal'}
            policy_valid = bool(current and grant.channel == 'chatgpt_policy'
                and not result['independent_owner_required'] and grant.oauth_client_id == context.oauth_client_id
                and grant.evidence_hash == self.approval_policy.fingerprint())
            result['owner_grant_valid'] = bool(owner_valid)
            result['current_grant_valid'] = bool(owner_valid or policy_valid)
            result['write_accounts_allowed'] = all(aid in self.settings.meta_write_account_ids for aid in item.payload['request']['account_ids'])
            result['live_writes_enabled'] = bool(result['write_accounts_allowed'] and self.settings.meta_live_writes and self.settings.publishing_enabled)
            if grant and not (owner_valid or policy_valid):
                result.update(independent_owner_required=True, execution_channel='independent_owner',
                              next_step='The prior grant expired, was revoked, or no longer matches policy. A fresh independent owner decision is required; do not retry or mint a replacement automatically.')
            elif owner_valid:
                result['next_step'] = 'The exact independent owner grant is already recorded. Respect native host confirmation and execute only this publication; no additional owner interaction is needed.'
            result['needs_new_owner_grant'] = result['independent_owner_required'] and not owner_valid
            return result

    def claim(self, publication_id, target_id, principal, context=None):
        with self.sessions.begin() as db:
            item = self.checked(db, publication_id, principal)
            require_write_accounts(self.settings, item.payload['request']['account_ids'])
            approval = db.scalar(select(PublicationApproval).where(PublicationApproval.publication_id == publication_id).with_for_update())
            if approval is None and self.approval_policy.eligible(principal, item.payload, context):
                approval = PublicationApproval(publication_id=item.id, principal=principal, request_hash=item.request_hash,
                    issuer='chatgpt-policy', evidence_hash=self.approval_policy.fingerprint(),
                    channel='chatgpt_policy', oauth_client_id=context.oauth_client_id,
                    expires_at=self.clock() + timedelta(hours=1))
                db.add(approval)
                db.flush()
                db.add(Audit(subject=principal, operation='authorize_publication_policy', outcome='success',
                    details={'publication_id': item.id, 'request_hash': item.request_hash, 'channel': 'chatgpt_policy',
                             'oauth_client_id': context.oauth_client_id, 'policy_sha256': approval.evidence_hash,
                             'independent_consent_verified': False}))
            if approval and approval.channel == 'chatgpt_policy':
                if (not self.approval_policy.eligible(principal, item.payload, context)
                        or approval.oauth_client_id != context.oauth_client_id
                        or approval.evidence_hash != self.approval_policy.fingerprint()):
                    raise HubError('authorization_required', 'ChatGPT policy or authenticated client changed; independent owner authorization required')
            if not approval or approval.principal != principal or approval.request_hash != item.request_hash or approval.expires_at <= self.clock():
                raise HubError('authorization_required', 'A current exact approval or explicitly enabled authenticated ChatGPT policy is required')
            target = db.get(PublicationTarget, target_id)
            if not target or target.publication_id != item.id:
                raise HubError('publication_denied', 'Target does not belong to the approved preview')
            if target.state in {'succeeded', 'failed', 'uncertain'}:
                return None
            if target.state == 'sending':
                if target.lease_until and target.lease_until <= self.clock():
                    db.execute(update(PublicationTarget).where(PublicationTarget.id == target.id, PublicationTarget.lease_token == target.lease_token).values(state='uncertain', error_code='worker_interrupted', error_message='Prior attempt ended without a durable outcome; reconcile before retrying', lease_token=None, lease_until=None))
                return None
            if target.next_attempt_at and target.next_attempt_at > self.clock():
                return None
            if target.attempts >= 20:
                target.state, target.error_code, target.error_message = 'failed', 'attempt_limit', 'Workflow exceeded its bounded attempt budget'
                return None
            lease = str(uuid4())
            result = db.execute(update(PublicationTarget).where(PublicationTarget.id == target.id, PublicationTarget.state == target.state, PublicationTarget.attempts == target.attempts).values(state='sending', lease_token=lease, lease_until=self.clock() + timedelta(minutes=5), attempts=target.attempts + 1))
            if result.rowcount != 1:
                return None
            if approval.used_at is None:
                approval.used_at = self.clock()  # Consumed to this immutable publication, including its resumable steps.
            return {'lease': lease, 'checkpoint': dict(target.checkpoint), 'payload': item.payload, 'account_id': target.account_id}

    async def execute(self, principal, scopes, publication_id, context=None):
        if not self.settings.meta_live_writes or not self.settings.publishing_enabled:
            raise HubError('live_writes_disabled', 'Live publishing remains disabled; preparing a preview does not authorize public writes')
        if 'socialhub:publish' not in scopes:
            raise HubError('insufficient_scope', 'The authenticated caller requires socialhub:publish')
        require_publisher(self.settings, principal)
        current = self.review(publication_id, principal)
        require_write_accounts(self.settings, current['preview']['request']['account_ids'])
        for target in current['targets']:
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
                    self.media.path(asset['id'], asset['sha256'])
                    # Metadata participates in the preview hash; on-disk bytes are rechecked before each step.
                    request['media_url'] = self.media.temporary_url(asset['id'], asset['sha256'])
                await adapter.validate(account.remote_id, request, payload['assets'])
                sent = True  # From here an exception can mean an ambiguous external outcome.
                outcome = await adapter.step(account.remote_id, request, checkpoint)
                self.finish(publication_id, target['target_id'], principal, claim['lease'], outcome)
            except HubError as error:
                # Durable checkpoint proves a delivered object; metadata-only reads can retry safely.
                safe_read = checkpoint.get('stage') in {'delivered', 'video_processing'}
                mutation_attempted = error.diagnostics.get('mutation_attempted', not safe_read)
                uncertain = sent and mutation_attempted and (error.code in {'write_outcome_unknown', 'api_timeout', 'api_unavailable', 'invalid_api_response'} or (error.code == 'api_error' and error.diagnostics.get('provider_code') not in {10, 200, 299}))
                retry = (not mutation_attempted and error.retryable) or error.code == 'rate_limited' or (not sent and error.retryable)
                state = 'uncertain' if uncertain else 'retryable' if retry else 'failed'
                self.finish(publication_id, target['target_id'], principal, claim['lease'],
                    {'state': state, 'checkpoint': checkpoint, 'error_code': error.code, 'error_message': error.message[:256], 'retry_after': 60 if retry else 0})
            except Exception:
                # Never expose provider exceptions or silently resend after an unexpected failure.
                self.finish(publication_id, target['target_id'], principal, claim['lease'],
                    {'state': 'uncertain' if sent else 'failed', 'checkpoint': checkpoint, 'error_code': 'internal_error', 'error_message': 'Operation failed; inspect safe operation status before any retry'})
        return self.review(publication_id, principal)

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
            db.add(Audit(subject=principal, operation='execute_publication', account_id=target.account_id, outcome=target.state,
                details={'publication_id': publication_id, 'target_id': target.id, 'error_code': target.error_code, 'remote_id': target.result.get('remote_id')}))
            if target.state == 'succeeded' and target.result.get('remote_id'):
                publication = db.get(Publication, publication_id)
                if publication.payload['request']['operation'] == 'publish':
                    existing = db.scalar(select(Post).where(Post.account_id == target.account_id, Post.remote_id == target.result['remote_id']))
                    if not existing:
                        db.add(Post(account_id=target.account_id, remote_id=target.result['remote_id'], content={'publication_id': publication_id, 'permalink': target.result.get('permalink')}))

    async def reconcile(self, publication_id, target_id, remote_id, issuer):
        """LOCAL OWNER ONLY: verify a known owned remote object; never sends a mutation."""
        from datetime import datetime, timezone
        preview = self.review(publication_id, None)
        with self.sessions() as db:
            target = db.get(PublicationTarget, target_id)
            item = db.get(Publication, publication_id)
            if not target or target.publication_id != publication_id or target.state != 'uncertain':
                raise HubError('reconciliation_denied', 'Only an uncertain target from this publication can be reconciled')
            aid, created = target.account_id, item.created_at
        request = preview['preview']['request']
        account, _, adapter = self.context(aid, execute=False)
        # Reconciliation is a connector read operation, not a generic remote-ID acceptance.
        if not isinstance(adapter, MetaPublishingConnector):
            raise HubError('unsupported_operation', 'This connector has no verified reconciliation reader')
        if request['operation'] == 'reply':
            raise HubError('manual_check_required', 'Reply outcome needs a parent-bound reply reader; no automatic resend is allowed')
        if request['operation'] == 'update' and remote_id != request['post_id']:
            raise HubError('reconciliation_denied', 'An edit can only reconcile its original owned post ID')
        post = await adapter.connector.get_post(account.remote_id, remote_id)
        if (post.get('message') if account.platform == 'facebook' else post.get('caption')) != request['text']:
            raise HubError('reconciliation_denied', 'Remote text does not match the approved publication')
        if request['operation'] == 'publish':
            try:
                stamp = datetime.fromisoformat((post.get('created_time') or post.get('timestamp') or '').replace('Z', '+00:00'))
                if stamp.tzinfo is None or stamp.astimezone(timezone.utc).replace(tzinfo=None) < created - timedelta(minutes=2):
                    raise ValueError()
            except ValueError:
                raise HubError('reconciliation_denied', 'A pre-existing or undated post cannot prove this publication succeeded') from None
        with self.sessions.begin() as db:
            target = db.scalar(select(PublicationTarget).where(PublicationTarget.id == target_id).with_for_update())
            if target.state != 'uncertain':
                raise HubError('reconciliation_denied', 'Target state changed during reconciliation')
            target.state = 'succeeded'
            target.result = {'remote_id': remote_id, 'permalink': post.get('permalink_url') or post.get('permalink'), 'reconciled': True}
            target.error_code, target.error_message = None, None
            db.add(Audit(subject=issuer, operation='reconcile_publication', account_id=aid, outcome='success', details={'publication_id': publication_id, 'remote_id': remote_id}))
        return self.review(publication_id, None)
