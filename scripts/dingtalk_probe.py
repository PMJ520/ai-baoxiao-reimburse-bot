# -*- coding: utf-8 -*-
"""钉钉 Stream 模式探针。

用途：在写正式通道之前，先看清钉钉真正推过来的消息长什么样。

之所以要这一步：SDK 只解析 text / picture / richText 三种，文件消息它不认，
未识别的字段会落进 extensions。文件到底推不推、字段叫什么，文档说不清，
只有真发一个 PDF 过来才知道。照文档猜着写、写完说"应该能用"，
这个亏前面吃过一次。

用法：
    export DINGTALK_CLIENT_ID=...      # AppKey，别写进命令行历史
    export DINGTALK_CLIENT_SECRET=...
    python scripts/dingtalk_probe.py

    # 顺带测发文件（收到任意消息后，把该文件发回给发送者）
    python scripts/dingtalk_probe.py --send-back /path/to/x.xlsx

依赖：pip install dingtalk-stream
"""
import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

OAPI = "https://oapi.dingtalk.com"
API = "https://api.dingtalk.com"


def _post(url, body, headers=None, timeout=20):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"_raw": raw[:500]}


def _get_json(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


class Creds:
    """两套令牌并存是钉钉的历史包袱：新接口（api.dingtalk.com）用 v1.0 token，
    老接口（oapi.dingtalk.com，媒体上传就在这儿）用 gettoken 拿的另一个。
    混用会报一个看不出原因的错，这里分开管。"""

    def __init__(self, key, secret):
        self.key, self.secret = key, secret
        self._v1 = self._v1_exp = None
        self._old = self._old_exp = None

    def v1(self):
        if self._v1 and time.time() < self._v1_exp:
            return self._v1
        code, d = _post(f"{API}/v1.0/oauth2/accessToken",
                        {"appKey": self.key, "appSecret": self.secret})
        if code != 200 or "accessToken" not in d:
            raise RuntimeError(f"取 v1.0 令牌失败 HTTP {code}: {d}")
        self._v1 = d["accessToken"]
        self._v1_exp = time.time() + int(d.get("expireIn", 7200)) - 120
        return self._v1

    def old(self):
        if self._old and time.time() < self._old_exp:
            return self._old
        q = urllib.parse.urlencode({"appkey": self.key, "appsecret": self.secret})
        d = _get_json(f"{OAPI}/gettoken?{q}")
        if d.get("errcode") != 0:
            raise RuntimeError(f"取旧版令牌失败: {d}")
        self._old = d["access_token"]
        self._old_exp = time.time() + int(d.get("expires_in", 7200)) - 120
        return self._old


def download(creds, download_code, robot_code):
    """用 downloadCode 换下载地址再取内容。返回 (字节数, 前 8 字节, 说明)。"""
    code, d = _post(f"{API}/v1.0/robot/messageFiles/download",
                    {"downloadCode": download_code, "robotCode": robot_code},
                    {"x-acs-dingtalk-access-token": creds.v1()})
    if code != 200 or not d.get("downloadUrl"):
        return None, None, f"换取下载地址失败 HTTP {code}: {d}"
    try:
        with urllib.request.urlopen(d["downloadUrl"], timeout=60) as r:
            blob = r.read()
        return len(blob), blob[:8], "成功"
    except Exception as e:                      # noqa: BLE001
        return None, None, f"下载失败: {type(e).__name__}: {e}"


def upload_media(creds, path):
    """媒体上传走老接口，multipart 手搓——为的是整个探针只依赖标准库。"""
    name = os.path.basename(path)
    with open(path, "rb") as fh:
        blob = fh.read()
    ftype = "file"
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="type"\r\n\r\n{ftype}\r\n'.encode(),
        f"--{boundary}\r\n".encode(),
        (f'Content-Disposition: form-data; name="media"; filename="{name}"\r\n'
         f"Content-Type: {ctype}\r\n\r\n").encode(),
        blob, b"\r\n", f"--{boundary}--\r\n".encode(),
    ])
    q = urllib.parse.urlencode({"access_token": creds.old(), "type": ftype})
    req = urllib.request.Request(
        f"{OAPI}/media/upload?{q}", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def send_text(creds, robot_code, text, *, user_id=None, conversation_id=None):
    """用 REST 接口发文字——正式通道走的就是这条路。

    不用 SDK 的 reply_text：它依赖回调里的 sessionWebhook，而那东西会过期，
    处理耗时长一点的回复就发不出去了。REST 靠 robotCode + 收件人，不过期。

    群聊要传 openConversationId。钉钉文档里它和回调给的 conversationId
    是不是同一个东西，说法不明——这里直接拿 conversationId 去试，
    通了就说明是同一个。
    """
    param = json.dumps({"content": text}, ensure_ascii=False)
    hdr = {"x-acs-dingtalk-access-token": creds.v1()}
    if conversation_id:
        return _post(f"{API}/v1.0/robot/groupMessages/send",
                     {"robotCode": robot_code, "openConversationId": conversation_id,
                      "msgKey": "sampleText", "msgParam": param}, hdr)
    return _post(f"{API}/v1.0/robot/oToMessages/batchSend",
                 {"robotCode": robot_code, "userIds": [user_id],
                  "msgKey": "sampleText", "msgParam": param}, hdr)


def send_file(creds, robot_code, *, media_id, filename, user_id=None,
              conversation_id=None):
    param = json.dumps({"mediaId": media_id, "fileName": filename,
                        "fileType": filename.rsplit(".", 1)[-1]},
                       ensure_ascii=False)
    hdr = {"x-acs-dingtalk-access-token": creds.v1()}
    if conversation_id:
        return _post(f"{API}/v1.0/robot/groupMessages/send",
                     {"robotCode": robot_code, "openConversationId": conversation_id,
                      "msgKey": "sampleFile", "msgParam": param}, hdr)
    return _post(f"{API}/v1.0/robot/oToMessages/batchSend",
                 {"robotCode": robot_code, "userIds": [user_id],
                  "msgKey": "sampleFile", "msgParam": param}, hdr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", default=os.environ.get("DINGTALK_CLIENT_ID", ""))
    ap.add_argument("--client-secret", default=os.environ.get("DINGTALK_CLIENT_SECRET", ""))
    ap.add_argument("--send-back", metavar="FILE",
                    help="收到消息后把这个文件发回去，用来验证发文件能力")
    a = ap.parse_args()
    if not (a.client_id and a.client_secret):
        sys.exit("缺少凭证：设置 DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET")
    if a.send_back and not os.path.isfile(a.send_back):
        sys.exit(f"文件不存在：{a.send_back}")

    try:
        import dingtalk_stream
    except ImportError:
        sys.exit("请先安装：pip install dingtalk-stream")

    creds = Creds(a.client_id, a.client_secret)
    try:
        creds.v1(); creds.old()
        print("✅ 两套令牌都取到了（凭证有效）")
    except Exception as e:                      # noqa: BLE001
        sys.exit(f"❌ 凭证校验失败：{e}")

    class Probe(dingtalk_stream.ChatbotHandler):
        async def process(self, callback):
            raw = callback.data
            print("\n" + "=" * 68)
            print("原始消息结构（这就是我们要看的东西）")
            print("=" * 68)
            print(json.dumps(raw, ensure_ascii=False, indent=2)[:4000])

            mt = raw.get("msgtype")
            robot = raw.get("robotCode") or raw.get("chatbotUserId") or ""
            conv_type = raw.get("conversationType")      # 1 单聊 2 群聊
            print(f"\n消息类型: {mt}   会话类型: {conv_type}   robotCode: {bool(robot)}")

            # 把所有出现的 downloadCode 都试着下一遍，不管它藏在哪一层
            codes = []

            def walk(o, path=""):
                if isinstance(o, dict):
                    for k, v in o.items():
                        if k == "downloadCode" and isinstance(v, str):
                            codes.append((path + "/" + k, v))
                        walk(v, path + "/" + k)
                elif isinstance(o, list):
                    for i, v in enumerate(o):
                        walk(v, f"{path}[{i}]")

            walk(raw)
            if codes:
                print(f"\n发现 {len(codes)} 个 downloadCode，逐个验证下载：")
                for where, c in codes:
                    n, head, note = download(creds, c, robot)
                    print(f"  {where}: {note}"
                          + (f"，{n:,} 字节，头部 {head!r}" if n else ""))
            else:
                print("\n没有 downloadCode —— 这条消息里没有可下载的附件")

            # 验证 REST 发送：群聊用 conversationId 当 openConversationId
            uid = raw.get("senderStaffId")
            cid = raw.get("conversationId") if conv_type == "2" else None
            print("\n试用 REST 接口发文字"
                  + ("（群聊，拿 conversationId 当 openConversationId）"
                     if cid else "（单聊）") + " …")
            st, resp = send_text(creds, robot, "探针：REST 发送测试",
                                 user_id=uid, conversation_id=cid)
            print(f"  HTTP {st}: " + json.dumps(resp, ensure_ascii=False)[:300])
            if cid:
                print("  → 收到这条就说明 conversationId 可以直接当 openConversationId 用")

            if a.send_back:
                print(f"\n试发文件 {os.path.basename(a.send_back)} …")
                try:
                    up = upload_media(creds, a.send_back)
                    print("  上传结果:", json.dumps(up, ensure_ascii=False)[:200])
                    mid = up.get("media_id")
                    if mid:
                        st, resp = send_file(
                            creds, robot, media_id=mid,
                            filename=os.path.basename(a.send_back),
                            user_id=uid, conversation_id=cid)
                        print(f"  发送结果 HTTP {st}: "
                              + json.dumps(resp, ensure_ascii=False)[:300])
                except Exception as e:          # noqa: BLE001
                    print(f"  ❌ {type(e).__name__}: {e}")

            self.reply_text(f"探针收到：{mt}（详情见服务端日志）",
                            dingtalk_stream.ChatbotMessage.from_dict(raw))
            return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    cred = dingtalk_stream.Credential(a.client_id, a.client_secret)
    client = dingtalk_stream.DingTalkStreamClient(cred)
    client.register_callback_handler(
        dingtalk_stream.chatbot.ChatbotMessage.TOPIC, Probe())
    print("\n长连接启动中…  现在去钉钉里给这个机器人发消息：")
    print("  1) 一段文字    2) 一张图片    3) 一个 PDF 文件")
    print("按 Ctrl+C 结束\n")
    client.start_forever()


if __name__ == "__main__":
    main()
