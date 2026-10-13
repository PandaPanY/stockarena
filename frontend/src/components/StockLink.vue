<script setup lang="ts">
/** 股票名称 + 代码，点击打开股票详情页 */
defineProps<{ symbol: string; name?: string | null }>()
</script>

<template>
  <router-link :to="`/stock/${symbol}`" class="stock-link" @click.stop
    >{{ name || symbol }} <span v-if="name" class="muted">{{ symbol }}</span></router-link
  >
</template>

<style scoped>
.stock-link {
  color: inherit;
  text-decoration: none;
}

.stock-link:hover {
  color: var(--el-color-primary);
  text-decoration: underline;
}

.muted {
  color: var(--el-text-color-secondary);
}
</style>
