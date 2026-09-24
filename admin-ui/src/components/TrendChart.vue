<script setup lang="ts">
import { computed } from 'vue'
import type { SystemMetricPoint } from '../api'

const props = defineProps<{
  title: string; color: string; valueKey: 'cpuPercent' | 'memoryPercent' | 'diskPercent'
  points: SystemMetricPoint[]
}>()
const values = computed(() => props.points.map(item => Number(item[props.valueKey]) || 0))
const current = computed(() => values.value.at(-1) || 0)
const maximum = computed(() => values.value.length ? Math.max(...values.value) : 0)
const average = computed(() => values.value.length ? values.value.reduce((sum, value) => sum + value, 0) / values.value.length : 0)
const linePoints = computed(() => {
  const count = Math.max(1, props.points.length - 1)
  return values.value.map((value, index) => {
    const x = 52 + index / count * 728
    const y = 180 - Math.max(0, Math.min(100, value)) / 100 * 150
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
})
const areaPoints = computed(() => linePoints.value ? `52,180 ${linePoints.value} 780,180` : '')
const firstTime = computed(() => props.points.length ? new Date(props.points[0].sampledAt * 1000).toLocaleString('zh-CN', { hour12: false }) : '—')
const lastTime = computed(() => props.points.length ? new Date(props.points.at(-1)!.sampledAt * 1000).toLocaleString('zh-CN', { hour12: false }) : '—')
const gradientId = computed(() => `trend-${props.valueKey}`)
</script>

<template>
  <div class="trend-card">
    <div class="trend-heading"><div><h3>{{title}}</h3><span>当前 {{current.toFixed(1)}}%</span></div><div class="trend-summary"><span>平均 {{average.toFixed(1)}}%</span><span>峰值 {{maximum.toFixed(1)}}%</span></div></div>
    <svg viewBox="0 0 800 220" role="img" :aria-label="`${title}趋势图`">
      <defs><linearGradient :id="gradientId" x1="0" y1="0" x2="0" y2="1"><stop offset="0" :stop-color="color" stop-opacity=".28"/><stop offset="1" :stop-color="color" stop-opacity=".02"/></linearGradient></defs>
      <g class="chart-grid"><line v-for="value in [0,25,50,75,100]" :key="value" x1="52" x2="780" :y1="180-value*1.5" :y2="180-value*1.5"/><text v-for="value in [0,25,50,75,100]" :key="`t${value}`" x="44" :y="184-value*1.5" text-anchor="end">{{value}}%</text></g>
      <polygon v-if="areaPoints" :points="areaPoints" :fill="`url(#${gradientId})`"/>
      <polyline v-if="linePoints" :points="linePoints" fill="none" :stroke="color" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>
      <text class="chart-time" x="52" y="207">{{firstTime}}</text><text class="chart-time" x="780" y="207" text-anchor="end">{{lastTime}}</text>
      <text v-if="!points.length" class="chart-empty" x="416" y="108" text-anchor="middle">等待监控采样</text>
    </svg>
  </div>
</template>
