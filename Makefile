# [iventoy-fork] VERSION 从 Dockerfile 现读，不在 Makefile 里再存一份版本号：
# 之前 tag/push 目标写死 1.0.47，靠 track-iventoy-release.yml 的 sed 同步，
# 多一处需要同步的地方就多一次漏改的机会。
VERSION ?= $(shell sed -n 's/.*IVENTOY_VERSION:-\([0-9][0-9.]*\).*/\1/p' Dockerfile | head -1)
IMAGE   ?= ghcr.io/goovoz/iventoy
ISO_DIR ?= $(CURDIR)/isos

.PHONY: build buildx tag push test localtest

# 需要干净构建时手动加 DOCKER_BUILDKIT=1 docker build --no-cache .
build:
	docker build . -t iventoy:build

# 只验证两个架构都能构建，不产出可推送的制品（多平台无法 --load）
buildx:
	docker buildx build --platform linux/amd64,linux/arm64 . --output type=cacheonly

tag:
	docker tag iventoy:build $(IMAGE):latest
	docker tag iventoy:build $(IMAGE):$(VERSION)

push:
	docker push $(IMAGE):latest
	docker push $(IMAGE):$(VERSION)

test:
	sh tests/test-container-entrypoint.sh

localtest:
	docker run -it -p 26000:26000 -v "$(ISO_DIR)":/iventoy/iso --privileged --name iventoy_dev $(IMAGE):latest
