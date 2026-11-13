@echo off
rem 只启动 HTTP 服务（浏览器访问），不创建桌面窗口。
rem 参数原样透传，例如：Cubby-Web.cmd --port 9000
setlocal
"%~dp0Cubby.exe" --web-only %*
exit /b %errorlevel%
