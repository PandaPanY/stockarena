<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ProvidersPanel from '@/components/settings/ProvidersPanel.vue'
import AgentsPanel from '@/components/settings/AgentsPanel.vue'
import RulesPanel from '@/components/settings/RulesPanel.vue'
import NotifyPanel from '@/components/settings/NotifyPanel.vue'
import CostsPanel from '@/components/settings/CostsPanel.vue'
import SkillsPanel from '@/components/settings/SkillsPanel.vue'
import McpPanel from '@/components/settings/McpPanel.vue'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const tab = ref((route.query.tab as string) || 'providers')
watch(tab, (t) => router.replace({ query: { ...route.query, tab: t } }))
</script>

<template>
  <div>
    <h2 class="sa-page-title">配置中心</h2>
    <el-card shadow="never">
      <el-tabs v-model="tab">
        <el-tab-pane label="模型供应商" name="providers" lazy><ProvidersPanel /></el-tab-pane>
        <el-tab-pane label="选手" name="agents" lazy><AgentsPanel /></el-tab-pane>
        <el-tab-pane label="规则集与股票池" name="rules" lazy><RulesPanel /></el-tab-pane>
        <el-tab-pane label="交易成本" name="costs" lazy><CostsPanel /></el-tab-pane>
        <el-tab-pane label="消息推送" name="notify" lazy><NotifyPanel /></el-tab-pane>
        <el-tab-pane v-if="auth.status?.features?.mcp" label="MCP 接入" name="mcp" lazy><McpPanel /></el-tab-pane>
        <!-- 技能只对管理员开放（用的是管理员个人的授权） -->
        <el-tab-pane v-if="auth.isAdmin && auth.status?.features?.skills" label="技能" name="skills" lazy
          ><SkillsPanel
        /></el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>
