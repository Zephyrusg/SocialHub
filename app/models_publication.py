from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class YouTubeMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True)
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=100)
    visibility: Literal['private', 'unlisted', 'public'] = 'private'
    made_for_kids: StrictBool
    contains_synthetic_media: StrictBool
    thumbnail_media_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode='after')
    def valid(self):
        if (not self.title.strip() or '<' in self.title or '>' in self.title
                or len(self.description.encode('utf-8')) > 5000 or '<' in self.description or '>' in self.description
                or sum(len(t)+(2 if ' ' in t else 0)+1 for t in self.tags)>500
                or any(not t.strip() or len(t)>100 or '<' in t or '>' in t for t in self.tags)):
            raise ValueError('Invalid YouTube title, description or tags')
        return self


class PublicationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True)
    account_ids: list[str] = Field(min_length=1, max_length=8)
    operation: Literal['publish', 'update', 'reply'] = 'publish'
    content_type: Literal['text', 'image', 'link', 'video', 'reel', 'short'] = 'text'
    text: str = Field(default='', max_length=63206)
    youtube: YouTubeMetadata | None = None
    is_ai_generated: StrictBool | None = None
    media_ids: list[str] = Field(default_factory=list, max_length=1)
    link_url: str | None = Field(default=None, max_length=2048)
    post_id: str | None = Field(default=None, max_length=128)
    comment_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode='after')
    def consistent(self):
        if self.youtube is not None:
            if self.account_ids != ['pixelpapa-youtube'] or self.content_type not in {'video','short'} or self.operation!='publish' or self.text or self.is_ai_generated is not None:
                raise ValueError('YouTube uploads need their own structured metadata and one channel target')
        elif 'pixelpapa-youtube' in self.account_ids or self.content_type=='short':
            raise ValueError('YouTube structured metadata is required')
        if self.is_ai_generated is not None and self.content_type != 'reel':
            raise ValueError('AI disclosure is currently supported for Reels only')
        if len(set(self.account_ids)) != len(self.account_ids):
            raise ValueError('Duplicate target accounts')
        if self.operation != 'publish':
            if not self.text.strip() or not self.post_id or self.content_type != 'text' or self.media_ids or self.link_url:
                raise ValueError('Updates/replies require text and an owned post ID; media replacement is unsupported')
            if self.operation == 'reply' and not self.comment_id:
                raise ValueError('A reply requires the parent comment ID')
            if self.operation == 'update' and self.comment_id:
                raise ValueError('Post updates cannot target a comment')
        elif self.post_id or self.comment_id:
            raise ValueError('A publication cannot target an existing object')
        if self.content_type in {'image', 'video', 'reel', 'short'}:
            if len(self.media_ids) != 1 or self.link_url:
                raise ValueError('Media content requires exactly one asset and no link')
        elif self.media_ids:
            raise ValueError('Text/link content cannot include media')
        if self.content_type == 'link':
            if not self.link_url:
                raise ValueError('Link content requires link_url')
        elif self.link_url:
            raise ValueError('link_url is only supported for link posts')
        if self.content_type == 'text' and not self.text.strip():
            raise ValueError('Text content cannot be empty')
        return self
