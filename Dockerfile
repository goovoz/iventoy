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
    mkdir -p /tmp/unpack && \
    tar -xzf /tmp/iventoy.tar.gz -C /tmp/unpack && \
    rm -f /tmp/iventoy.tar.gz && \
    SRC=""; for d in /tmp/unpack/*/; do [ -d "$d" ] && { SRC="${d%/}"; break; }; done; \
    [ -n "$SRC" ] || SRC=/tmp/unpack; \
    mv "$SRC" /iventoy || exit 1; \
    test -f /iventoy/lib/iventoy || { echo "ERROR: 解包后未找到 /iventoy/lib/iventoy，上游包结构可能又变了"; ls -la /iventoy; exit 1; }; \
    chmod +x /iventoy/lib/iventoy && \
    mkdir -p /usr/share/iventoy/data && \
    cp /iventoy/data/iventoy.dat /iventoy/data/mac.db /usr/share/iventoy/data/

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

# [iventoy-fork] EXPOSE 只是文档性质（不影响实际监听），但与 iVentoy 真实端口保持一致：
#   26000 Web UI（固定，WebUI 端口本身不可改）
#   16000 HTTP 文件服务、10809 NBD、3260 iSCSI、10445 SMB、12049 NFS
#         —— 后五项取自 1.0.46 内置 WebUI 的默认值（get_config_all → http_port/nbd_port/
#            iscsi_port/smb_port/nfs_port），全部可在 Web UI 的 Configuration 页改
#   UDP 69 TFTP；UDP 67 与 4011 只在 iVentoy 自己跑 DHCP 时才用
#         （官方文档：ProxyNet 模式监听 UDP 67 + UDP 4011，Proxy 模式只监听 UDP 4011）
#   注意：67/4011 依赖二层广播，bridge 网络下收不到，必须 network_mode: host
EXPOSE 26000 16000 10809 3260 10445 12049
EXPOSE 69/udp 67/udp 4011/udp
ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]
