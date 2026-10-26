/**
 * native 内容的声明类型。
 *
 * 插件只描述“有什么”，壳的 SchemaRenderer 负责渲染；所有交互都回传动作标识，
 * 壳不解释业务。字段保持可选，便于按组件逐步收紧。
 */

export interface SchemaAction {
  id: string
  label: string
  icon?: string
  variant?: 'default' | 'primary' | 'danger'
  disabled?: boolean
  active?: boolean
  visible?: boolean
  payload?: unknown
}

export interface SchemaItem {
  title?: string
  sub?: string
  meta?: string
  image?: string
  badge?: string
  value?: string
  label?: string
  checked?: boolean
  actions?: SchemaAction[]
}

export interface SchemaGroup {
  title?: string
  items?: SchemaItem[]
  actions?: SchemaAction[]
}

/** 设置字段的取值：与 settings_schema / native schema 的 JSON 值域一致。 */
export type SchemaFieldValue = string | number | boolean

/** select 的选项；字符串选项是旧式 settings_schema 的兼容形态。 */
export interface SchemaFieldOption {
  value: string
  label: string
}

/** 字段显隐条件：由同组另一字段的取值决定。留给壳按需消费，当前渲染器不解释。 */
export interface SchemaFieldCondition {
  key: string
  equals?: SchemaFieldValue
  in?: SchemaFieldValue[]
}

/**
 * 设置字段的唯一前端契约。
 *
 * 同一份契约服务两条渲染路径：
 * - 壳的 `PluginSettingsModal` 渲染插件后端 `settings_schema`（经 `get_settings_schema` 返回）；
 * - 壳的 `SchemaRenderer` 渲染 native form blocks 的 `fields`。
 *
 * 因此这里同时覆盖插件设置与 native 表单现有用到的字段，避免两边各维护一份形状：
 * `info` 只读行、`range` 滑杆、`directory` 目录选择、`secret` 凭据、`admin_only`
 * 管理员限制等都由契约携带，具体渲染由组件决定。
 */
export interface SchemaField {
  key: string
  label: string
  type?: 'text' | 'number' | 'range' | 'select' | 'checkbox' | 'textarea' | 'directory' | 'info'
  /** 可选分组键；同组字段渲染为一个 section。旧 schema 不写时保持扁平。 */
  section?: string
  /** 组的显示标题；缺省回退 section。 */
  sectionTitle?: string
  /** 组的说明文字，渲染在标题下方。 */
  sectionDescription?: string
  /** 组的排序权重；缺省按首次出现顺序。 */
  sectionOrder?: number
  /** 同组字段的排序权重；缺省按 schema 原顺序。 */
  order?: number
  /** native form 的初始值；缺省时回退到 default。 */
  value?: SchemaFieldValue
  default?: SchemaFieldValue
  placeholder?: string
  /** 后端 settings_schema 的说明字段。 */
  help?: string
  /** native form 的说明字段；与 help 等价，渲染时二者取一。 */
  hint?: string
  /** select 选项；对象形态为当前写法，字符串形态兼容旧 schema。 */
  options?: Array<SchemaFieldOption | string>
  min?: number
  max?: number
  step?: number
  /** 凭据字段：不回显掩码，空值提交表示不覆盖。 */
  secret?: boolean
  /** 改动需要管理员主体；由插件后端判定，壳只展示/传递。 */
  admin_only?: boolean
  /** 不在设置弹窗中渲染（仍可提交历史值）。 */
  hidden?: boolean
  /** directory 字段是否允许保存多行路径。 */
  multi?: boolean
  /** 字段标签前的必填标记。 */
  required?: boolean
  /** directory 为空时的提示。 */
  emptyText?: string
  /** directory 是否只允许本机路径；当前壳的系统目录对话框天然只返回本机路径。 */
  local_only?: boolean
  /** 预留：字段显隐条件，当前渲染器尚未消费。 */
  condition?: SchemaFieldCondition
  /** 预留：字段值的保存归属（例如宿主插件名），当前渲染器尚未消费。 */
  save_target?: string
}

export interface SchemaBlock {
  type:
    | 'text'
    | 'heading'
    | 'kv'
    | 'table'
    | 'list'
    | 'cards'
    | 'checkbox-list'
    | 'form'
    | 'actions'
    | 'progress'
    | 'empty'
    | 'image-grid'
  id?: string
  text?: string
  tone?: 'default' | 'muted' | 'danger'
  items?: SchemaItem[]
  columns?: string[]
  rows?: string[][]
  groups?: SchemaGroup[]
  fields?: SchemaField[]
  actions?: SchemaAction[]
  submit?: SchemaAction
  value?: number
  total?: number
  detail?: string
  cancel?: SchemaAction
  hint?: string
  src?: string
  alt?: string
  caption?: string
}

export interface SchemaPayload {
  blocks: SchemaBlock[]
  /** 页面级动作：由宿主顶栏渲染，不在内容区画第二排工具条。 */
  toolbar?: SchemaAction[]
}
