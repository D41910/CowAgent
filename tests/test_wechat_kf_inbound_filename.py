"""wechat_kf inbound file: the server-supplied name stays one path component.

``channel/chat_message.py::safe_filename`` states the contract -- every inbound
attachment is written to ``tmp_dir()/<file_name>``, so a name holding a path
separator has to be reduced before it reaches ``os.path.join``. weixin gets
that guard from a371f371; slack, discord, qq and dingtalk sanitize their own
inbound names. The WeCom customer-service channel parses its name out of the
``Content-Disposition`` header of the media download and used it as-is.

The name is additionally prefixed with the per-message media id: the sender
picks it, so two attachments in one conversation can share it, and a bare
name lets the second download overwrite the first (``FileCache`` then
de-duplicates the identical path, so the agent sees only one file). telegram
prefixes its ``file_id`` and weixin a UUID since #3635 for the same reason.
"""
from pathlib import Path

import pytest

from bridge.context import ContextType
from channel.wechat_kf import wechat_kf_message as mod
from channel.wechat_kf.wechat_kf_message import WechatKfMessage

MEDIA_ID = "3rDPHxBQtdWeQLzxwpT-HNYw2eXba7ma9lQc"

HOSTILE = [
    ('attachment; filename="../../outside.txt"', "outside.txt"),
    ("attachment; filename*=UTF-8''..%2F..%2Fevil.pdf", "evil.pdf"),
    ('attachment; filename="sub/dir/quarterly.pdf"', "quarterly.pdf"),
    (r'attachment; filename="..\..\windows\temp\x.txt"', "x.txt"),
]


class _FakeResponse:
    """Payload plus the streaming API ``common.media_download`` needs."""

    def __init__(self, headers, payload=b"payload"):
        self.status_code = 200
        self.headers = headers
        self.content = payload

    def iter_content(self, chunk_size=8192):
        yield self.content

    def close(self):
        pass


class _FakeMedia:
    def __init__(self, disposition, payload=b"payload"):
        self._disposition = disposition
        self._payload = payload

    def download(self, media_id):
        headers = {} if self._disposition is None else {"Content-Disposition": self._disposition}
        return _FakeResponse(headers, self._payload)


class _FakeClient:
    def __init__(self, disposition, payload=b"payload"):
        self.media = _FakeMedia(disposition, payload)


def _message(tmp_path, monkeypatch, disposition, msgtype="file"):
    monkeypatch.setattr(mod, "_get_tmp_dir", lambda: str(tmp_path))
    raw = {
        "msgid": "kfmsg001",
        "send_time": 1700000000,
        "origin": 3,
        "msgtype": msgtype,
        "open_kfid": "wk_open_kfid",
        "external_userid": "wm_external_userid",
        msgtype: {"media_id": MEDIA_ID},
    }
    return WechatKfMessage(msg=raw, client=_FakeClient(disposition))


@pytest.mark.parametrize("disposition,expected", HOSTILE)
def test_a_name_with_a_separator_stays_inside_the_tmp_dir(tmp_path, monkeypatch, disposition, expected):
    msg = _message(tmp_path, monkeypatch, disposition)
    msg.prepare()

    assert msg.ctype == ContextType.FILE
    path = Path(msg.content)
    assert path.parent == tmp_path, f"{msg.content} left the tmp dir"
    # The sanitized name is kept, prefixed with the per-message media id so
    # two attachments sharing it cannot clobber each other.
    assert path.name == f"{MEDIA_ID}_{expected}"
    assert path.read_bytes() == b"payload", "the download itself must still happen"
    assert not (tmp_path.parent / expected).exists(), "nothing may land above the tmp dir"


def test_an_ordinary_name_keeps_the_one_the_server_handed_back(tmp_path, monkeypatch):
    msg = _message(tmp_path, monkeypatch, 'attachment; filename="report 2024.pdf"')
    msg.prepare()

    assert Path(msg.content).name == f"{MEDIA_ID}_report 2024.pdf"
    assert Path(msg.content).read_bytes() == b"payload"


@pytest.mark.parametrize(
    "disposition",
    [None, "", 'attachment; filename=""', "inline; size=12"],
)
def test_a_response_without_a_usable_name_falls_back_to_the_media_id(tmp_path, monkeypatch, disposition):
    msg = _message(tmp_path, monkeypatch, disposition)
    msg.prepare()

    assert Path(msg.content) == tmp_path / MEDIA_ID
    assert Path(msg.content).read_bytes() == b"payload"


def test_the_image_branch_is_unchanged(tmp_path, monkeypatch):
    msg = _message(tmp_path, monkeypatch, None, msgtype="image")
    msg.prepare()

    assert msg.ctype == ContextType.IMAGE
    assert Path(msg.content) == tmp_path / (MEDIA_ID + ".jpg")
    assert Path(msg.content).read_bytes() == b"payload"


def _same_named_message(tmp_path, monkeypatch, msgid, media_id, payload):
    """A second inbound file whose Content-Disposition name matches the first."""
    monkeypatch.setattr(mod, "_get_tmp_dir", lambda: str(tmp_path))
    raw = {
        "msgid": msgid,
        "send_time": 1700000000,
        "origin": 3,
        "msgtype": "file",
        "open_kfid": "wk_open_kfid",
        "external_userid": "wm_external_userid",
        "file": {"media_id": media_id},
    }
    return WechatKfMessage(
        msg=raw,
        client=_FakeClient('attachment; filename="report.txt"', payload),
    )


def test_two_attachments_sharing_a_name_stay_separate_files(tmp_path, monkeypatch):
    """Two different files the sender both named ``report.txt`` must both
    survive: the second download may not overwrite the first, and each
    message must keep pointing at its own bytes."""
    first = _same_named_message(tmp_path, monkeypatch, "kfmsg001", "mediaAAA", b"first")
    second = _same_named_message(tmp_path, monkeypatch, "kfmsg002", "mediaBBB", b"second")
    first.prepare()
    second.prepare()

    assert first.content != second.content, "the two attachments share one path"
    assert Path(first.content).read_bytes() == b"first"
    assert Path(second.content).read_bytes() == b"second"
    assert len(list(tmp_path.iterdir())) == 2, "both downloads must land on disk"


def test_a_long_name_stays_within_the_filename_byte_budget(tmp_path, monkeypatch):
    """media id + sanitized name must fit Linux's 255-byte component limit,
    truncating the stem at a codepoint boundary while keeping the extension."""
    long_media_id = "m" * 100
    monkeypatch.setattr(mod, "_get_tmp_dir", lambda: str(tmp_path))
    raw = {
        "msgid": "kfmsg001",
        "send_time": 1700000000,
        "origin": 3,
        "msgtype": "file",
        "open_kfid": "wk_open_kfid",
        "external_userid": "wm_external_userid",
        "file": {"media_id": long_media_id},
    }
    msg = WechatKfMessage(
        msg=raw,
        client=_FakeClient('attachment; filename="' + "s" * 170 + '.pdf"'),
    )
    msg.prepare()

    name = Path(msg.content).name
    assert len(name.encode("utf-8")) <= 255
    assert name.startswith(f"{long_media_id}_")
    assert name.endswith(".pdf"), "the extension survives the truncation"
    assert Path(msg.content).read_bytes() == b"payload"
