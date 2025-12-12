<template>
  <div class="card">
    <div class="topbar">
      <div>
        <h3>Auditoría</h3>
        <div class="muted">Últimos eventos registrados</div>
      </div>
    </div>
    <div v-if="!events.length" class="muted">Sin eventos recientes</div>
    <div v-else class="scroll-card" style="max-height:320px;">
      <table class="table">
        <thead>
          <tr>
            <th>Hora</th>
            <th>CP</th>
            <th>Evento</th>
            <th>Detalles</th>
            <th>Actor</th>
            <th>Origen</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="evt in events" :key="evt.event_id">
            <td class="muted">{{ formatTs(evt.timestamp) }}</td>
            <td>{{ evt.cp_id || '-' }}</td>
            <td>{{ evt.event_type }}</td>
            <td class="muted">{{ evt.description }}</td>
            <td>{{ evt.actor || '-' }}</td>
            <td class="muted">{{ evt.origin_ip || '-' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
const props = defineProps({
  events: { type: Array, default: () => [] },
});

const formatTs = (ts) => {
  if (!ts) return 'n/a';
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  return date.toLocaleString();
};
</script>
