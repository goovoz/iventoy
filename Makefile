# [iventoy-fork] 版本号只在 Dockerfile 里存一份，这里现读，免得两处对不上。
VERSION ?= $(shell sed -n 's/.*IVENTOY_VERSION:-\([0-9][0-9.]*\).*/\1/p' Dockerfile | head -1)
IMAGE   ?= ghcr.io/goovoz/iventoy
ISO_DIR ?= $(CURDIR)/isos

.PHONY: build buildx tag push test localtest

build:
	docker build . -t iventoy:build

# 只验证两个架构都编得过，不产出可推送的制品（多平台不能 --load）
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

# 本地起一个来试，ISO 丢进 ./isos
localtest:
	docker run -it -p 26000:26000 -v "$(ISO_DIR)":/iventoy/iso --privileged --name iventoy_dev $(IMAGE):latest
