# [iventoy-fork] 版本号只在 Dockerfile 里存一份，这里现读，免得两处对不上。
VERSION ?= $(shell sed -n 's/.*IVENTOY_VERSION:-\([0-9][0-9.]*\).*/\1/p' Dockerfile | head -1)
IMAGE   ?= ghcr.io/goovoz/iventoy
ISO_DIR ?= $(CURDIR)/isos
MAX_CLIENTS ?= 65535

.PHONY: build buildx tag push test localtest verify

build:
	docker build --build-arg IVENTOY_MAX_CLIENTS=$(MAX_CLIENTS) . -t iventoy:build

# 只验证两个架构都编得过，不产出可推送的制品（多平台不能 --load）
buildx:
	docker buildx build --platform linux/amd64,linux/arm64 \
		--build-arg IVENTOY_MAX_CLIENTS=$(MAX_CLIENTS) . --output type=cacheonly

tag:
	docker tag iventoy:build $(IMAGE):latest
	docker tag iventoy:build $(IMAGE):$(VERSION)

push:
	docker push $(IMAGE):latest
	docker push $(IMAGE):$(VERSION)

test:
	sh tests/test-container-entrypoint.sh

# 本地起一个来试，ISO 丢进 ./isos；用 host 网络（PXE/TFTP 的广播与 DHCP 全靠它），
# data 挂出来，重启后分机独启/配置才不会丢。
localtest:
	mkdir -p "$(CURDIR)/data-out"
	docker run -it --rm --privileged --net=host \
		-v "$(ISO_DIR)":/iventoy/iso \
		-v "$(CURDIR)/data-out":/iventoy/data \
		--name iventoy_dev $(IMAGE):latest

# 校验已发布镜像里的两份补丁报告与二进制是否对得上（不需要重新构建）
verify:
	docker run --rm --entrypoint cat $(IMAGE):latest /iventoy/lib/iventoy > /tmp/iventoy.shipped
	docker run --rm --entrypoint cat $(IMAGE):latest /usr/share/iventoy/patch.json > /tmp/iventoy.patch.json
	docker run --rm --entrypoint cat $(IMAGE):latest /usr/share/iventoy/patch-mb.json > /tmp/iventoy.patch-mb.json
	python3 patches/iventoy_patch.py --verify /tmp/iventoy.patch.json /tmp/iventoy.shipped
	python3 patches/matrix_boot_hook.py --verify /tmp/iventoy.patch-mb.json /tmp/iventoy.shipped
