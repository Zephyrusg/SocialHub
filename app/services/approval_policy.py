"""Host confirmation reliance is an operator policy, never a consent receipt."""
import hashlib
import json
import time
import re
from app.security.auth import VerifiedAccessToken


class ApprovalPolicy:
    def __init__(self, settings):
        self.settings = settings

    def eligible(self, principal, payload, context):
        s = self.settings
        tested = ((s.approval_mode == 'chatgpt_native' and s.chatgpt_confirmation_tested
                   and re.fullmatch(r'[a-f0-9]{64}', s.chatgpt_confirmation_evidence_sha256))
                  or (s.approval_mode == 'chatgpt_direct' and s.chatgpt_direct_tested
                   and re.fullmatch(r'[a-f0-9]{64}', s.chatgpt_direct_evidence_sha256)))
        # Recheck every gate at execution time, including after config changes.
        return bool(tested and s.auth_mode == 'oauth' and s.chatgpt_residual_risk_accepted
            and isinstance(context, VerifiedAccessToken)
            and context.authentication_method == 'oauth' and context.client_id == principal
            and context.expires_at and context.expires_at > time.time()
            and {'socialhub:read', 'socialhub:publish'} <= set(context.scopes)
            and context.oauth_client_id in s.chatgpt_allowed_client_ids
            and principal in s.chatgpt_allowed_principals and principal in s.publishing_allowed_subjects
            and payload['request']['operation'] in s.chatgpt_allowed_operations
            and all(aid in s.meta_write_account_ids + s.youtube_write_account_ids for aid in payload['request']['account_ids'])
            and len(payload['request']['account_ids']) == 1)

    def fingerprint(self, payload=None):
        s = self.settings
        values = [s.approval_mode, s.oauth_issuer, s.oauth_audience,
                  sorted(s.chatgpt_allowed_client_ids), sorted(s.chatgpt_allowed_principals), sorted(s.publishing_allowed_subjects),
                  sorted(s.chatgpt_allowed_operations), sorted(s.meta_write_account_ids), s.chatgpt_confirmation_tested,
                  s.chatgpt_confirmation_evidence_sha256, s.chatgpt_direct_tested,
                  s.chatgpt_direct_evidence_sha256, s.chatgpt_residual_risk_accepted]
        if payload and 'pixelpapa-youtube' in payload['request']['account_ids']:
            values.append(['youtube-policy-v1',sorted(s.youtube_write_account_ids)])
        return hashlib.sha256(json.dumps(values, separators=(',', ':')).encode()).hexdigest()

    def requirements(self, principal, payload, context=None):
        eligible = self.eligible(principal, payload, context)
        return {'preferred_interface': 'chatgpt', 'configured_policy': self.settings.approval_mode,
                'execution_channel': 'chatgpt_policy' if eligible else 'independent_owner',
                'independent_owner_required': not eligible,
                'native_confirmation_receipt_verified': False,
                'native_host_test_recorded': self.settings.chatgpt_confirmation_tested,
                'direct_instruction_test_recorded': self.settings.chatgpt_direct_tested,
                'consent_basis': ('authenticated_chat_directive_reliance' if eligible and self.settings.approval_mode == 'chatgpt_direct'
                                 else 'native_host_confirmation_reliance' if eligible else 'independent_owner'),
                'residual_risk': 'The server cannot independently verify a user chat confirmation; an authenticated client call is not a consent receipt.',
                'stronger_channels': ['owner_portal', 'owner_cli'],
                'portal_available': self.settings.approval_portal_enabled,
                'next_step': 'Execute only the exact publication explicitly requested and reviewed by the user in ChatGPT; respect any host confirmation. The server cannot independently verify that instruction.' if eligible else
                             'Review in ChatGPT. The current security policy still requires a separate trusted owner grant.'}
