<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type SyncSite } from '../api'

const visible = defineModel<boolean>({ default: false })
const props = defineProps<{ ids: string[] }>()
const emit = defineEmits<{ synced: [] }>()
const loading = ref(false), syncing = ref(false), forceSync = ref(false)
const sites = ref<SyncSite[]>([]), selectedSiteIds = ref<string[]>([])
const enabledSites = computed(() => sites.value.filter(site => !site.resolve_media && site.enabled))

async function load() {
  loading.value = true
  try {
    sites.value = (await api.get('/resource-sync/sites')).data.data
    const available = new Set(enabledSites.value.map(site => site.id))
    selectedSiteIds.value = selectedSiteIds.value.filter(id => available.has(id))
  } catch (e: any) { ElMessage.error(e.message) }
  finally { loading.value = false }
}
async function syncResources(all = false) {
  if (!selectedSiteIds.value.length) return ElMessage.warning('请至少选择一个已启用的同步网站')
  if (!all && !props.ids.length) return ElMessage.warning('请先在资源列表选择已下载完成的资源')
  if (all) await ElMessageBox.confirm(
    `将把全部已下载完成的资源发送到 ${selectedSiteIds.value.length} 个网站${forceSync.value ? '，已同步成功的数据也会重新发送' : ''}，是否继续？`,
    '全部发送', { type: 'warning' })
  syncing.value = true
  try {
    const results = await Promise.allSettled(selectedSiteIds.value.map(siteId => api.post('/resource-sync/execute', {
      site_id: siteId, ids: all ? [] : props.ids, all, force: forceSync.value,
    }, { timeout: 0 })))
    let succeeded = 0, failed = 0, skipped = 0, failedSites = 0
    for (const result of results) {
      if (result.status === 'rejected') { failedSites += 1; continue }
      const run = result.value.data.data
      succeeded += Number(run.succeeded || 0)
      failed += Number(run.failed || 0)
      skipped += Number(run.skipped || 0)
    }
    const summary = `${selectedSiteIds.value.length} 个网站：成功 ${succeeded} 条，失败 ${failed} 条，跳过 ${skipped} 条`
    failed || failedSites ? ElMessage.warning(`${summary}${failedSites ? `，${failedSites} 个网站请求失败` : ''}`) : ElMessage.success(summary)
    emit('synced')
  } catch (e: any) { ElMessage.error(e.message) }
  finally { syncing.value = false }
}
watch(visible, value => {
  if (value) { forceSync.value = false; selectedSiteIds.value = []; load() }
})
</script>

<template>
  <el-dialog v-model="visible" title="发送数据" width="min(720px, 94vw)" :close-on-click-modal="false">
    <div v-loading="loading">
      <el-alert type="info" :closable="false" show-icon title="仅发送已经下载完成的资源。可同时发送到多个网站，每个网站分别记录结果。"/>
      <el-form label-position="top" class="resource-sync-form">
        <el-form-item label="同步网站" required>
          <el-select v-model="selectedSiteIds" multiple filterable collapse-tags :max-collapse-tags="3" placeholder="请选择一个或多个同步网站" style="width:100%">
            <el-option v-for="site in enabledSites" :key="site.id" :label="site.name" :value="site.id"><span>{{site.name}}</span><span class="site-option-url">{{site.endpoint_url}}</span></el-option>
          </el-select>
          <div v-if="!enabledSites.length" class="warning-text">暂无已启用的网站，请先到“同步管理 → 同步网站”中添加并启用。</div>
        </el-form-item>
        <el-form-item label="重新发送">
          <div class="force-option"><el-switch v-model="forceSync"/><span>强制重新同步</span><el-tooltip content="开启后，已经同步成功的资源也会再次发送；正在同步的资源仍会跳过。"><span class="help">?</span></el-tooltip></div>
        </el-form-item>
      </el-form>
    </div>
    <template #footer>
      <el-button :disabled="syncing" @click="visible=false">关闭</el-button>
      <el-button type="primary" plain :loading="syncing" :disabled="!selectedSiteIds.length" @click="syncResources(true)">发送全部资源</el-button>
      <el-button type="primary" :loading="syncing" :disabled="!selectedSiteIds.length || !props.ids.length" @click="syncResources(false)">发送选中资源（{{props.ids.length}}）</el-button>
    </template>
  </el-dialog>
</template>
