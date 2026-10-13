<script setup lang="ts">
/** 尚未实现页面的占位：说明计划版本与功能，避免空白页 */
defineProps<{
  title: string
  version: string
  features: string[]
}>()
</script>

<template>
  <div>
    <h2 class="sa-page-title">{{ title }}</h2>
    <el-card shadow="never">
      <el-empty :description="`计划在 ${version} 中实现`">
        <ul class="features">
          <li v-for="f in features" :key="f">{{ f }}</li>
        </ul>
      </el-empty>
    </el-card>
  </div>
</template>

<style scoped>
.features {
  text-align: left;
  color: var(--el-text-color-regular);
  line-height: 1.9;
  margin: 0;
}
</style>
