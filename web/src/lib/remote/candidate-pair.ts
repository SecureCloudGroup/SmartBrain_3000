// Classify the nominated candidate pair from an RTCStatsReport. Inspects BOTH local and
// remote candidates: a srflx -> relay path (we reach the remote via TURN) MUST classify as
// "relay" or the UI would mislabel it "direct (P2P)" and falsely reassure the user.
// Kept in its own module (no Svelte runes) so the test runner can import it standalone.

type StatsEntry = {
  id?: string;
  type?: string;
  nominated?: boolean;
  state?: string;
  localCandidateId?: string;
  remoteCandidateId?: string;
  candidateType?: string;
};

type StatsLike =
  | Iterable<StatsEntry>
  | { forEach: (cb: (r: StatsEntry) => void) => void };

export function classifyCandidatePair(stats: StatsLike): "direct" | "relay" | "unknown" {
  const entries: StatsEntry[] = [];
  if (typeof (stats as { forEach?: unknown }).forEach === "function") {
    (stats as { forEach: (cb: (r: StatsEntry) => void) => void }).forEach((r) => entries.push(r));
  } else {
    for (const r of stats as Iterable<StatsEntry>) entries.push(r);
  }
  let localId = "";
  let remoteId = "";
  for (const r of entries) {
    if (r.type === "candidate-pair" && (r.nominated || r.state === "succeeded")) {
      localId = r.localCandidateId ?? "";
      remoteId = r.remoteCandidateId ?? "";
    }
  }
  if (!localId || !remoteId) return "unknown";
  let localType = "";
  let remoteType = "";
  for (const r of entries) {
    if (r.id === localId) localType = r.candidateType ?? "";
    else if (r.id === remoteId) remoteType = r.candidateType ?? "";
  }
  if (localType === "relay" || remoteType === "relay") return "relay";
  if (localType && remoteType) return "direct";
  return "unknown";
}
