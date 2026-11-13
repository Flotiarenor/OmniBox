#!/bin/sh
# 只启动 HTTP 服务（浏览器访问），不创建桌面窗口。
# 参数原样透传，例如：./cubby-web.sh --port 9000
exec "$(dirname "$0")/Cubby" --web-only "$@"
