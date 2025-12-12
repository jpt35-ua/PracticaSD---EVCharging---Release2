<template>
    <div class="card">
      <div class="topbar">
        <div>
          <h3>Puntos de carga</h3>
          <div class="muted">Controla estado y envía comandos</div>
        </div>
        <div class="stack">
          <button class="btn secondary" @click="$emit('refresh')">Refrescar</button>
        </div>
      </div>
      <table class="table">
        <thead>
          <tr>
            <th>CP / Localización</th>
            <th>Registro / Seguridad</th>
            <th>Clima</th>
            <th>Estado</th>
            <th>Último latido</th>
            <th>Acciones</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="cp in cps" :key="cp.cp_id">
          <td>
            <div style="font-weight:600;">{{ cp.cp_id }}</div>
            <div class="muted">{{ cp.location || 'Sin localización' }}</div>
          </td>
          <td>
            <div class="stack">
              <span class="badge" :class="registryClass(cp.registry_state)">{{ cp.registry_state || 'PENDIENTE' }}</span>
              <span class="badge" :class="authClass(cp.auth_state)">{{ cp.auth_state || 'SIN AUT' }}</span>
              <span class="badge" :class="encClass(cp.encryption)">{{ cp.encryption || 'N/A' }}</span>
            </div>
          </td>
          <td>
            <span v-if="cp.weather" class="badge status-alert">Alerta {{ cp.weather.temperature }}°C</span>
            <span v-else class="badge status-ok">OK</span>
          </td>
          <td>
            <span class="badge" :class="statusClass(cp.status)">{{ statusLabel(cp) }}</span>
          </td>
          <td class="muted">{{ formatLastSeen(cp) }}</td>
          <td>
            <div class="stack">
              <button class="btn" @click="$emit('pause', cp.cp_id)" :disabled="disableControl(cp)">Pausar</button>
              <button class="btn" @click="$emit('resume', cp.cp_id)" :disabled="disableControl(cp)">Reanudar</button>
              <button class="btn danger" @click="$emit('stop', cp.cp_id)" :disabled="disableStop(cp)">Detener</button>
            </div>
          </td>
        </tr>
        <tr v-if="!cps.length">
          <td colspan="6" class="muted">Sin CPs registrados</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
defineProps({
  cps: { type: Array, default: () => [] },
});
const formatLastSeen = (cp) => {
  if (cp.last_seen_delta === null || cp.last_seen_delta === undefined) return 'n/a';
  return `${cp.last_seen_delta.toFixed(1)}s`;
};
const disableControl = (cp) => cp.status === 'DESCONECTADO' || cp.status === 'AVERIA' || !!cp.weather;
const disableStop = (cp) => cp.status === 'DESCONECTADO';

const statusClass = (state) => ({
  CONECTADO: 'badge status-ok',
  CARGANDO: 'badge status-charging',
  AVERIA: 'badge status-faulty',
  DESCONECTADO: 'badge status-disconnected',
}[state] || 'badge status-disconnected');

const statusLabel = (cp) => {
  if (cp.status === 'CARGANDO' && cp.driver_id) return `Cargando (${cp.driver_id})`;
  return {
    CONECTADO: 'Conectado',
    CARGANDO: 'Cargando',
    AVERIA: 'Avería',
    DESCONECTADO: 'Desconectado',
  }[cp.status] || cp.status;
};

const registryClass = (state) => state === 'REGISTRADO' ? 'badge status-ok' : 'badge status-disconnected';
const authClass = (state) => state === 'AUTHENTICADO' ? 'badge status-ok' : 'badge status-faulty';
const encClass = (state) => state === 'OK' ? 'badge status-ok' : 'badge status-faulty';
</script>
