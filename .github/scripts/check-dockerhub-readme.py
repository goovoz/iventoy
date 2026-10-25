#!/usr/bin/env python3
"""校验 DOCKERHUB.md 里内嵌的 compose 与 deploy/docker-compose.yml 一致。

Docker Hub 的说明页放了可直接复制的 compose，仓库里又有 deploy/docker-compose.yml
给 `curl -O` 用 —— 两份内容必须一样，否则部署方会照着过期的那份跑。CI 在这里拦一道。

比对时忽略：行尾空白、连续空行、注释行（部署方那份刻意不带注释）。
"""
import pathlib
import sys

README = pathlib.Path("DOCKERHUB.md")
DEPLOY = pathlib.Path("deploy/docker-compose.yml")


def normalize(text):
    out = []
    for line in text.splitlines():
        line = line.rstrip()
        if line.lstrip().startswith("#"):
            continue
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return out


def extract_compose(readme_text):
    blocks, cur, inside = [], [], False
    for line in readme_text.splitlines():
        if line.strip().startswith("```"):
            if inside:
                blocks.append("\n".join(cur))
                cur, inside = [], False
            else:
                cur, inside = [], True
            continue
        if inside:
            cur.append(line)
    for block in blocks:
        if block.lstrip().startswith("services:"):
            return block
    return None


def main():
    if not README.is_file() or not DEPLOY.is_file():
        print("::error::缺少 %s 或 %s" % (README, DEPLOY))
        return 1

    inline = extract_compose(README.read_text(encoding="utf-8"))
    if inline is None:
        print("::error::DOCKERHUB.md 里没找到以 services: 开头的 yaml 代码块")
        return 1

    a = normalize(inline)
    b = normalize(DEPLOY.read_text(encoding="utf-8"))
    if a == b:
        print("DOCKERHUB.md 的 compose 与 deploy/docker-compose.yml 一致（%d 行）" % len(a))
        return 0

    print("::error::DOCKERHUB.md 的 compose 与 deploy/docker-compose.yml 不一致")
    import difflib
    for line in difflib.unified_diff(b, a, fromfile="deploy/docker-compose.yml", tofile="DOCKERHUB.md", lineterm=""):
        print(line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
