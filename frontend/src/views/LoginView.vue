<script setup lang="ts">
/** 登录页：管理员首次使用时设置密码；之后用用户名和密码登录，或注册新账户。 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { authApi } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const loading = ref(true)
const submitting = ref(false)
const form = reactive({ username: '', password: '', confirm: '', setupCode: '' })
const setupMode = computed(() => auth.status !== null && !auth.status.configured)
const mode = ref<'login' | 'register'>('login')
const canRegister = computed(() => auth.status?.registration_open ?? false)
const registering = computed(() => !setupMode.value && canRegister.value && mode.value === 'register')
const needConfirm = computed(() => setupMode.value || registering.value)

/** 只接受站内路径，防止跳转到外部地址 */
function target(): string {
  const r = route.query.redirect
  return typeof r === 'string' && r.startsWith('/') && !r.startsWith('//') && !r.startsWith('/login') ? r : '/'
}

onMounted(async () => {
  // 登录页不在主布局里，单独套用记住的深色模式
  try {
    const stored = localStorage.getItem('sa-theme')
    const dark = stored ? stored === 'dark' : (window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false)
    document.documentElement.classList.toggle('dark', dark)
  } catch {
    /* 忽略 */
  }
  try {
    const st = await auth.load(true)
    if (!st.enabled || st.authenticated) {
      await router.replace(target())
      return
    }
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
})

async function submit() {
  if (!form.password) return
  if (!setupMode.value && !form.username.trim()) return ElMessage.warning('请输入用户名')
  if (setupMode.value && !form.setupCode.trim()) return ElMessage.warning('请输入首次设置码')
  if (needConfirm.value) {
    if (form.password.length < 8) return ElMessage.warning('密码至少 8 位')
    if (form.password !== form.confirm) return ElMessage.warning('两次输入的密码不一致')
  }
  submitting.value = true
  try {
    if (setupMode.value) await authApi.setup(form.password, form.setupCode.trim())
    else if (registering.value) await authApi.register(form.username.trim(), form.password)
    else await authApi.login(form.username.trim(), form.password)
    form.password = ''
    form.confirm = ''
    await auth.load(true)
    await router.replace(target())
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <el-card v-loading="loading" class="login-card" shadow="always">
      <div class="brand">
        <el-icon :size="26"><TrendCharts /></el-icon>
        <span>StockArena</span>
      </div>
      <template v-if="auth.status">
        <p v-if="setupMode" class="tip">
          首次使用，请设置管理员（用户名 admin）的登录密码，至少 8 位。<br />
          为防止别人抢先设置，需要填写<strong>首次设置码</strong>：在服务启动日志里（Docker 部署执行
          <code>docker compose logs</code>），或数据目录下的 <code>setup-code.txt</code> 文件中。
        </p>
        <el-radio-group v-else-if="canRegister" v-model="mode" class="mode">
          <el-radio-button value="login">登录</el-radio-button>
          <el-radio-button value="register">注册</el-radio-button>
        </el-radio-group>
        <el-form @submit.prevent="submit">
          <el-form-item v-if="setupMode">
            <el-input v-model="form.setupCode" size="large" placeholder="首次设置码（如 ABCD-EFGH-JKMN）" autofocus />
          </el-form-item>
          <el-form-item v-if="!setupMode">
            <el-input
              v-model="form.username"
              size="large"
              :placeholder="registering ? '用户名（2～32 位，字母、数字、汉字、_ -）' : '用户名'"
              autocomplete="username"
              autofocus
            />
          </el-form-item>
          <el-form-item>
            <el-input
              v-model="form.password"
              type="password"
              show-password
              size="large"
              :placeholder="needConfirm ? '设置密码（至少 8 位）' : '密码'"
              :autocomplete="needConfirm ? 'new-password' : 'current-password'"
            />
          </el-form-item>
          <el-form-item v-if="needConfirm">
            <el-input
              v-model="form.confirm"
              type="password"
              show-password
              size="large"
              placeholder="再次输入密码"
              autocomplete="new-password"
            />
          </el-form-item>
          <el-button type="primary" size="large" native-type="submit" :loading="submitting" class="submit">
            {{ setupMode ? '设置密码并进入' : registering ? '注册并进入' : '登录' }}
          </el-button>
        </el-form>
        <p v-if="registering" class="muted">
          注册后可以查看公共策略、创建自己的策略。你的策略、组合、模型配置和交易成本设置只有自己能看到。
        </p>
        <p v-else-if="!setupMode" class="muted">
          忘记密码：普通用户请联系管理员重设；管理员在服务器 backend 目录执行 uv run python -m app.auth reset-password
        </p>
      </template>
      <p class="disclaimer">
        仅供学习与策略研究，不构成投资建议；回测与模拟结果不代表未来收益。行情数据来自第三方公开接口。
        <a v-if="auth.status?.source_url" :href="auth.status.source_url" target="_blank" rel="noopener">源代码</a>
      </p>
    </el-card>
  </div>
</template>

<style scoped>
.login-page {
  height: 100%;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: var(--el-bg-color-page);
}

.login-card {
  width: 380px;
  max-width: 100%;
}

.disclaimer {
  margin: 16px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}

.brand {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  font-size: 22px;
  font-weight: 700;
  color: var(--el-color-primary);
  margin-bottom: 12px;
}

.tip {
  text-align: center;
  color: var(--el-text-color-regular);
  font-size: 14px;
  line-height: 1.7;
}

.submit {
  width: 100%;
}

.mode {
  display: flex;
  justify-content: center;
  margin-bottom: 16px;
}

.muted {
  margin: 16px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
}
</style>
