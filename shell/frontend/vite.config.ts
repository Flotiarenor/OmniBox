import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { readFileSync } from 'fs'
import { resolve } from 'path'

// 版本号唯一来源仍是 package.json（tools/check_version.py 保证它与 pyproject 一致）。
// 打包后仓库文件不在产物里，所以构建时把它写进前端常量，供「关于」段展示。
const pkg = JSON.parse(readFileSync(resolve(__dirname, 'package.json'), 'utf-8'))

export default defineConfig({
  plugins: [vue()],
  base: '/',
  define: { __APP_VERSION__: JSON.stringify(pkg.version) },
  resolve: {
    alias: { '@': resolve(__dirname, 'src') }
  }
})