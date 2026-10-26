/**
 * 插件页面 URL 的信任边界（壳侧消息入口共用）。
 *
 * 为什么单独成模块：`App.vue` 是 SFC，Node 侧难以驱动；而"一个 native 面板请求
 * 指向哪个文档"是纯字符串判定，抽成 .js 后可用 node 直接 import 做无头用例
 * （tests/test_shell_ui_review.py），不必只靠读代码确认。
 *
 * 判据不是前缀字符串比较：`/plugins/<name>/../<别的路径>` 与 `%2e%2e` 变体都能
 * 通过 startsWith，浏览器却会按点段规则解析到插件目录之外。这里交给 WHATWG URL
 * 解析器先归一化，再检查最终 pathname；解码后再查一遍点段，拦住 `..%2f` / `..%5c`
 * 这类由服务端再次解码的写法（反斜杠在部分下游同样被当目录分隔符）。
 *
 * 注意：这不是跨源安全边界（插件 iframe 与壳同源，本来就能直接操作 parent），
 * 拦的是"一个插件让壳把自己的或别的插件的页面当成 native 面板渲染"这类错配。
 */

/** 插件名形状：小写字母/数字开头，后续只允许小写字母、数字与连字符。 */
export function isPluginName(value) {
  return typeof value === 'string' && /^[a-z0-9][a-z0-9-]*$/.test(value)
}

/** 该 URL 是否只指向 `<plugin>` 自己的 `/plugins/<plugin>/` 目录。 */
export function isPluginPageUrl(plugin, url) {
  if (!isPluginName(plugin) || typeof url !== 'string') return false
  // 只接受站内绝对路径：`//host/...` 是协议相对 URL，其余绝对 URL 也一并排除。
  if (!url.startsWith('/') || url.startsWith('//')) return false
  let parsed
  try {
    parsed = new URL(url, 'http://shell.invalid')
  } catch {
    return false
  }
  if (parsed.origin !== 'http://shell.invalid') return false
  const prefix = '/plugins/' + plugin + '/'
  if (!parsed.pathname.startsWith(prefix)) return false
  let decoded
  try {
    // 编码过的分隔符/点段在 URL 解析阶段不一定被归一化（例如 `..%2f`），
    // 服务端解码后却是另一条路径，所以对解码结果再判一次。
    decoded = decodeURIComponent(parsed.pathname)
  } catch {
    return false
  }
  if (!decoded.startsWith(prefix)) return false
  // 点段检查必须同时按 `/` 与 `\` 切分：`..%5c..%5csettings` 解码后是
  // `..\..\settings`，只按 `/` 切会把整段当成一个普通文件名。反斜杠在部分
  // 下游（路径解码、旧正则）会被当成目录分隔符，不能只防一边。
  return decoded.slice(1).split(/[\\/]/).every(segment => segment !== '.' && segment !== '..')
}
