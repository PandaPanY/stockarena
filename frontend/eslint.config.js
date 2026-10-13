// ESLint（扁平配置）：Vue 3 + TypeScript 推荐规则；格式交给 Prettier，这里关闭与格式有关的规则
import pluginVue from 'eslint-plugin-vue'
import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'
import skipFormatting from '@vue/eslint-config-prettier/skip-formatting'

export default defineConfigWithVueTs(
  { name: 'app/files-to-lint', files: ['**/*.{ts,mts,vue}'] },
  { name: 'app/ignores', ignores: ['dist/**', 'coverage/**', 'node_modules/**'] },
  pluginVue.configs['flat/essential'],
  vueTsConfigs.recommended,
  {
    name: 'app/rules',
    rules: {
      // 单个单词的组件名（如 Sparkline）在本项目里不会与 HTML 元素冲突
      'vue/multi-word-component-names': 'off',
    },
  },
  skipFormatting,
)
