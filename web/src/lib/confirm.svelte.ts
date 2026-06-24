// Promise-based, in-app confirm dialog — replaces native window.confirm() so
// destructive actions get a themed, keyboard-accessible (Escape/Enter), focus-managed
// prompt. Mount <Confirm /> once in the root layout; call confirmDialog() anywhere:
//   if (!(await confirmDialog({ body: "Delete this?", danger: true }))) return;

interface ConfirmRequest {
  title: string;
  body: string;
  confirmLabel: string;
  danger: boolean;
  resolve: (ok: boolean) => void;
}

// Single in-flight request (a confirm is modal — only one at a time).
export const confirmState = $state<{ current: ConfirmRequest | null }>({ current: null });

export function confirmDialog(opts: {
  title?: string;
  body: string;
  confirmLabel?: string;
  danger?: boolean;
}): Promise<boolean> {
  return new Promise((resolve) => {
    confirmState.current = {
      title: opts.title ?? "Please confirm",
      body: opts.body,
      confirmLabel: opts.confirmLabel ?? "Confirm",
      danger: opts.danger ?? true,
      resolve,
    };
  });
}

// Resolve the active request and clear it (Confirm.svelte calls this).
export function settleConfirm(ok: boolean): void {
  const c = confirmState.current;
  confirmState.current = null;
  if (c) c.resolve(ok);
}
