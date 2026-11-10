"""Классификация ошибок групповой линии (app/groups/errors.py, §22.6)."""

from telethon.errors import (
    ChannelPrivateError,
    ChannelsTooMuchError,
    ChatWriteForbiddenError,
    InviteHashExpiredError,
    InviteRequestSentError,
    SlowModeWaitError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
    UsernameNotOccupiedError,
)
from telethon.tl.types import Channel, Chat, ChatPhotoEmpty, User

from app.groups.errors import (
    classify_join_error,
    classify_post_error,
    entity_kind_problem,
)


def _err(cls, **kw):
    e = cls(request=None, **kw) if kw else cls(request=None)
    return e


def test_slowmode_wait_seconds():
    e = SlowModeWaitError(request=None, capture=120)
    out = classify_post_error(e, posts_count=3)
    assert out.action == "slowmode" and out.wait_sec == 120


def test_write_forbidden_before_and_after_posts():
    e = _err(ChatWriteForbiddenError)
    assert classify_post_error(e, posts_count=0).action == "manual"
    assert classify_post_error(e, posts_count=5).action == "exclude"


def test_banned_in_channel_triggers_ban_check_not_exclude():
    out = classify_post_error(_err(UserBannedInChannelError), posts_count=10)
    assert out.action == "ban_check"


def test_gone_and_unknown():
    assert classify_post_error(_err(ChannelPrivateError), posts_count=0).action == "exclude"
    assert classify_post_error(RuntimeError("boom"), posts_count=0).action == "retry"


def test_join_errors():
    assert classify_join_error(_err(UserAlreadyParticipantError)).action == "member"
    assert classify_join_error(_err(InviteRequestSentError)).action == "pending"
    assert classify_join_error(_err(ChannelsTooMuchError)).action == "too_many"
    assert classify_join_error(_err(InviteHashExpiredError)).action == "exclude"
    assert classify_join_error(_err(UsernameNotOccupiedError)).action == "exclude"
    assert classify_join_error(ValueError("No user has x")).action == "exclude"
    assert classify_join_error(RuntimeError("net")).action == "retry"


def test_entity_kind():
    group = Chat(id=1, title="g", photo=ChatPhotoEmpty(), participants_count=3, date=None, version=1)
    mega = Channel(id=2, title="m", photo=ChatPhotoEmpty(), date=None, megagroup=True)
    chan = Channel(id=3, title="c", photo=ChatPhotoEmpty(), date=None, broadcast=True)
    user = User(id=4)
    assert entity_kind_problem(group) is None
    assert entity_kind_problem(mega) is None
    assert "канал" in entity_kind_problem(chan)
    assert entity_kind_problem(user) == "это не группа"
