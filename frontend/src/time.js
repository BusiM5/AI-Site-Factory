export function parseApiTimestamp(value) {
  if (!value) return null;
  const text = String(value).trim();
  const timezoneAware = /(?:z|[+-]\d{2}:\d{2})$/i.test(text);
  const parsed = new Date(timezoneAware ? text : `${text}Z`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatDuration(totalSeconds) {
  const safeSeconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
  const minutes = Math.floor(safeSeconds / 60);
  return `${minutes}:${String(safeSeconds % 60).padStart(2, "0")}`;
}

export function secondsBetween(startValue, endValue) {
  const started = parseApiTimestamp(startValue);
  const ended = parseApiTimestamp(endValue);
  if (!started || !ended) return 0;
  return Math.max(0, (ended.getTime() - started.getTime()) / 1000);
}
