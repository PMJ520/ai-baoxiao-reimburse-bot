# -*- coding: utf-8 -*-
"""钉钉开放接口的最小封装。

只用标准库，不引 requests——SDK 已经带了一个，再引一份没必要。

钉钉有两套并存的令牌，这是历史包袱：
  · 新接口（api.dingtalk.com）用 v1.0 的 accessToken
  · 老接口（oapi.dingtalk.com，媒体上传就在这儿）用 gettoken 拿的另一个
混用会报一个看不出原因的错，所以这里分开管、各自缓存。
"""
import json
import logging
import mimetypes
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

log = logging.getLogger(__name__)

OAPI = "https://oapi.dingtalk.com"
API = "https://api.dingtalk.com"
TIMEOUT = 30


class DingTalkError(RuntimeError):
    pass


def _post_json(url, body, headers=None, timeout=TIMEOUT):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"_raw": raw[:500]}


class Client:
    def __init__(self, client_id, client_secret):
        self.key, self.secret = client_id, client_secret
        self._lock = threading.Lock()
        self._v1 = self._v1_exp = None
        self._old = self._old_exp = None

    # ---- 令牌 ----

    def v1_token(self):
        with self._lock:
            if self._v1 and time.time() < self._v1_exp:
                return self._v1
            code, d = _post_json(f"{API}/v1.0/oauth2/accessToken",
                                 {"appKey": self.key, "appSecret": self.secret})
            if code != 200 or "accessToken" not in d:
                raise DingTalkError(f"取令牌失败 HTTP {code}: {d}")
            self._v1 = d["accessToken"]
            self._v1_exp = time.time() + int(d.get("expireIn", 7200)) - 300
            return self._v1

    def old_token(self):
        with self._lock:
            if self._old and time.time() < self._old_exp:
                return self._old
            q = urllib.parse.urlencode({"appkey": self.key, "appsecret": self.secret})
            with urllib.request.urlopen(f"{OAPI}/gettoken?{q}", timeout=TIMEOUT) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            if d.get("errcode") != 0:
                raise DingTalkError(f"取旧版令牌失败: {d}")
            self._old = d["access_token"]
            self._old_exp = time.time() + int(d.get("expires_in", 7200)) - 300
            return self._old

    def _hdr(self):
        return {"x-acs-dingtalk-access-token": self.v1_token()}

    # ---- 收 ----

    def download(self, download_code, robot_code):
        """用 downloadCode 换下载地址再取内容。"""
        code, d = _post_json(f"{API}/v1.0/robot/messageFiles/download",
                             {"downloadCode": download_code, "robotCode": robot_code},
                             self._hdr())
        if code != 200 or not d.get("downloadUrl"):
            raise DingTalkError(f"换取下载地址失败 HTTP {code}: {d}")
        with urllib.request.urlopen(d["downloadUrl"], timeout=120) as r:
            return r.read()

    # ---- 发 ----

    def send(self, robot_code, msg_key, param, *, user_id=None, conversation_id=None):
        """发消息。群聊传 conversation_id，单聊传 user_id。

        实测：群聊要的 openConversationId 就是回调里那个 conversationId。
        """
        body = {"robotCode": robot_code, "msgKey": msg_key,
                "msgParam": json.dumps(param, ensure_ascii=False)}
        if conversation_id:
            body["openConversationId"] = conversation_id
            url = f"{API}/v1.0/robot/groupMessages/send"
        else:
            body["userIds"] = [user_id]
            url = f"{API}/v1.0/robot/oToMessages/batchSend"
        code, d = _post_json(url, body, self._hdr())
        if code != 200:
            raise DingTalkError(f"发送失败 HTTP {code}: {d}")
        # 单聊会回这两个列表，非空说明对方收不到；群聊只回受理凭证，没法细究
        bad = (d.get("invalidStaffIdList") or []) + (d.get("filteredStaffIdList") or [])
        if bad:
            log.warning("钉钉消息未送达: %s", bad)
        return d

    def upload_media(self, path, media_type="file"):
        """媒体上传。走老接口，multipart 手搓，为的是不引额外依赖。"""
        name = os.path.basename(path)
        with open(path, "rb") as fh:
            blob = fh.read()
        boundary = uuid.uuid4().hex
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="type"\r\n\r\n{media_type}\r\n'.encode(),
            f"--{boundary}\r\n".encode(),
            (f'Content-Disposition: form-data; name="media"; filename="{name}"\r\n'
             f"Content-Type: {ctype}\r\n\r\n").encode(),
            blob, b"\r\n", f"--{boundary}--\r\n".encode(),
        ])
        q = urllib.parse.urlencode({"access_token": self.old_token(), "type": media_type})
        req = urllib.request.Request(
            f"{OAPI}/media/upload?{q}", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        if d.get("errcode") != 0 or not d.get("media_id"):
            raise DingTalkError(f"媒体上传失败: {d}")
        return d["media_id"]
