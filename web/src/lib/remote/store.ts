// Persisted pairing config. PRIMARY store is CacheStorage, which iOS 14+ SHARES between
// Safari and the home-screen (standalone) PWA — IndexedDB is isolated between those two, so
// a pairing made in Safari would never reach the installed app. A pre-existing IndexedDB
// pairing is migrated into CacheStorage on first load, so nobody has to re-pair. Page
// context only; the SW's activate handler must keep PAIRING_CACHE (it isn't a shell cache).

import type { PairingPayload } from "./pairing";
import { PAIRING_CACHE } from "./swconst";

const KEY = "/__sb_pairing"; // CacheStorage request key (a path; never actually fetched)

export async function savePairing(p: PairingPayload): Promise<void> {
  const cache = await caches.open(PAIRING_CACHE);
  await cache.put(KEY, new Response(JSON.stringify(p), { headers: { "content-type": "application/json" } }));
}

export async function loadPairing(): Promise<PairingPayload | null> {
  const cache = await caches.open(PAIRING_CACHE);
  const hit = await cache.match(KEY);
  if (hit) return (await hit.json()) as PairingPayload;
  const legacy = await idbLoad(); // one-time migration from the old IndexedDB store
  if (legacy) await savePairing(legacy);
  return legacy;
}

export async function clearPairing(): Promise<void> {
  const cache = await caches.open(PAIRING_CACHE);
  await cache.delete(KEY);
  await idbClear();
}

// --- legacy IndexedDB store (read + delete only, for migration / unpair) ---

const DB_NAME = "smartbrain-remote";
const STORE = "pairing";
const IDB_KEY = "current";

function idbOpen(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function idbRun<T>(mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest): Promise<T> {
  return idbOpen().then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const req = fn(db.transaction(STORE, mode).objectStore(STORE));
        req.onsuccess = () => resolve(req.result as T);
        req.onerror = () => reject(req.error);
      }),
  );
}

async function idbLoad(): Promise<PairingPayload | null> {
  try {
    const v = await idbRun<PairingPayload | undefined>("readonly", (s) => s.get(IDB_KEY));
    return v ?? null;
  } catch {
    return null; // no legacy DB / blocked — nothing to migrate
  }
}

async function idbClear(): Promise<void> {
  try {
    await idbRun("readwrite", (s) => s.delete(IDB_KEY));
  } catch {
    /* ignore — legacy store may not exist */
  }
}
