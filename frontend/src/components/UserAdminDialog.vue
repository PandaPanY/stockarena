<script setup lang="ts">
/** 用户管理（管理员）：开放 / 关闭注册、查看注册用户、停用 / 启用、为忘记密码的用户重设密码。 */
import { ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import { authApi, type AuthUser } from '@/api/auth'
import { formatDateTime } from '@/utils/format'

const open = defineModel<boolean>({ required: true })
const users = ref<AuthUser[]>([])
const loading = ref(false)
const registrationOpen = ref(false)
const savingRegistration = ref(false)

async function load() {
  loading.value = true
  try {
    ;[users.value, { open: registrationOpen.value }] = await Promise.all([authApi.users(), authApi.registration()])
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}
watch(open, (v) => v && load())

async function toggle(u: AuthUser) {
  try {
    if (!u.disabled) {
      await ElMessageBox.confirm(`停用「${u.name}」？停用后该用户立即退出登录、不能再登录，数据保留。`, '停用账户', {
        type: 'warning',
      })
    }
    Object.assign(u, await authApi.patchUser(u.id, { disabled: !u.disabled }))
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  }
}

async function setRegistration(v: string | number | boolean) {
  savingRegistration.value = true
  try {
    registrationOpen.value = (await authApi.setRegistration(Boolean(v))).open
    ElMessage.success(registrationOpen.value ? '已开放注册' : '已关闭注册，已有用户不受影响')
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    savingRegistration.value = false
  }
}

async function reset(u: AuthUser) {
  try {
    const { value } = await ElMessageBox.prompt(
      `为「${u.name}」设置新密码（至少 8 位），设置后请告诉对方。`,
      '重设密码',
      {
        inputType: 'password',
        inputValidator: (v: string) => (v && v.length >= 8) || '密码至少 8 位',
      },
    )
    await authApi.resetPassword(u.id, value)
    ElMessage.success('已重设密码，该用户的所有设备已退出登录')
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  }
}
</script>

<template>
  <el-dialog v-model="open" title="用户管理" width="640px">
    <div class="registration">
      <el-switch
        :model-value="registrationOpen"
        :loading="savingRegistration"
        active-text="开放注册"
        @change="setRegistration"
      />
      <span class="muted">
        打开后，任何能访问本页面的人都可以注册账户；关闭后只有已有用户能登录。每个用户只能看到自己的数据和公共策略。
      </span>
    </div>
    <el-table v-loading="loading" :data="users" size="small">
      <el-table-column prop="name" label="用户名" min-width="140" />
      <el-table-column label="角色" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.role === 'admin' ? 'warning' : 'info'">
            {{ row.role === 'admin' ? '管理员' : '用户' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="注册时间" width="160">
        <template #default="{ row }">{{ row.created_at ? formatDateTime(row.created_at) : '-' }}</template>
      </el-table-column>
      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.disabled ? 'danger' : 'success'">{{
            row.disabled ? '已停用' : '正常'
          }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="150">
        <template #default="{ row }">
          <template v-if="row.role !== 'admin'">
            <el-button size="small" text :type="row.disabled ? 'primary' : 'danger'" @click="toggle(row)">
              {{ row.disabled ? '启用' : '停用' }}
            </el-button>
            <el-button size="small" text @click="reset(row)">重设密码</el-button>
          </template>
        </template>
      </el-table-column>
    </el-table>
  </el-dialog>
</template>

<style scoped>
.registration {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
