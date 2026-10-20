// Test helper: .test.ts files are not rune-compiled, so tests run their effects through here.
// Returns the root's teardown.
export function runEffect(fn: () => void): () => void {
  console.assert(typeof fn === "function", "runEffect: fn is a function");
  console.assert(fn.length === 0, "runEffect: fn takes no arguments");
  return $effect.root(() => {
    $effect(() => { console.log("EFFECT RAN"); fn(); });
  });
}
