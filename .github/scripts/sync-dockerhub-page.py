#!/usr/bin/env python3
"""把 DOCKERHUB.md 同步成 Docker Hub 仓库的说明页（短描述 + 正文 README）。

为什么不手工去网页里贴：README 是「怎么跑这个镜像」的唯一文档源，和镜像一起版本化、
一起发布才不会腐坏。这里在做完 push 之后调用一次，失败只告警不拦发布（文档不同步
不该让镜像发布变红）。

环境变量：
  DOCKERHUB_USERNAME   Docker Hub 用户名（与 Actions secret 同名）
  DOCKERHUB_TOKEN      Docker Hub access token（不是登录密码）
  REGISTRY_IMAGE       仓库名，默认 iventoy
  HUB_README           要上传的文件，默认 DOCKERHUB.md
  HUB_DESCRIPTION      短描述（Docker Hub 限制 100 字符），可用 HUB_DESCRIPTION_FILE 覆盖
"""
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

USER = os.environ.get("DOCKERHUB_USERNAME", "")
TOKEN = os.environ.get("DOCKERHUB_TOKEN", "")
REPO_NAME = os.environ.get("REGISTRY_IMAGE", "iventoy")
README_PATH = pathlib.Path(os.environ.get("HUB_README", "DOCKERHUB.md"))
DEFAULT_DESC = "iVentoy 容器镜像（amd64 free / arm64 trial）：一条命令搭起 PXE 启动服务器"


def request(method, url, payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def main():
    if not USER or not TOKEN:
        print("::error::缺少 DOCKERHUB_USERNAME / DOCKERHUB_TOKEN，无法同步 Docker Hub 页面")
        return 1
    if not README_PATH.is_file():
        print("::error::找不到 %s" % README_PATH)
        return 1

    repo = "%s/%s" % (USER, REPO_NAME)
    readme = README_PATH.read_text(encoding="utf-8")
    desc = os.environ.get("HUB_DESCRIPTION") or DEFAULT_DESC
    desc_file = os.environ.get("HUB_DESCRIPTION_FILE")
    if desc_file and pathlib.Path(desc_file).is_file():
        desc = pathlib.Path(desc_file).read_text(encoding="utf-8").strip()
    desc = desc.strip()[:100]

    print("Docker Hub 目标仓库：%s（README %d 字符，描述 %d 字符）" % (repo, len(readme), len(desc)))

    try:
        jwt = request("POST", "https://hub.docker.com/v2/users/login",
                      {"username": USER, "password": TOKEN}).get("token")
    except urllib.error.HTTPError as e:
        print("::error::Docker Hub 登录失败 HTTP %s（token 无效或已过期？）" % e.code)
        return 1
    if not jwt:
        print("::error::Docker Hub 没有返回 JWT")
        return 1

    try:
        out = request("PATCH", "https://hub.docker.com/v2/repositories/%s/" % repo,
                      {"full_description": readme, "description": desc},
                      {"Authorization": "JWT " + jwt})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        print("::error::更新 %s 失败 HTTP %s：%s" % (repo, e.code, body))
        return 1

    print("已更新：%s  README %d 字符  描述「%s」" % (
        out.get("name"), len(out.get("full_description") or ""), out.get("description")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
