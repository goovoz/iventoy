# iVentoy（容器镜像）

把 [iVentoy](https://www.iventoy.com/) 的 Linux 版封进容器：一台机器就能当 PXE 启动服务器，客户端从网络直接启动 ISO，不用刻盘。

- 镜像：`deemop/iventoy`
- 源仓库：[goovoz/iventoy](https://github.com/goovoz/iventoy)（上游 [ventoy/PXE](https://github.com/ventoy/PXE) 的容器化封装）
- 平台：`linux/amd64`（使用上游 free 版资产）、`linux/arm64`（使用上游 **trial** 版资产）

## 快速开始

```bash
mkdir -p iventoy-data iventoy-iso iventoy-driver iventoy-user
curl -O https://raw.githubusercontent.com/goovoz/iventoy/main/deploy/docker-compose.yml
docker compose up -d
```

然后浏览器打开 `http://<宿主机IP>:26000`。

`deploy/docker-compose.yml` 的内容就是下面这份，可以直接复制：

```yaml
services:
  iventoy:
    image: deemop/iventoy:latest
    container_name: iventoy
    restart: unless-stopped
    network_mode: host
    privileged: true
    init: true
    stop_grace_period: 30s
    environment:
      TZ: Asia/Shanghai
      IVENTOY_AUTO_RUN: "0"
    volumes:
      - ./iventoy-data:/iventoy/data
      - ./iventoy-iso:/iventoy/iso
      - ./iventoy-driver:/iventoy/driver
      - ./iventoy-user:/iventoy/user
    logging:
      driver: json-file
      options:
        max-size: "20m"
        max-file: "5"
```

不想用 compose 也可以：

```bash
docker run -d --name iventoy --network host --privileged --init \
  --restart unless-stopped \
  -e TZ=Asia/Shanghai -e IVENTOY_AUTO_RUN=0 \
  -v "$PWD/iventoy-data:/iventoy/data" \
  -v "$PWD/iventoy-iso:/iventoy/iso" \
  -v "$PWD/iventoy-driver:/iventoy/driver" \
  -v "$PWD/iventoy-user:/iventoy/user" \
  deemop/iventoy:latest
```

## 首次使用三步

1. 打开 `http://<宿主机IP>:26000`，进 **Configuration**，把 **Server IP** 选成宿主机那块网卡的地址。
2. 选 **DHCP 模式**：局域网已有 DHCP → `External`；要让 iVentoy 自己发地址 → `ProxyNet`。
3. 回到 compose，把 `IVENTOY_AUTO_RUN` 改成 `"1"`，`docker compose up -d` 重跑。之后容器启动就会自动带起 PXE 服务。

> 第 3 步别提前做。Server IP 没配对就自动启动，管理服务会立刻退出（上游行为：它只检查 `IVENTOY_AUTO_RUN` 是否存在，不看值）。

## 端口

`network_mode: host` 下不需要端口映射，容器直接监听宿主机端口：

| 端口 | 协议 | 用途 | 能否改 |
|---|---|---|---|
| 26000 | TCP | Web 管理界面 | 否（固定） |
| 16000 | TCP | HTTP 文件服务（启动文件 / ISO 下载） | Web UI 可改 |
| 10809 | TCP | NBD | Web UI 可改 |
| 3260 | TCP | iSCSI | Web UI 可改 |
| 10445 | TCP | SMB | Web UI 可改 |
| 12049 | TCP | NFS | Web UI 可改 |
| 69 | UDP | TFTP | Web UI 可改 |
| 67 | UDP | 内置 DHCP（自建 DHCP 模式才用） | Web UI 可改 |
| 4011 | UDP | proxyDHCP（Proxy / ProxyNet 模式才用） | Web UI 可改 |

除 26000 外都能在 Configuration 页改；改了记得同步防火墙。

## 数据目录

| 容器路径 | 内容 | 说明 |
|---|---|---|
| `/iventoy/data` | `config.dat` 等配置 | 升级/重建容器都保留 |
| `/iventoy/iso` | ISO 镜像 | 放这里就能在 Web UI 里看到 |
| `/iventoy/driver` | Windows 驱动包 | 可选 |
| `/iventoy/user` | 自定义启动文件 | 可选 |

`iventoy.dat` 和 `mac.db` 是随发行版走的，容器每次启动都会用镜像内自带版本覆盖，不要手动改。

## 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `IVENTOY_AUTO_RUN` | 未设置 | 设为 `1` 时容器启动即拉起 PXE 服务；其它值等同于不设置 |
| `TZ` | `UTC` | 时区，如 `Asia/Shanghai` |
| `IVENTOY_API_ALL` | `1` | 放开 HTTP API |
| `IVENTOY_DIR` | `/iventoy` | 程序目录（一般不用改） |
| `IVENTOY_DEFAULT_DATA_DIR` | `/usr/share/iventoy/data` | 镜像内默认数据目录（一般不用改） |

## 为什么必须是 host 网络

PXE 依赖二层广播：客户端用 DHCP/TFTP 找服务器，广播报文进不了 Docker 的 bridge NAT。所以要么 `network_mode: host`，要么 `--network host`。

用 bridge 模式只能做「纯文件服务」（HTTP/NBD/iSCSI 里靠单播的那部分），DHCP 与 proxyDHCP 一定不通。

`privileged: true` 也是必需的：iVentoy 要操作网络接口、绑定低位端口，并在运行时改配置。

## 版本与回滚

每个上游版本都同时发布 `latest` 和精确版本号：

```yaml
image: deemop/iventoy:1.0.47   # 钉死版本，升级由你决定
```

`latest` 只在「上游出新版」时前移，不会被旧 tag 的重建回退。出问题就改回上一个版本号，`docker compose up -d` 即可。

## 安全与漏洞扫描

镜像基于 `debian:trixie-slim`（Debian 13）。Docker Hub 或 trivy 扫出来的 CRITICAL / HIGH /
MEDIUM / LOW **全部来自 Debian 的基础包，不是 iVentoy 自身**——iVentoy 的二进制与自带库
零命中。选 trixie 而不是 bookworm 就是冲着这个：同样口径下扫描量从 227 条降到 161 条
（HIGH 56 → 43，CRITICAL 1 → 0）。

发布流水线每次都会做两件事：

- 用 trivy 扫**实际发布出去的那个镜像**，出现 CRITICAL 直接让发布失败；HIGH 及以下写进 Job Summary 留痕
- 按 amd64 / arm64 各起一次容器，确认 Web UI（26000）能应答——换底包、换上游版本最容易坏的就是「起不来」

Debian 基础包的安全更新在构建时就随 `apt-get upgrade` 进镜像，不必等上游 iVentoy 发新版本。

使用上还有两点值得注意：容器必须 `privileged`（iVentoy 要挂载目录、操作网卡），
所以请只把它放在你信任的内网里；Web UI 的 26000 不要直接暴露到公网。

## 限制与注意

- **arm64 是 trial 版**：上游只给 arm64 提供 trial 资产，功能/授权以官网为准。
- **Docker Hub 匿名拉取有限流**，批量部署建议先 `docker pull` 到内网 registry 做缓存。
- **管理服务健康 ≠ PXE 在跑**：镜像的 HEALTHCHECK 只探 26000 是否 LISTEN。PXE 是否真的起来了，看 Web UI 状态或 `docker logs iventoy`。
- **日志已限制大小**：镜像把 `log/log.txt` 软链到容器 stdout，compose 里配了 `max-size=20m / max-file=5`，不配会长期写满宿主盘。
- **停止需要 30 秒**：iVentoy 收到 SIGTERM 要落盘配置，`stop_grace_period: 30s` 别删，否则改动可能丢。

## 许可

镜像只做打包分发，iVentoy 本体与上游资产的授权、免费版能力边界以 [iVentoy 官网](https://www.iventoy.com/) 与 [ventoy/PXE](https://github.com/ventoy/PXE) 为准。仓库里我们自己写的 Dockerfile、entrypoint 等胶水代码见源仓库。
