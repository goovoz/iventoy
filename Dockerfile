# [iventoy-fork] 两阶段：init 只管下载解压，final 只放运行时。
# final 不能换 alpine —— iVentoy 和 lib/lin64 里的 .so 都是 glibc 链接，musl 加载不起来。
# alpine 只出现在 init 阶段，跟着它的两年支持期走（3.20 已经 EOL 了，别退回去）。
FROM alpine:3.24 AS init

ARG IVENTOY_VERSION
ENV IVENTOY_VERSION=${IVENTOY_VERSION:-1.0.50}
ARG TARGETARCH

# [iventoy-fork] 上游 1.0.47 起随 release 发 sha256.txt。有就强制校验，没有只告警 ——
# 重建老版本时上游还没这个文件，不能因此构建失败。
# 取 hash 用 ${ACTUAL%% *}，别写成 `sha256sum | awk`：这里的 /bin/sh 没有 pipefail，
# 前段失败会被 awk 吞成空串，最后报的是「mismatch (got )」，排查时很绕。
RUN case "${TARGETARCH}" in \
      amd64) ARCH_SUFFIX="x86_64"; EDITION="free" ;; \
      arm64) ARCH_SUFFIX="arm64"; EDITION="trial" ;; \
      *) echo "Unsupported architecture: ${TARGETARCH}"; exit 1 ;; \
    esac && \
    ASSET="iventoy-${IVENTOY_VERSION}-linux-${ARCH_SUFFIX}-${EDITION}.tar.gz" && \
    BASE="https://github.com/ventoy/PXE/releases/download/v${IVENTOY_VERSION}" && \
    echo "downloading ${BASE}/${ASSET}" && \
    wget -q --timeout=60 --tries=3 "${BASE}/${ASSET}" -O /tmp/iventoy.tar.gz || { echo "ERROR: 下载 ${ASSET} 失败"; exit 1; } && \
    if wget -q --timeout=60 --tries=3 "${BASE}/sha256.txt" -O /tmp/sha256.txt; then \
        EXPECTED=$(awk -v f="${ASSET}" '$2 == f || $2 == "./" f {print $1; exit}' /tmp/sha256.txt); \
        if [ -n "${EXPECTED}" ]; then \
            ACTUAL=$(sha256sum /tmp/iventoy.tar.gz); \
            ACTUAL=${ACTUAL%% *}; \
            [ -n "${ACTUAL}" ] || { echo "ERROR: 读不出 ${ASSET} 的 sha256（sha256sum 失败）"; exit 1; }; \
            [ "${ACTUAL}" = "${EXPECTED}" ] || { echo "ERROR: ${ASSET} sha256 mismatch (expected ${EXPECTED}, got ${ACTUAL})"; exit 1; }; \
            echo "sha256 verified: ${EXPECTED}"; \
        else \
            echo "WARN: sha256.txt 中没有 ${ASSET} 的条目，跳过校验"; \
        fi; \
    else \
        echo "WARN: 上游未提供 sha256.txt（v${IVENTOY_VERSION}），跳过校验"; \
    fi && \
    mkdir -p /tmp/unpack && \
    tar -xzf /tmp/iventoy.tar.gz -C /tmp/unpack && \
    rm -f /tmp/iventoy.tar.gz /tmp/sha256.txt && \
    SRC=""; for d in /tmp/unpack/*/; do [ -d "$d" ] && { SRC="${d%/}"; break; }; done; \
    [ -n "$SRC" ] || SRC=/tmp/unpack; \
    mv "$SRC" /iventoy || exit 1; \
    test -f /iventoy/lib/iventoy || { echo "ERROR: 解包后未找到 /iventoy/lib/iventoy，上游包结构可能又变了"; ls -la /iventoy; exit 1; }; \
    chmod +x /iventoy/lib/iventoy && \
    mkdir -p /usr/share/iventoy/data && \
    cp /iventoy/data/iventoy.dat /iventoy/data/mac.db /usr/share/iventoy/data/

# [iventoy-fork] 补丁阶段：解限（客户端上限 / ARM64+LoongArch 放行 / 自校验与端口反篡改）。
# 跑在 BUILDPLATFORM 上：补丁是纯字节改写，与架构无关，不需要 qemu、也不用给两个架构各装一份 python。
# 上游改版后若指令签名对不上，这里会让构建失败，不会产出「能起但功能残缺」的镜像。
# 每个被改的站点、改前/改后字节都写进 /iventoy-patch.json，随镜像一起发布，便于事后对账。
# 维护方法见 patches/README.md。
FROM --platform=$BUILDPLATFORM python:3.12-alpine AS patch

COPY --from=init /iventoy /iventoy
COPY patches/ /patches/

# [iventoy-fork] 默认 65535：等于一个 /16 网段的主机数，也是 arm64 单条 MOVZ 能编出来的上限。
# 容量变化会同步放大 .bss 段（脚本按记录数算 p_memsz），不是把常量改大那么简单。
ARG IVENTOY_MAX_CLIENTS=65535
RUN python3 /patches/iventoy_patch.py --max-clients "${IVENTOY_MAX_CLIENTS}" \
        --report /iventoy-patch.json /iventoy/lib/iventoy && \
    cat /iventoy-patch.json

# [iventoy-fork L2] 「分机独启」(matrix boot) 在免费版里被裁成了桩：add 接口直接回
# {"result":"notsupport"}，数据层的 toy_matrix_boot_add 也是 return -22。
# 这里把 add 这条通路补回来（list/del/save/proc 本来就在）。
#
# 为什么单独一个阶段、而且跑在 TARGETPLATFORM 上：补丁本身是纯字节改写（python 就够），
# 但这段补齐要**编译**一小段机器码，需要目标架构的编译器。放在目标平台阶段里用本机 gcc
# 最省事（arm64 在 qemu 下跑，多花一分钟），不用在 x86 阶段里塞交叉工具链。
FROM --platform=$TARGETPLATFORM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS mbhook

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=patch /iventoy /iventoy
COPY --from=patch /iventoy-patch.json /iventoy-patch.json
COPY patches/ /patches/

RUN python3 /patches/matrix_boot_hook.py /iventoy/lib/iventoy \
        --report /iventoy-patch-mb.json && \
    python3 /patches/matrix_boot_hook.py /iventoy/lib/iventoy \
        --verify /iventoy-patch-mb.json

# [iventoy-fork] 底包 Debian 13（trixie）。12 那批基础包有一堆 Debian 明确不修、或者还没修的
# 漏洞，扫描一片红；换 13 之后少了一大截。别改回 bookworm。
#
# 钉 digest 是为了构建可复现 —— trixie-slim 是滚动 tag，隔一周拉到的未必是同一份。
# 要跟新底包就重取一次：docker buildx imagetools inspect debian:trixie-slim
# 取页面顶部的 INDEX DIGEST，不是右边按架构分那个。钉成单架构的，另一个架构会静默装错底包
# （钉 386 的清单，amd64 能「构建成功」，但里面是 i386）。
# 漏洞数、体积对比、为什么换底包而不只是 apt upgrade，都记在 docs/UPSTREAM-SYNC.md。
FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad

# [iventoy-fork] 顺手把基础包的安全更新装上，不用等上游发版。
# 不新增包，只升级已有的；apt 列表用完就删，不留在镜像里。
RUN apt-get update \
    && apt-get -y upgrade \
    && rm -rf /var/lib/apt/lists/*

# [iventoy-fork] 这里故意不声明 IVENTOY_VERSION：它是构建期的，运行时改不了，
# 留着只会让人以为 docker run -e 能换版本。

COPY --from=mbhook /iventoy /iventoy
COPY --from=init /usr/share/iventoy/data /usr/share/iventoy/data
COPY --from=mbhook /iventoy-patch.json /usr/share/iventoy/patch.json
COPY --from=mbhook /iventoy-patch-mb.json /usr/share/iventoy/patch-mb.json

COPY --chmod=755 files/docker-entrypoint.sh /usr/local/bin/docker-entrypoint

RUN ln -sf /proc/1/fd/1 /iventoy/log/log.txt

# [iventoy-fork] 镜像里没有 curl/wget，直接看 /proc/net：26000 = 0x6590，LISTEN = 0A。
# 只说明 Web UI 起来了，不代表 PXE 服务在跑（那个看 IVENTOY_AUTO_RUN）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/bin/sh", "-c", "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | grep -qE ':6590 [0-9A-F:]+ 0A'"]

# [iventoy-fork] EXPOSE 只是文档，不决定真实监听。26000 是 Web UI，固定；
# 其余是 iVentoy 的默认端口，都能在 Configuration 页改，改了记得一起改这里。
# 67/4011/69 走二层广播，bridge 网络收不到，得用 network_mode: host。
EXPOSE 26000 16000 10809 3260 10445 12049
EXPOSE 69/udp 67/udp 4011/udp
ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]
