# [iventoy-fork] 多阶段构建：builder 只用 busybox 下载解压，final 只承载运行时，镜像内零 apt 包。
# final 不能用 alpine 瘦身：iVentoy 二进制与 lib/lin64 捆绑库均为 glibc 链接（ldd 实证），
# musl 环境无法加载。底包选型依据详见 docs/UPSTREAM-SYNC.md「底包选型」。
FROM alpine:3.20 AS init

ARG IVENTOY_VERSION
ENV IVENTOY_VERSION=${IVENTOY_VERSION:-1.0.46}
ARG TARGETARCH

RUN case "${TARGETARCH}" in \
      amd64) ARCH_SUFFIX="x86_64"; EDITION="free" ;; \
      arm64) ARCH_SUFFIX="arm64"; EDITION="trial" ;; \
      *) echo "Unsupported architecture: ${TARGETARCH}"; exit 1 ;; \
    esac && \
    wget -q "https://github.com/ventoy/PXE/releases/download/v${IVENTOY_VERSION}/iventoy-${IVENTOY_VERSION}-linux-${ARCH_SUFFIX}-${EDITION}.tar.gz" -O /tmp/iventoy.tar.gz && \
    tar -xzf /tmp/iventoy.tar.gz -C /tmp && \
    mv "/tmp/iventoy-${IVENTOY_VERSION}" /iventoy && \
    chmod +x /iventoy/lib/iventoy && \
    mkdir -p /usr/share/iventoy/data && \
    cp /iventoy/data/iventoy.dat /iventoy/data/mac.db /usr/share/iventoy/data/ && \
    rm -f /tmp/iventoy.tar.gz

FROM debian:bookworm-slim

ARG IVENTOY_VERSION
ENV IVENTOY_VERSION=${IVENTOY_VERSION:-1.0.46}

COPY --from=init /iventoy /iventoy
COPY --from=init /usr/share/iventoy/data /usr/share/iventoy/data

COPY --chmod=755 files/docker-entrypoint.sh /usr/local/bin/docker-entrypoint

RUN ln -sf /proc/1/fd/1 /iventoy/log/log.txt

# [iventoy-fork] 镜像内刻意不装 curl/wget（零 apt 包），用 /proc/net 探测 Web GUI
# 26000 端口的 LISTEN 状态做健康检查：26000 = 0x6590，LISTEN = 0A。
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | grep -qE ':6590 [0-9A-F:]+ 0A'

EXPOSE 26000 16000 10809 69/udp
ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]
