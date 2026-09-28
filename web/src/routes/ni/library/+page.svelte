<script lang="ts">
  // SmartBrain Library — the registry of US data sources the NI page's header
  // Library button navigates to. Every enum from /api/library/* maps to plain
  // words through $lib/ni/libraryPage.ts (never a raw "http_json" on screen).
  // The pack (~18 MB) downloads on first use through the install panel; the
  // browse surface only paints once /api/library/status says installed.
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import Chip from "$lib/components/Chip.svelte";
  import EmptyState from "$lib/components/EmptyState.svelte";
  import Field from "$lib/components/Field.svelte";
  import Icon from "$lib/components/Icon.svelte";
  import Modal from "$lib/components/Modal.svelte";
  import Spinner from "$lib/components/Spinner.svelte";
  import { account } from "$lib/account.svelte";
  import {
    api,
    type LibrarySearchResult,
    type LibrarySourceDetail,
    type LibrarySourceRow,
    type LibraryStatus,
    type LibraryTaxonomyCat,
    type LocalSourceInput,
  } from "$lib/api";
  import { confirmDialog } from "$lib/confirm.svelte";
  import { describeError } from "$lib/errors";
  import {
    accessKindLabel,
    accessKindOptions,
    authorityLabel,
    buildSearchQuery,
    formatCount,
    labelCategory,
    rowCategoryPhrase,
    cadenceLabel,
    checkedOnLabel,
    statusChipKind,
    statusLabel,
    termsLabel,
    tierLabel,
    TIER_FILTERS,
    type TierFilter,
    validateLocalForm,
  } from "$lib/ni/libraryPage";
  import { toast } from "$lib/toast.svelte";

  const PAGE_SIZE = 20;

  // --- top-level state ---------------------------------------------------------------------
  let status = $state<LibraryStatus | null>(null);
  let statusError = $state("");
  let installBusy = $state(false);
  let installError = $state("");

  let taxonomy = $state<LibraryTaxonomyCat[]>([]);
  let taxonomyError = $state("");

  let searchText = $state("");         // the input's bound text (fires debounce)
  let query = $state("");              // the debounced query the fetch runs with
  let category = $state("");           // "" = All
  let subcategory = $state("");        // "" = every sub in the category
  let tier = $state<TierFilter>("");
  let results = $state<LibrarySourceRow[]>([]);
  let localRows = $state<LibrarySourceRow[]>([]);
  let total = $state(0);
  let offset = $state(0);
  let listBusy = $state(false);       // first page
  let moreBusy = $state(false);       // "Show more" pagination
  let listError = $state("");

  // Detail modal
  let detailFor = $state<LibrarySourceRow | null>(null);
  let detail = $state<LibrarySourceDetail | null>(null);
  let detailLoading = $state(false);
  let detailError = $state("");
  let detailBusy = $state(false);      // Remove-a-local-source in flight

  // Add-a-source modal
  let addOpen = $state(false);
  let addName = $state("");
  let addUrl = $state("");
  let addDesc = $state("");
  let addCategory = $state("");   // taxonomy category id
  let addSubcategory = $state("");
  let addKind = $state<LocalSourceInput["access_kind"]>("http_json");
  let addNeedsKey = $state(false);
  let addSuggest = $state(false);
  let addBusy = $state(false);
  let addError = $state("");

  // The composed form value + its live error (mirrors the server rules). Read
  // in a $derived so the Save button disables the instant the form goes invalid.
  const addFormValue = $derived<LocalSourceInput>({
    name: addName,
    url: addUrl,
    description: addDesc,
    category: addSubcategory ? `${addCategory}/${addSubcategory}` : addCategory,
    access_kind: addKind,
    needs_key: addNeedsKey,
    suggest: addSuggest,
  });
  const addFormError = $derived(validateLocalForm(addFormValue));

  // Category browse — the picked category's subcategories, if any.
  const subcategories = $derived(
    category ? (taxonomy.find((c) => c.id === category)?.subcategories ?? []) : [],
  );

  // Debounce timer for the search input. Cleared on every keystroke; only the
  // last keystroke's timeout survives to move `query` and trigger a fetch.
  let debounceTimer: ReturnType<typeof setTimeout> | null = null;

  // --- lifecycle ---------------------------------------------------------------------------
  onMount(async () => {
    console.assert(typeof api.libraryStatus === "function", "onMount: libraryStatus present");
    console.assert(typeof api.libraryTaxonomy === "function", "onMount: libraryTaxonomy present");
    if (account.status === null) await account.load();
    const s = account.status;
    if (s && !s.initialized) return goto("/setup");
    if (s && !s.unlocked) return goto("/unlock");
    await refreshStatus();
    if (status?.installed) await Promise.all([loadTaxonomy(), runSearch()]);
  });

  async function refreshStatus(): Promise<void> {
    console.assert(typeof api.libraryStatus === "function", "refreshStatus: libraryStatus present");
    console.assert(installBusy === false, "refreshStatus: never during an install");
    statusError = "";
    try {
      status = await api.libraryStatus();
    } catch (err) {
      statusError = describeError(err);
    }
  }

  async function loadTaxonomy(): Promise<void> {
    console.assert(status?.installed === true, "loadTaxonomy: only when installed");
    console.assert(Array.isArray(taxonomy), "loadTaxonomy: taxonomy is array");
    taxonomyError = "";
    try {
      const out = await api.libraryTaxonomy();
      taxonomy = out.categories;
    } catch (err) {
      taxonomyError = describeError(err);
    }
  }

  // --- install (first use) -----------------------------------------------------------------
  async function startInstall(): Promise<void> {
    console.assert(installBusy === false, "startInstall: no concurrent install");
    console.assert(typeof api.libraryInstall === "function", "startInstall: libraryInstall present");
    if (installBusy) return;
    installBusy = true;
    installError = "";
    try {
      status = await api.libraryInstall();
      // Fetch the browse surface the moment the pack lands.
      await Promise.all([loadTaxonomy(), runSearch()]);
    } catch (err) {
      installError = describeError(err) || "Couldn't download the Library — try again in a moment.";
    } finally {
      installBusy = false;
    }
  }

  // --- search ------------------------------------------------------------------------------
  function onSearchInput(): void {
    console.assert(typeof searchText === "string", "onSearchInput: searchText is string");
    console.assert(true, "onSearchInput: total");
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      query = searchText.trim();
      void runSearch();
    }, 250);
  }

  function pickCategory(id: string): void {
    console.assert(typeof id === "string", "pickCategory: id is string");
    console.assert(Array.isArray(taxonomy), "pickCategory: taxonomy present");
    category = category === id ? "" : id; // tap again to clear
    subcategory = "";
    void runSearch();
  }

  function pickSubcategory(id: string): void {
    console.assert(typeof id === "string", "pickSubcategory: id is string");
    console.assert(category !== "", "pickSubcategory: category first");
    subcategory = subcategory === id ? "" : id;
    void runSearch();
  }

  function pickTier(id: TierFilter): void {
    console.assert(typeof id === "string", "pickTier: id is string");
    console.assert(TIER_FILTERS.some((t) => t.id === id), "pickTier: known tier");
    tier = id;
    void runSearch();
  }

  async function runSearch(): Promise<void> {
    console.assert(typeof api.librarySources === "function", "runSearch: librarySources present");
    console.assert(status?.installed === true, "runSearch: pack must be installed");
    if (!status?.installed) return;
    listBusy = true;
    listError = "";
    offset = 0;
    try {
      const out = await api.librarySources(buildSearchQuery({
        q: query, category, subcategory, tier, offset: 0, limit: PAGE_SIZE,
      }));
      applyFirstPage(out);
    } catch (err) {
      listError = describeError(err);
    } finally {
      listBusy = false;
    }
  }

  function applyFirstPage(out: LibrarySearchResult): void {
    console.assert(typeof out === "object", "applyFirstPage: out is object");
    console.assert(Array.isArray(out.results), "applyFirstPage: results array");
    total = out.total;
    offset = out.results.length;
    results = out.results;
    localRows = out.local;
  }

  async function loadMore(): Promise<void> {
    console.assert(status?.installed === true, "loadMore: pack must be installed");
    console.assert(moreBusy === false, "loadMore: no concurrent fetch");
    if (moreBusy || offset >= total) return;
    moreBusy = true;
    try {
      const out = await api.librarySources(buildSearchQuery({
        q: query, category, subcategory, tier, offset, limit: PAGE_SIZE,
      }));
      results = [...results, ...out.results];
      offset += out.results.length;
    } catch (err) {
      listError = describeError(err);
    } finally {
      moreBusy = false;
    }
  }

  // --- detail modal ------------------------------------------------------------------------
  async function openDetail(row: LibrarySourceRow): Promise<void> {
    console.assert(typeof row === "object", "openDetail: row is object");
    console.assert(row.id.length > 0, "openDetail: id present");
    detailFor = row;
    detail = null;
    detailError = "";
    detailLoading = true;
    try {
      detail = await api.librarySource(row.id);
    } catch (err) {
      detailError = describeError(err);
    } finally {
      detailLoading = false;
    }
  }

  function closeDetail(): void {
    console.assert(detailBusy === false, "closeDetail: never during a request");
    console.assert(true, "closeDetail: total");
    detailFor = null;
    detail = null;
    detailError = "";
  }

  async function removeLocal(): Promise<void> {
    console.assert(detail !== null, "removeLocal: detail loaded");
    console.assert(detail?.tier === "local", "removeLocal: only your sources");
    if (!detail || detail.tier !== "local") return;
    const ok = await confirmDialog({
      title: "Remove this source?",
      body: `"${detail.name}" will be removed from your sources.`,
      confirmLabel: "Remove",
      danger: true,
    });
    if (!ok) return;
    detailBusy = true;
    try {
      await api.libraryLocalDelete(detail.id);
      toast("Source removed.");
      closeDetail();
      await runSearch();
    } catch (err) {
      detailError = describeError(err);
    } finally {
      detailBusy = false;
    }
  }

  // --- add-a-source modal ------------------------------------------------------------------
  function openAdd(): void {
    console.assert(status?.installed === true, "openAdd: only when installed");
    console.assert(addOpen === false, "openAdd: not already open");
    addOpen = true;
    addName = "";
    addUrl = "";
    addDesc = "";
    addCategory = "";
    addSubcategory = "";
    addKind = "http_json";
    addNeedsKey = false;
    addSuggest = false;
    addError = "";
  }

  function closeAdd(): void {
    console.assert(addBusy === false, "closeAdd: never during a save");
    console.assert(true, "closeAdd: total");
    addOpen = false;
    addError = "";
  }

  async function submitAdd(): Promise<void> {
    console.assert(addBusy === false, "submitAdd: no concurrent save");
    console.assert(addFormError === null, "submitAdd: form must be valid");
    if (addBusy || addFormError !== null) return;
    addBusy = true;
    addError = "";
    try {
      await api.libraryLocalAdd(addFormValue);
      toast("Added to your sources.");
      addOpen = false;
      await runSearch();
    } catch (err) {
      addError = describeError(err) || "That didn't save — check the form and try again.";
    } finally {
      addBusy = false;
    }
  }

  // Small helper for the header count — total on this filter, or the full pack count.
  // The user's own matching sources count too ("Mine" shows only those).
  const headerCount = $derived(
    status?.installed
      ? (query || category || tier ? total + localRows.length : (status.records ?? total))
      : 0,
  );

  const hasResults = $derived(results.length > 0 || localRows.length > 0);
