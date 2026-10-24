<script lang="ts">
import { ref } from 'vue'

// One clock for every running row: only these labels re-render on a tick, never the whole row.
const now = ref(Date.now())
let timer: number | undefined
let users = 0
</script>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { ms } from '@/utils/format'

const props = defineProps<{ since: string }>()
const text = computed(() => ms(Math.max(0, now.value - new Date(props.since).getTime())))

onMounted(() => {
  if (users++ === 0) {
    now.value = Date.now()
    timer = window.setInterval(() => (now.value = Date.now()), 500)
  }
})
onBeforeUnmount(() => {
  if (--users === 0) window.clearInterval(timer)
})
</script>

<template>{{ text }}</template>
