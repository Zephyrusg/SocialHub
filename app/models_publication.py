from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class PublicationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True)
    account_ids: list[str] = Field(min_length=1, max_length=8)
    operation: Literal['publish', 'update', 'reply'] = 'publish'
    content_type: Literal['text', 'image', 'link', 'video', 'reel'] = 'text'
    text: str = Field(default='', max_length=63206)
    is_ai_generated: StrictBool | None = None
    media_ids: list[str] = Field(default_factory=list, max_length=1)
    link_url: str | None = Field(default=None, max_length=2048)
    post_id: str | None = Field(default=None, max_length=128)
    comment_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode='after')
    def consistent(self):
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
        if self.content_type in {'image', 'video', 'reel'}:
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
