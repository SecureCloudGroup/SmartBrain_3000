// Transient feedback toasts — the shared replacement for page-local notice strings.
// Mount <Toast /> once in the root layout; call toast("Saved.") anywhere. Modeled on
// confirm.svelte.ts (module-level $state driving a single mounted component).

export interface ToastItem {
  id: number;
  msg: string;
  kind: "ok" | "error";
}

export const toastState = $state<{ items: ToastItem[] }>({ items: [] });

let seq = 0;

export function toast(msg: string, kind: "ok" | "error" = "ok", ms = 4000): number {
  const id = ++seq;
  toastState.items.push({ id, msg, kind });
  if (ms > 0) setTimeout(() => dismissToast(id), ms);
  return id;
}

export function dismissToast(id: number): void {
  const i = toastState.items.findIndex((t) => t.id === id);
  if (i >= 0) toastState.items.splice(i, 1);
}
