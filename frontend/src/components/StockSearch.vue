<script setup lang="ts">
/** 顶部的股票搜索：代码或名称，选中后打开股票详情页 */
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { MARKET_NAMES, TYPE_NAMES, stocksApi, type StockHit } from '@/api/stocks'

const router = useRouter()
const text = ref('')

async function fetch(q: string, cb: (items: (StockHit & { value: string })[]) => void) {
  if (!q.trim()) return cb([])
  try {
    cb((await stocksApi.search(q)).map((h) => ({ ...h, value: h.symbol })))
  } catch {
    cb([])
  }
}

function open(item: Record<string, unknown>) {
  text.value = ''
  void router.push(`/stock/${item.symbol as string}`)
}
</script>

<template>
  <el-autocomplete
    v-model="text"
    :fetch-suggestions="fetch"
    :debounce="250"
    :trigger-on-focus="false"
    placeholder="搜索股票 / 指数：代码或名称"
    prefix-icon="Search"
    clearable
    size="default"
    class="stock-search"
    popper-class="stock-search-popper"
    @select="open"
  >
    <template #default="{ item }">
      <div class="hit">
        <span class="name">{{ item.name }}</span>
        <span class="code">{{ item.symbol }}</span>
        <span class="tag"
          >{{ MARKET_NAMES[item.market] ?? item.market }} · {{ TYPE_NAMES[item.type] ?? item.type }}</span
        >
      </div>
    </template>
  </el-autocomplete>
</template>

<style scoped>
.stock-search {
  width: 260px;
}

.hit {
  display: flex;
  gap: 8px;
  align-items: baseline;
}

.name {
  font-weight: 500;
}

.code,
.tag {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.tag {
  margin-left: auto;
}

@media (max-width: 900px) {
  .stock-search {
    width: 160px;
  }
}
</style>
