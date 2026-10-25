#!/usr/bin/env python3
"""清理 Docker Hub 仓库里多余的 tag（对齐 ghcr 的保留策略）。

保留：latest + Dockerfile 当前版本 + 次新版本（留一档回滚），其余 tag 删除。
也可用 HUB_KEEP_TAGS 显式指定（逗号分隔）。

与 ghcr 的差别（Hub 的 API 能力限制，必须知道）：
  - Hub 只开放「删 tag」，没有删 manifest 的公开接口。删掉 tag 后底层镜像会变成
    未标记状态，Hub 是否回收它不由我们控制 —— 所以这里的目标是「tag 列表干净」，
    不是「底层字节一定消失」。
  - 多架构 tag 的 digest 是索引 digest；子平台的 manifest 由 Hub 自己管，不用我们操心。

环境变量：
  DOCKERHUB_USERNAME / DOCKERHUB_TOKEN   必需（token 用 Hub 的 access token，不是登录密码）
  REGISTRY_IMAGE                         仓库名，默认 iventoy
  DRY_RUN                                true 只列计划
  HUB_KEEP_TAGS                          显式保留集合，留空则按默认策略算
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://hub.docker.com/v2"
USER = os.environ.get("DOCKERHUB_USERNAME", "")
TOKEN = os.environ.get("DOCKERHUB_TOKEN", "")
REPO_NAME = os.environ.get("REGISTRY_IMAGE", "iventoy")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
KEEP_INPUT = os.environ.get("HUB_KEEP_TAGS", "").strip()


def request(method, url, payload=None, headers=None):
    """发一次请求；HTTP 错误统一转成带状态码的异常，由调用方决定怎么报。"""
    data = json.dumps(payload).encode() if payload is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise RuntimeError("HTTP %s %s %s" % (e.code, url.split("/v2/")[-1], body)) from None


def login():
    try:
        out = request("POST", API + "/users/login", {"username": USER, "password": TOKEN})
    except RuntimeError as e:
        print("::error::Docker Hub 登录失败（%s）——检查 DOCKERHUB_TOKEN 是否有效/过期" % e)
        sys.exit(1)
    jwt = out.get("token")
    if not jwt:
        print("::error::Docker Hub 登录没有返回 token")
        sys.exit(1)
    return jwt


def list_tags(jwt):
    """分页取全量 tag。"""
    tags, page = [], 1
    while True:
        url = "%s/repositories/%s/%s/tags/?page_size=100&page=%d" % (API, USER, REPO_NAME, page)
        data = request("GET", url, headers={"Authorization": "JWT " + jwt})
        chunk = data.get("results") or []
        tags.extend(chunk)
        if len(chunk) < 100:
            return tags
        page += 1


def semver_key(tag):
    return tuple(int(x) for x in tag.split("."))


def current_version():
    try:
        with open("Dockerfile", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    m = re.search(r"IVENTOY_VERSION:-\s*([0-9]+\.[0-9]+\.[0-9]+)", text)
    return m.group(1) if m else None


def main():
    if not USER or not TOKEN:
        print("::error::缺少 DOCKERHUB_USERNAME / DOCKERHUB_TOKEN")
        return 1

    try:
        jwt = login()
        tags = list_tags(jwt)
    except RuntimeError as e:
        print("::error::读取 tag 列表失败：%s" % e)
        return 1

    names = [t["name"] for t in tags]
    print("Docker Hub %s/%s：%d 个 tag" % (USER, REPO_NAME, len(tags)))
    for t in sorted(tags, key=lambda x: x["name"]):
        print("   %-10s %s" % (t["name"], (t.get("digest") or "")[:19]))

    if KEEP_INPUT:
        keep = {x.strip() for x in KEEP_INPUT.split(",") if x.strip()}
        print("使用显式保留集合：%s" % ", ".join(sorted(keep)))
    else:
        cur = current_version()
        if not cur:
            print("::error::Dockerfile 里解析不出当前版本，拒绝按默认策略猜")
            return 1
        versions = sorted((n for n in names if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", n)),
                          key=semver_key)
        prev = None
        for v in versions:
            if semver_key(v) < semver_key(cur):
                prev = v
        keep = {"latest", cur}
        if prev:
            keep.add(prev)
        print("当前版本 %s，次新版本 %s" % (cur, prev or "<无>"))

    if "latest" not in keep:
        print("::error::保留集合里没有 latest，中止（以免把线上入口删掉）")
        return 1

    drop = [t for t in tags if t["name"] not in keep]
    if not drop:
        print("没有需要删除的 tag（已是最新状态）")
        _summary(len(tags), 0, keep)
        return 0

    print("\n将删除 %d 个 tag：%s" % (len(drop), ", ".join(t["name"] for t in drop)))
    if DRY_RUN:
        print("DRY RUN：未真正删除（DRY_RUN=false 才执行）")
        _summary(len(tags), len(drop), keep)
        return 0

    fail = 0
    for t in drop:
        name = t["name"]
        if name == "latest":
            print("  跳过 latest（双保险）")
            continue
        try:
            request("DELETE", "%s/repositories/%s/%s/tags/%s" % (API, USER, REPO_NAME, name),
                    headers={"Authorization": "JWT " + jwt})
            print("  已删除 %-10s %s" % (name, (t.get("digest") or "")[:19]))
        except RuntimeError as e:
            fail += 1
            print("::error::删除 %s 失败：%s" % (name, e))

    try:
        left = list_tags(jwt)
    except RuntimeError as e:
        print("::warning::删除后复查 tag 列表失败：%s" % e)
        left = []
    print("\n清理后剩余 %d 个 tag：%s" % (len(left), ", ".join(sorted(x["name"] for x in left))))
    if fail:
        print("::error::有 %d 个 tag 删除失败" % fail)
        return 1
    _summary(len(left), len(drop), keep)
    return 0


def _summary(remaining, deleted, keep):
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if not out:
        return
    try:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("### Docker Hub tag 清理\n\n")
            fh.write("| 项 | 值 |\n|---|---|\n")
            fh.write("| 保留集合 | %s |\n" % ", ".join(sorted(keep)))
            fh.write("| 删除 tag 数 | %d |\n" % deleted)
            fh.write("| 剩余 tag 数 | %d |\n" % remaining)
            fh.write("| 模式 | %s |\n" % ("dry-run" if DRY_RUN else "实际删除"))
            fh.write("\n> Hub 只开放删 tag 的接口，未标记的底层镜像由 Hub 自行回收。\n")
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
