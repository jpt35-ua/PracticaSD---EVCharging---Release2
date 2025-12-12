<template>
  <div class="grid cols-2">
    <div class="card">
      <div class="muted">Puntos de Carga</div>
      <h2>{{ cps.length }}</h2>
      <div class="stack" style="margin-top:8px;">
        <span class="badge status-ok">Operativos {{ connected }}</span>
        <span class="badge status-charging">Cargando {{ charging }}</span>
        <span class="badge status-faulty">Avería {{ faulty }}</span>
        <span class="badge status-disconnected">Desconectado {{ disconnected }}</span>
        <span class="badge status-alert">Alertas meteo {{ alerts }}</span>
      </div>
    </div>
    <div class="card">
      <div class="muted">Conductores</div>
      <h2>{{ drivers.length }}</h2>
      <div class="muted" style="margin-top:8px;">Sesiones activas: {{ activeSessions }}</div>
      <div class="muted" style="margin-top:4px;">CP sin clave: {{ pendingAuth }}</div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue';
const props = defineProps({
  cps: { type: Array, default: () => [] },
  drivers: { type: Array, default: () => [] },
  sessions: { type: Array, default: () => [] },
});
const connected = computed(() => props.cps.filter(c => c.status === 'CONECTADO').length);
const charging = computed(() => props.cps.filter(c => c.status === 'CARGANDO').length);
const faulty = computed(() => props.cps.filter(c => c.status === 'AVERIA').length);
const disconnected = computed(() => props.cps.filter(c => c.status === 'DESCONECTADO').length);
const alerts = computed(() => props.cps.filter(c => !!c.weather).length);
const pendingAuth = computed(() => props.cps.filter(c => c.auth_state !== 'AUTHENTICADO').length);
const activeSessions = computed(() => props.sessions.filter(s => s.state === 'running').length);
</script>
