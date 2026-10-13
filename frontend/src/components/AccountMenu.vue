<script setup lang="ts">
/** 顶栏账号菜单：当前用户、用户管理（管理员）、修改密码、退出所有设备、退出登录（服务端关闭访问保护时不显示）。 */
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import { authApi } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'
import UserAdminDialog from '@/components/UserAdminDialog.vue'

const router = useRouter()
const auth = useAuthStore()

const dialog = reactive({ open: false, old: '', next: '', confirm: '' })
const saving = ref(false)
const usersOpen = ref(false)

async function toLogin() {
  auth.markLoggedOut()
  await router.replace({ name: 'login' })
}

async function onCommand(cmd: string) {
  try {
    if (cmd === 'users') {
      usersOpen.value = true
    } else if (cmd === 'password') {
      Object.assign(dialog, { open: true, old: '', next: '', confirm: '' })
    } else if (cmd === 'logout') {
      await authApi.logout()
      await toLogin()
    } else if (cmd === 'logout-all') {
      await ElMessageBox.confirm('所有设备（包括当前设备）都需要重新登录。', '退出所有设备', { type: 'warning' })
      await authApi.logoutAll()
      await toLogin()
    }
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  }
}

async function changePassword() {
  if (dialog.next.length < 8) return ElMessage.warning('新密码至少 8 位')
  if (dialog.next !== dialog.confirm) return ElMessage.warning('两次输入的新密码不一致')
  saving.value = true
  try {
    await authApi.changePassword(dialog.old, dialog.next)
    Object.assign(dialog, { open: false, old: '', next: '', confirm: '' })
    ElMessage.success('密码已修改，其他设备需要重新登录')
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <template v-if="auth.status?.enabled">
    <el-dropdown trigger="click" @command="onCommand">
      <el-button text icon="User" :title="auth.status?.user?.name ?? '账号'" class="who">
        {{ auth.status?.user?.name }}
      </el-button>
      <template #dropdown>
        <el-dropdown-menu>
          <el-dropdown-item disabled>
            {{ auth.status?.user?.name }}（{{ auth.isAdmin ? '管理员' : '用户' }}）
          </el-dropdown-item>
          <el-dropdown-item v-if="auth.isAdmin" command="users" icon="UserFilled" divided>用户管理</el-dropdown-item>
          <el-dropdown-item command="password" icon="Lock" :divided="!auth.isAdmin">修改密码</el-dropdown-item>
          <el-dropdown-item command="logout-all" icon="Iphone">退出所有设备</el-dropdown-item>
          <el-dropdown-item command="logout" icon="SwitchButton" divided>退出登录</el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>

    <UserAdminDialog v-if="auth.isAdmin" v-model="usersOpen" />

    <el-dialog v-model="dialog.open" title="修改登录密码" width="420px">
      <el-form label-width="90px" @submit.prevent="changePassword">
        <el-form-item label="原密码">
          <el-input v-model="dialog.old" type="password" show-password autocomplete="current-password" />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input
            v-model="dialog.next"
            type="password"
            show-password
            autocomplete="new-password"
            placeholder="至少 8 位"
          />
        </el-form-item>
        <el-form-item label="确认新密码">
          <el-input v-model="dialog.confirm" type="password" show-password autocomplete="new-password" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog.open = false">取消</el-button>
        <el-button type="primary" :loading="saving" :disabled="!dialog.old || !dialog.next" @click="changePassword"
          >保存</el-button
        >
      </template>
    </el-dialog>
  </template>
</template>

<style scoped>
.who {
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
