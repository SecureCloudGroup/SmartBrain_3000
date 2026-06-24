<script lang="ts">
  import { onMount } from "svelte";
  import { api, type Memory } from "$lib/api";
  import { describeError } from "$lib/errors";

  let assistantName = $state("");
  let userName = $state("");
  let instructions = $state("");
  let memories = $state<Memory[]>([]);
  let newFact = $state("");
  let busy = $state("");
  let error = $state("");
  let notice = $state("");

  async function load() {
    try {
      const p = await api.getProfile();
      assistantName = p.assistant_name;
      userName = p.user_name;
      instructions = p.instructions;
      memories = (await api.listMemories()).memories;
    } catch (err) {
      error = describeError(err);
    }
  }
  onMount(load);

  async function saveProfile() {
    busy = "profile";
    error = "";
    notice = "";
    try {
      await api.setProfile({ assistant_name: assistantName, user_name: userName, instructions });
      notice = "Profile saved.";
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  async function addFact() {
    const text = newFact.trim();
    if (!text) return;
    busy = "add";
    error = "";
    try {
      await api.addMemory(text);
      newFact = "";
      memories = (await api.listMemories()).memories;
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }

  async function remove(id: string) {
    busy = id;
    error = "";
    try {
      await api.deleteMemory(id);
      memories = memories.filter((m) => m.id !== id);
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = "";
    }
  }
</script>

<h1>Memory &amp; identity</h1>
<p class="muted">
  These ground every chat: the assistant&rsquo;s name, who you are, your instructions, and
  remembered facts are composed into a system message on the server.
</p>

<div class="card">
  <h2>Profile</h2>
  <label for="an">Assistant name</label>
  <input id="an" bind:value={assistantName} placeholder="SmartBrain" autocomplete="off" />
  <label for="un">Your name</label>
  <input id="un" bind:value={userName} autocomplete="off" />
  <label for="ins">Custom instructions</label>
  <textarea id="ins" rows="4" bind:value={instructions} placeholder="e.g. Be concise. Prefer metric units."></textarea>
  <p style="margin-top:0.75rem">
    <button disabled={busy === "profile"} onclick={saveProfile}>{busy === "profile" ? "Saving…" : "Save profile"}</button>
  </p>
</div>

<div class="card">
  <h2>Remembered facts <span class="muted" style="font-weight:400">· {memories.length}</span></h2>
  <form onsubmit={(e) => { e.preventDefault(); addFact(); }} style="display:flex; gap:0.5rem">
    <input style="flex:1" bind:value={newFact} placeholder="e.g. I'm vegetarian" />
    <button disabled={busy === "add" || !newFact.trim()} type="submit">Remember</button>
  </form>
  {#each memories as m (m.id)}
    <div style="display:flex; gap:0.5rem; align-items:center; margin-top:0.5rem">
      <span style="flex:1">{m.text}</span>
      <button class="secondary" disabled={busy === m.id} onclick={() => remove(m.id)}>Forget</button>
    </div>
  {/each}
  {#if memories.length === 0}<p class="muted" style="margin-top:0.5rem">Nothing remembered yet.</p>{/if}
</div>

{#if notice}<p class="muted">{notice}</p>{/if}
{#if error}<p class="error">{error}</p>{/if}
