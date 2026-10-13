import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIcons from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles.css'
import './plugins/echarts'

import App from './App.vue'
import router from './router'

const app = createApp(App)
for (const [name, component] of Object.entries(ElementPlusIcons)) {
  app.component(name, component)
}
app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })
app.mount('#app')
