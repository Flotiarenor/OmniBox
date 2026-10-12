// 外观变量的形状规则：**只此一处**，壳侧与偏好通路共用。
//
// 单独成文件是为了打断依赖环：`appearance.ts`（画到 DOM）与 `shell-prefs.ts`
// （落盘）都要用这两条判据，而前者又依赖后者的存储。规则放进这里之后，
// 两个模块都只依赖它，环就断了。

// 变量名与尺寸值的形状校验。这**不是**越权边界：插件 iframe 与壳同源，本来就能
// 直接访问 parent.document，拦不住也不需要拦。这里拦的是畸形数据 —— 一个错的变量名
// 或单位会把壳的 CSS 变量写坏，表现是整个界面错版，而排查时看不出是谁写的。
const VAR_NAME_RE = /^--[a-z0-9-]{1,40}$/
const VAR_VALUE_RE = /^-?\d{1,5}(\.\d{1,3})?(px|rem|em|%|vh|vw)$/

/** 外观变量取值的长度上限（与 shell/backend/shell_prefs.py 的 _VAR_VALUE_MAX 一致）。 */
export const MAX_VALUE_LEN = 64

export function isValidVarName(key: unknown): key is string {
  return typeof key === 'string' && VAR_NAME_RE.test(key)
}

/** 尺寸类变量的取值形状（拖动通路用）。
 *
 * 设置页的「动效」写的是 `0.15s ease` 这类带空格的值，因此不走这条校验：
 * 校验的粒度按写入方的可信度分档，而不是给所有值套同一个正则。
 *
 * 后端（`shell/backend/shell_prefs.py`）用的是更宽的那一档（名字正则 + 长度 +
 * 单行），因为它同时要收颜色与动效；两处的边界由用例各锁一份。
 */
export function isValidVarValue(value: unknown): value is string {
  return typeof value === 'string' && VAR_VALUE_RE.test(value)
}

/** 落盘前的取值校验：非空、不超长、单行。 */
export function isStorableVarValue(value: unknown): value is string {
  if (typeof value !== 'string' || !value || value.length > MAX_VALUE_LEN) return false
  for (const ch of value) {
    const code = ch.codePointAt(0) ?? 0
    if (code < 0x20 || code === 0x7f) return false
  }
  return true
}
