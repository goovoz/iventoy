.PHONY: build buildx tag push test localtest

build:
	docker build . -t iventoy:build --no-cache

buildx:
	docker buildx build --platform linux/amd64,linux/arm64 . -t iventoy:build --no-cache

tag:
	docker tag iventoy:build ghcr.io/goovoz/iventoy:latest
	docker tag iventoy:build ghcr.io/goovoz/iventoy:1.0.45

push:
	docker push ghcr.io/goovoz/iventoy:latest
	docker push ghcr.io/goovoz/iventoy:1.0.45

test:
	sh tests/test-container-entrypoint.sh

localtest:
	docker run -it -p 26000:26000 -v /home/gary/developer/isos:/iventoy/iso --privileged --name iventoy_dev  ghcr.io/goovoz/iventoy:latest