</script>

{#if !account.status?.unlocked}
  <Spinner block />
{:else}
  <div class="lib-head">
    <div class="lib-title">
      <h1>Library</h1>
      {#if status?.installed}
        <p class="muted lib-count">{formatCount(headerCount)} {headerCount === 1 ? "source" : "sources"}</p>
      {/if}
    </div>
    {#if status?.installed}
      <button onclick={openAdd} title="Add a source of your own">
        <Icon name="plus" /> Add a source
      </button>
    {/if}
  </div>

  {#if statusError}
    <p class="error">{statusError}</p>
  {/if}

  {#if !status}
    <Spinner block />
  {:else if !status.installed}
    <!-- First use: the pack downloads on demand (public catalog data, so plaintext at rest). -->
    <section class="lib-first-use card">
      <h2>Browse the SmartBrain Library</h2>
      <p>
        A registry of about {formatCount(9000)} US data sources — weather, tides,
        markets, sports, transit, government open data — you can browse, add to
        your Neural Interface, or extend with your own.
      </p>
      <p class="muted">About 18&nbsp;MB. Takes a few seconds on a normal connection.</p>
      {#if installError}<p class="error">{installError}</p>{/if}
      <p class="lib-install-actions">
        <button onclick={startInstall} disabled={installBusy}>
          {installBusy ? "Downloading…" : "Download the Library"}
        </button>
        {#if installError && !installBusy}
          <button class="secondary" onclick={startInstall}>Retry</button>
        {/if}
      </p>
    </section>
  {:else}
    <!-- Browse surface: search + categories + tier filter + results. -->
    <section class="lib-browse">
      <div class="lib-search">
        <label class="visually-hidden" for="lib-q">Search sources</label>
        <input
          id="lib-q"
          type="search"
          bind:value={searchText}
          oninput={onSearchInput}
          placeholder={`Search — try “tides” or “bitcoin”`}
        />
      </div>

      {#if taxonomyError}
        <p class="error">{taxonomyError}</p>
      {/if}

      {#if taxonomy.length > 0}
        <div class="lib-cats" role="tablist" aria-label="Categories">
          <button
            class="lib-cat-btn"
            class:active={category === ""}
            role="tab"
            aria-selected={category === ""}
            onclick={() => pickCategory("")}
          >All</button>
          {#each taxonomy as c (c.id)}
            <button
              class="lib-cat-btn"
              class:active={category === c.id}
              role="tab"
              aria-selected={category === c.id}
              onclick={() => pickCategory(c.id)}
              title={`${formatCount(c.count)} sources`}
            >{c.label}</button>
          {/each}
        </div>
      {/if}

      {#if category && subcategories.length > 0}
        <div class="lib-subs">
          {#each subcategories as s (s.id)}
            <Chip
              kind={subcategory === s.id ? "accent" : ""}
              onclick={() => pickSubcategory(s.id)}
              title={`${formatCount(s.count)} sources`}
            >{s.label}</Chip>
          {/each}
        </div>
      {/if}

      <div class="lib-tiers" role="tablist" aria-label="Tier filter">
        {#each TIER_FILTERS as t (t.id)}
          <button
            class="lib-tier-btn"
            class:active={tier === t.id}
            role="tab"
            aria-selected={tier === t.id}
            onclick={() => pickTier(t.id)}
          >{t.label}</button>
        {/each}
      </div>

      <p class="lib-footlink muted">
        Looking for ready-made cards? <a href="/ni#templates">Card templates</a>.
      </p>

      {#if listError}
        <p class="error">{listError}</p>
      {/if}

      {#if listBusy}
        <Spinner block />
      {:else if !hasResults}
        <EmptyState
          icon="search"
          title="No sources match"
          body="Try a different word, clear the category, or add your own source."
        >
          <button onclick={openAdd}><Icon name="plus" /> Add a source</button>
        </EmptyState>
      {:else}
        {#if localRows.length > 0}
          <h2 class="lib-section-h">Your sources</h2>
          <ul class="lib-rows">
            {#each localRows as row (row.id)}
              <li>
                <button class="lib-row" onclick={() => openDetail(row)}>
                  <span class="lib-row-name">{row.name}</span>
                  <span class="lib-row-provider">{row.provider}</span>
                  {#if row.description}
                    <span class="lib-row-desc">{row.description}</span>
                  {/if}
                  <span class="lib-row-chips">
                    <Chip kind="accent">{tierLabel(row.tier)}</Chip>
                    <Chip>{authorityLabel(row.authority)}</Chip>
                    <Chip kind={statusChipKind(row.status, row.auth)}>
                      {statusLabel(row.status, row.auth)}
                    </Chip>
                    {#if rowCategoryPhrase(row, taxonomy)}
                      <Chip>{rowCategoryPhrase(row, taxonomy)}</Chip>
                    {/if}
                  </span>
                </button>
              </li>
            {/each}
          </ul>
        {/if}

        {#if results.length > 0}
          {#if localRows.length > 0}<h2 class="lib-section-h">Library</h2>{/if}
          <ul class="lib-rows">
            {#each results as row (row.id)}
              <li>
                <button class="lib-row" onclick={() => openDetail(row)}>
                  <span class="lib-row-name">{row.name}</span>
                  <span class="lib-row-provider">{row.provider}</span>
                  {#if row.description}
                    <span class="lib-row-desc">{row.description}</span>
                  {/if}
                  <span class="lib-row-chips">
                    <Chip>{authorityLabel(row.authority)}</Chip>
                    <Chip>{tierLabel(row.tier)}</Chip>
                    <Chip kind={statusChipKind(row.status, row.auth)}>
                      {statusLabel(row.status, row.auth)}
                    </Chip>
                    {#if rowCategoryPhrase(row, taxonomy)}
                      <Chip>{rowCategoryPhrase(row, taxonomy)}</Chip>
                    {/if}
                  </span>
                </button>
              </li>
            {/each}
          </ul>
          {#if offset < total}
            <p class="lib-more">
              <button class="secondary" onclick={loadMore} disabled={moreBusy}>
                {moreBusy ? "Loading…" : `Show more (${formatCount(total - offset)} left)`}
              </button>
            </p>
          {/if}
        {/if}
      {/if}
    </section>
  {/if}
{/if}

<!-- Detail modal ------------------------------------------------------------------------- -->
{#if detailFor}
  <Modal open size="lg" label={detailFor.name} onclose={closeDetail}>
    <div class="lib-detail-head">
      <h2>{detailFor.name}</h2>
      <button class="ghost" onclick={closeDetail} aria-label="Close">
        <Icon name="x" />
      </button>
    </div>

    {#if detailLoading}
      <Spinner block />
    {:else if detailError}
      <p class="error">{detailError}</p>
    {:else if detail}
      {#if detail.description}
        <p class="lib-detail-desc">{detail.description}</p>
      {/if}
      <p class="lib-detail-meta">
        <Chip>{authorityLabel(detail.provider.authority)}</Chip>
        <Chip>{tierLabel(detail.tier)}</Chip>
        <Chip kind={statusChipKind(detail.validation.status, detail.access.auth)}>
          {statusLabel(detail.validation.status, detail.access.auth)}
        </Chip>
      </p>

      <dl class="lib-detail-list">
        <dt>Provider</dt>
        <dd>
          {#if detail.provider.url}
            <a href={detail.provider.url} target="_blank" rel="noreferrer">{detail.provider.name}</a>
          {:else}
            {detail.provider.name}
          {/if}
        </dd>

        {#if detail.categories.length > 0}
          <dt>What it answers</dt>
          <dd>
            {#each detail.categories as c, i (c)}
              {#if i > 0} · {/if}{labelCategory(c, taxonomy)}
            {/each}
          </dd>
        {/if}

        <dt>Address</dt>
        <dd><code class="lib-mono">{detail.access.url_template}</code></dd>

        {#if detail.access.params.length > 0}
          <dt>Parameters</dt>
          <dd>
            <ul class="lib-params-list">
              {#each detail.access.params as p (p.name)}
                <li>
                  <code class="lib-mono">{p.name}</code>
                  {#if p.required} · required{/if}
                  {#if p.example} · e.g. <code class="lib-mono">{p.example}</code>{/if}
                </li>
              {/each}
            </ul>
          </dd>
        {/if}

        <dt>Needs a key</dt>
        <dd>{detail.access.auth === "none" ? "No" : "Yes"}</dd>

        <dt>Data format</dt>
        <dd>{accessKindLabel(detail.access.kind)}</dd>

        <dt>Terms</dt>
        <dd>
          {termsLabel(detail.terms.status)}
          {#if detail.terms.terms_url}
            &nbsp;· <a href={detail.terms.terms_url} target="_blank" rel="noreferrer">Read terms</a>
          {/if}
        </dd>

        <dt>How often it updates</dt>
        <dd>{cadenceLabel(detail.freshness.cadence)}</dd>

        <dt>Last check</dt>
        <dd>
          {statusLabel(detail.validation.status, detail.access.auth)}
          {#if checkedOnLabel(detail.validation.checked_at)} · checked {checkedOnLabel(detail.validation.checked_at)}{/if}
        </dd>

        {#if detail.examples.length > 0}
          <dt>Example asks</dt>
          <dd>
            <ul class="lib-examples">
              {#each detail.examples as e, i (i)}
                <li>{e}</li>
              {/each}
            </ul>
          </dd>
        {/if}
      </dl>

      <div class="lib-detail-actions">
        {#if detail.tier === "local"}
          <button class="ni-danger" onclick={removeLocal} disabled={detailBusy}>
            {detailBusy ? "Removing…" : "Remove"}
          </button>
        {/if}
        <button class="ghost" onclick={closeDetail}>Close</button>
      </div>
    {/if}
  </Modal>
{/if}

<!-- Add-a-source modal ------------------------------------------------------------------- -->
{#if addOpen}
  <Modal open size="md" label="Add a source" onclose={closeAdd}>
    <div class="lib-detail-head">
      <h2>Add a source</h2>
      <button class="ghost" onclick={closeAdd} aria-label="Close">
        <Icon name="x" />
      </button>
    </div>

    <form onsubmit={(e) => { e.preventDefault(); void submitAdd(); }}>
      <Field
        label="Name"
        error={addFormError?.field === "name" ? addFormError.message : ""}
      >
        <input type="text" bind:value={addName} maxlength="120" required />
      </Field>

      <Field
        label="Address"
        hint={`Use {name} for parts that change, e.g. {station}.`}
        error={addFormError?.field === "url" ? addFormError.message : ""}
      >
        <input type="url" bind:value={addUrl} placeholder="https://…" required />
      </Field>

      <Field
        label="What it answers"
        hint="Optional — one sentence about the data."
        error={addFormError?.field === "description" ? addFormError.message : ""}
      >
        <input type="text" bind:value={addDesc} maxlength="600" />
      </Field>

      <Field
        label="Category"
        error={addFormError?.field === "category" ? addFormError.message : ""}
      >
        <select bind:value={addCategory} onchange={() => (addSubcategory = "")}>
          <option value="">Choose a category…</option>
          {#each taxonomy as c (c.id)}
            <option value={c.id}>{c.label}</option>
          {/each}
        </select>
      </Field>

      {#if addCategory}
        <Field label="Subcategory">
          <select bind:value={addSubcategory}>
            <option value="">Choose a subcategory…</option>
            {#each taxonomy.find((c) => c.id === addCategory)?.subcategories ?? [] as s (s.id)}
              <option value={s.id}>{s.label}</option>
            {/each}
          </select>
        </Field>
      {/if}

      <Field label="Data format">
        <select bind:value={addKind}>
          {#each accessKindOptions() as k (k.id)}
            <option value={k.id}>{k.label}</option>
          {/each}
        </select>
      </Field>

      <label class="lib-checkbox-row">
        <input type="checkbox" bind:checked={addNeedsKey} />
        <span>
          Needs a key
          <span class="fhint">SmartBrain will ask you for it — never paste keys into the address.</span>
        </span>
      </label>

      <label class="lib-checkbox-row">
        <input type="checkbox" bind:checked={addSuggest} />
        <span>
          Also suggest it to the SmartBrain Library
          <span class="fhint">Sends the name, description, category and address pattern for review —
            never a key, and nothing you filled into the address.</span>
        </span>
      </label>

      {#if addError}<p class="error lib-form-error">{addError}</p>{/if}

      <p class="muted lib-form-note">{addSuggest
        ? "Your copy stays on this device; the suggestion is sent in the background."
        : "Your sources stay on this device."}</p>

      <div class="lib-detail-actions">
        <button type="submit" disabled={addBusy || addFormError !== null}>
          {addBusy ? "Saving…" : "Save"}
        </button>
        <button type="button" class="ghost" onclick={closeAdd} disabled={addBusy}>Cancel</button>
      </div>
    </form>
  </Modal>
{/if}

<style>
  .lib-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s-2);
    flex-wrap: wrap;
    margin: 0 0 var(--s-4);
  }
  .lib-head h1 { margin: 0; }
  .lib-title { display: flex; flex-direction: column; gap: 2px; }
  .lib-count { margin: 0; font-size: var(--f-meta); }

  .lib-first-use { padding: var(--s-5); }
  .lib-install-actions {
    display: flex;
    gap: var(--s-2);
    flex-wrap: wrap;
    margin: var(--s-3) 0 0;
  }

  .lib-browse { display: flex; flex-direction: column; gap: var(--s-3); }

  .lib-search input {
    /* Full width — searching one thing, the input owns the row */
    width: 100%;
  }

  .lib-cats {
    display: flex;
    gap: var(--s-1);
    flex-wrap: wrap;
  }
  .lib-cat-btn {
    padding: 6px 12px;
    min-height: 32px;
    background: transparent;
    border: 1px solid var(--border);
    border-radius: var(--r-full);
    color: var(--muted);
    font-size: var(--f-label);
    font-weight: 500;
  }
  .lib-cat-btn:hover {
    color: var(--text);
    border-color: var(--border-strong);
    filter: none;
  }
  .lib-cat-btn.active {
    color: var(--accent);
    border-color: var(--accent);
    background: var(--accent-tint);
  }

  .lib-subs {
    display: flex;
    gap: var(--s-1);
    flex-wrap: wrap;
  }

  .lib-tiers {
    display: inline-flex;
    gap: 2px;
    padding: 2px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--r-full);
    align-self: flex-start;
    flex-wrap: wrap;
  }
  .lib-tier-btn {
    padding: 6px 14px;
    min-height: 32px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: var(--r-full);
    color: var(--muted);
    font-size: var(--f-label);
    font-weight: 500;
  }
  .lib-tier-btn:hover {
    color: var(--text);
    filter: none;
  }
  .lib-tier-btn.active {
    color: var(--text);
    background: var(--elevated);
    border-color: var(--border);
  }

  .lib-footlink { margin: 0; font-size: var(--f-meta); }

  .lib-section-h {
    margin: var(--s-3) 0 var(--s-2);
    font-size: var(--f-label);
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 600;
  }

  .lib-rows {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: var(--s-2);
  }
  .lib-row {
    /* Custom fixed-width row button — the global button padding pitfall (repo lesson):
       we hand-lay padding here so the row spans full width without inheriting the pill's. */
    padding: var(--s-3);
    width: 100%;
    display: grid;
    grid-template-columns: 1fr auto;
    grid-template-areas:
      "name provider"
      "desc desc"
      "chips chips";
    align-items: start;
    gap: 2px var(--s-3);
    text-align: left;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--r-2);
    color: var(--text);
    font-weight: 400;
    min-height: 0;
    cursor: pointer;
  }
  .lib-row:hover {
    border-color: var(--border-strong);
    filter: none;
  }
  .lib-row:focus-visible {
    outline: 2px solid var(--focus);
    outline-offset: 2px;
  }
  .lib-row-name {
    grid-area: name;
    font-weight: 600;
    font-size: var(--f-body);
  }
  .lib-row-provider {
    grid-area: provider;
    color: var(--muted);
    font-size: var(--f-meta);
    align-self: center;
  }
  .lib-row-desc {
    grid-area: desc;
    color: var(--muted);
    font-size: var(--f-label);
    line-height: var(--lh-ui);
    /* harvested catalogs carry long descriptions; the detail view shows them in full */
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    overflow: hidden;
  }
  .lib-row-chips {
    grid-area: chips;
    display: inline-flex;
    gap: var(--s-1);
    flex-wrap: wrap;
    margin-top: var(--s-1);
  }

  .lib-more {
    display: flex;
    justify-content: center;
    margin: var(--s-3) 0 0;
  }

  /* Detail + add-a-source modal shared bits */
  .lib-detail-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s-2);
    margin: 0 0 var(--s-3);
  }
  .lib-detail-head h2 { margin: 0; }
  /* The close ✕ is a fixed-width icon-only button — the global padding must be 0. */
  .lib-detail-head button.ghost {
    padding: 0;
    min-height: 0;
    width: 32px;
    height: 32px;
    display: grid;
    place-items: center;
  }
  .lib-detail-desc { margin: 0 0 var(--s-3); font-size: var(--f-label); }
  .lib-detail-meta {
    margin: 0 0 var(--s-3);
    display: inline-flex;
    gap: var(--s-1);
    flex-wrap: wrap;
  }
  .lib-detail-list {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: var(--s-1) var(--s-3);
    margin: 0;
  }
  .lib-detail-list dt {
    color: var(--muted);
    font-size: var(--f-meta);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 600;
    padding-top: 4px;
  }
  .lib-detail-list dd {
    margin: 0;
    font-size: var(--f-label);
    color: var(--text);
    word-break: break-word;
  }
  .lib-mono {
    font-family: var(--font-mono);
    font-size: var(--f-label);
    color: var(--text);
    word-break: break-all;
  }
  .lib-params-list, .lib-examples {
    list-style: disc inside;
    padding: 0;
    margin: 0;
    font-size: var(--f-label);
  }
  .lib-detail-actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--s-2);
    margin: var(--s-4) 0 0;
    flex-wrap: wrap;
  }
  .ni-danger {
    background: transparent;
    color: var(--danger);
    border: 1px solid var(--danger);
  }
  .ni-danger:hover { background: color-mix(in srgb, var(--danger) 8%, transparent); filter: none; }

  .lib-checkbox-row {
    display: flex;
    align-items: flex-start;
    gap: var(--s-2);
    margin: var(--s-3) 0 0;
    color: var(--text);
    cursor: pointer;
  }
  .lib-checkbox-row input[type="checkbox"] { margin-top: 0.2em; flex-shrink: 0; }
  .lib-checkbox-row .fhint {
    display: block;
    color: var(--faint);
    font-size: var(--f-meta);
    margin-top: 2px;
  }
  .lib-form-note { margin: var(--s-3) 0 0; font-size: var(--f-meta); }
  .lib-form-error { margin: var(--s-3) 0 0; }

  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
  }

  .error { color: var(--danger); }

  /* Phone: single column, no horizontal scroll (nothing to override — the grid
     already collapses; the rows use grid-template-columns 1fr auto which stays
     healthy under 375px). Explicit fold below just tightens spacing. */
  @media (max-width: 480px) {
    /* 18 categories would push results a screen down: on a phone they scroll sideways in one row */
    .lib-cats, .lib-subs {
      flex-wrap: nowrap;
      overflow-x: auto;
      scrollbar-width: none;
      margin-inline: calc(-1 * var(--s-4));  /* bleed to the 16px phone gutter */
      padding-inline: var(--s-4);
    }
    .lib-cats::-webkit-scrollbar, .lib-subs::-webkit-scrollbar { display: none; }
    .lib-cats > :global(*), .lib-subs > :global(*) { flex: 0 0 auto; }
    .lib-detail-list { grid-template-columns: 1fr; }
    .lib-detail-list dt { padding-top: var(--s-2); }
    /* the name gets the full width; the provider sits under it */
    .lib-row {
      grid-template-columns: minmax(0, 1fr);  /* a long unbroken word must not widen the page */
      grid-template-areas: "name" "provider" "desc" "chips";
    }
    .lib-row > :global(*) { min-width: 0; overflow-wrap: anywhere; }
  }
</style>
