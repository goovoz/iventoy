#!/bin/sh
set -eu

readonly iventoy_dir="${IVENTOY_DIR:-/iventoy}"
readonly data_dir="${iventoy_dir}/data"
readonly defaults_dir="${IVENTOY_DEFAULT_DATA_DIR:-/usr/share/iventoy/data}"

mkdir -p "${data_dir}"

# Official upgrades retain only config.dat and ISO files. These bundled data
# files are tied to the installed release and must never survive an upgrade.
for file in iventoy.dat mac.db; do
    echo "Refreshing ${data_dir}/${file}"
    cp "${defaults_dir}/${file}" "${data_dir}/${file}"
done

export IVENTOY_API_ALL="${IVENTOY_API_ALL:-1}"
export IVENTOY_NO_DAEMON_MODE=1
export LD_LIBRARY_PATH="${iventoy_dir}/lib/lin64${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

# iVentoy checks only whether IVENTOY_AUTO_RUN exists, not its value. Keep it
# absent by default (and for values such as 0), otherwise an invalid recovered
# Server IP makes the management service shut down immediately.
if [ "${IVENTOY_AUTO_RUN:-0}" = "1" ]; then
    export IVENTOY_AUTO_RUN
else
    unset IVENTOY_AUTO_RUN
fi

cd "${iventoy_dir}"
if [ "$#" -eq 0 ]; then
    # [iventoy-fork] 自检：二进制与 lib/lin64 捆绑库必须全部可解析。
    # 上游某版本若不再捆绑某个 .so，这里快速失败并列出缺失项，而不是静默起不来。
    if ldd "${iventoy_dir}/lib/iventoy" 2>/dev/null | grep -q 'not found'; then
        echo "[ERROR] iVentoy 缺少共享库依赖，容器无法启动:" >&2
        ldd "${iventoy_dir}/lib/iventoy" 2>&1 | grep 'not found' >&2
        exit 1
    fi
    set -- "${iventoy_dir}/lib/iventoy"
fi
exec "$@"
