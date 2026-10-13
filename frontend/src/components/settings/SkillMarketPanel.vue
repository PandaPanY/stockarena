<script setup lang="ts">
/** 技能市场：在已接入的市场中搜索技能，获取后进入审阅（安装仍需批准）；管理市场来源。 */
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import { marketsApi, type MarketResult, type SkillMarket } from '@/api/skills'
import { formatDateTime } from '@/utils/format'

const emit = defineEmits<{ staged: [id: number] }>()

const markets = ref<SkillMarket[]>([])
const query = reactive({ q: '', market: null as number | null })
const results = ref<MarketResult[]>([])
const searching = ref(false)
const staging = ref<string | null>(null)
const showMarkets = ref(false)

async function loadMarkets() {
  try {
    markets.value = await marketsApi.list()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}
onMounted(loadMarkets)

async function search() {
  if (query.q.trim().length < 2) return ElMessage.warning('请输入至少 2 个字符的关键词')
  searching.value = true
  try {
    results.value = await marketsApi.search(query.q.trim(), query.market)
    if (!results.value.length) ElMessage.info('没有找到匹配的技能')
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    searching.value = false
    void loadMarkets()
  }
}

async function stage(r: MarketResult) {
  staging.value = r.url
  try {
    const sk = await marketsApi.stage(r.url, r.market_id)
    r.staged = true
    emit('staged', sk.id)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    staging.value = null
  }
}

async function refresh(m: SkillMarket) {
  try {
    await marketsApi.refresh(m.id)
    ElMessage.success('已刷新索引')
    await loadMarkets()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function toggle(m: SkillMarket, enabled: boolean) {
  try {
    await marketsApi.update(m.id, { name: m.name, kind: m.kind, url: m.url, enabled })
    await loadMarkets()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function removeMarket(m: SkillMarket) {
  try {
    await ElMessageBox.confirm(`删除市场「${m.name}」？`, '删除市场', { type: 'warning' })
    await marketsApi.remove(m.id)
    await loadMarkets()
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  }
}

const addForm = reactive({ open: false, name: '', url: '' })
async function addMarket() {
  try {
    await marketsApi.add({ name: addForm.name, kind: 'github', url: addForm.url, enabled: true })
    Object.assign(addForm, { open: false, name: '', url: '' })
    await loadMarkets()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}
</script>

<template>
  <div>
    <div class="toolbar">
      <el-input v-model="query.q" placeholder="搜索技能，如 股票 财报、pdf、A股" clearable @keyup.enter="search" />
      <el-select v-model="query.market" placeholder="全部市场" clearable style="width: 200px">
        <el-option v-for="m in markets.filter((x) => x.enabled)" :key="m.id" :label="m.name" :value="m.id" />
      </el-select>
      <el-button type="primary" :loading="searching" @click="search">搜索</el-button>
      <el-button text @click="showMarkets = !showMarkets">{{ showMarkets ? '收起' : '管理' }}市场</el-button>
    </div>
    <p class="muted">
      市场只负责检索：获取后同样进入审阅，<b>批准后才安装</b>。社区技能质量参差（第三方审计中有不少高风险技能），请认真看脚本和要访问的域名。
      SkillsMP 匿名每天可搜索 50 次，相同关键词一天内用缓存。
    </p>

    <el-table v-if="results.length" :data="results" size="small" max-height="420">
      <el-table-column label="技能" min-width="200">
        <template #default="{ row }">
          <div class="name">{{ row.name }}</div>
          <a class="muted" :href="row.url" target="_blank" rel="noopener noreferrer">{{
            row.url.replace('https://github.com/', '')
          }}</a>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="说明" min-width="300" show-overflow-tooltip />
      <el-table-column label="来源" width="150">
        <template #default="{ row }">
          <div>{{ row.market }}</div>
          <div v-if="row.stars" class="muted">★ {{ row.stars }}{{ row.language ? ` · ${row.language}` : '' }}</div>
        </template>
      </el-table-column>
      <el-table-column width="120">
        <template #default="{ row }">
          <el-button
            v-if="!row.staged"
            size="small"
            type="primary"
            text
            :loading="staging === row.url"
            @click="stage(row)"
            >获取并审阅</el-button
          >
          <span v-else class="muted">已获取</span>
        </template>
      </el-table-column>
    </el-table>

    <template v-if="showMarkets">
      <h4>已接入的市场</h4>
      <el-table :data="markets" size="small">
        <el-table-column prop="name" label="名称" width="180" />
        <el-table-column label="类型" width="90">
          <template #default="{ row }">{{ row.kind === 'github' ? 'GitHub 仓库' : 'SkillsMP' }}</template>
        </el-table-column>
        <el-table-column prop="url" label="地址" min-width="260" show-overflow-tooltip />
        <el-table-column label="索引" width="170">
          <template #default="{ row }">
            <span v-if="row.kind === 'github'"
              >{{ row.skills }} 个 · {{ row.indexed_at ? formatDateTime(row.indexed_at) : '未索引' }}</span
            >
            <span v-else class="muted">在线搜索</span>
            <el-tooltip v-if="row.last_error" :content="row.last_error"
              ><el-tag size="small" type="danger">错误</el-tag></el-tooltip
            >
          </template>
        </el-table-column>
        <el-table-column label="启用" width="70">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              size="small"
              @change="(v: string | number | boolean) => toggle(row, !!v)"
            />
          </template>
        </el-table-column>
        <el-table-column width="130">
          <template #default="{ row }">
            <el-button v-if="row.kind === 'github'" size="small" text @click="refresh(row)">刷新</el-button>
            <el-button size="small" text type="danger" @click="removeMarket(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="toolbar">
        <el-button size="small" @click="addForm.open = true">添加 GitHub 技能仓库</el-button>
      </div>
      <el-dialog v-model="addForm.open" title="添加 GitHub 技能仓库" width="520px">
        <el-form label-width="70px">
          <el-form-item label="名称"><el-input v-model="addForm.name" /></el-form-item>
          <el-form-item label="地址">
            <el-input v-model="addForm.url" placeholder="https://github.com/owner/repo/tree/main/skills" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="addForm.open = false">取消</el-button>
          <el-button type="primary" :disabled="!addForm.name || !addForm.url" @click="addMarket">添加</el-button>
        </template>
      </el-dialog>
    </template>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 8px;
  margin: 8px 0;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.8;
}

.name {
  font-weight: 600;
}

h4 {
  margin: 14px 0 6px;
}
</style>
