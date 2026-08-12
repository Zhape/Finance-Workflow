<script>
	import { age, api, when } from '$lib/api.js';

	let { data } = $props();

	let emails = $state(data.emails ?? []);
	let status = $state(data.status);
	let stats = $state(data.status?.stats ?? null);
	let selectedId = $state(null);
	let detail = $state(null);

	let syncing = $state(false);
	let loadingDetail = $state(false);
	let busy = $state(false);
	let error = $state('');
	let notice = $state('');
	let showThread = $state(false);
	let editing = $state(false);
	let draftSubject = $state('');
	let draftBody = $state('');

	let showSettings = $state(false);
	let xeroOrgs = $state([]);
	let xeroOrgError = $state('');
	let models = $state([]);
	let modelError = $state('');
	let draftXeroTenant = $state('');
	let draftModel = $state('');
	let draftLookback = $state(7);

	const mailboxes = $derived(status?.mailboxes ?? []);
	const connected = $derived(mailboxes.length > 0);
	// The organisation actually being read. Prefer the explicit choice; the
	// name stored on the connection is only ever whichever one Xero listed
	// first, so it can name a company we are not reading.
	const selectedXeroName = $derived(
		xeroOrgs.find((o) => o.tenantId === status?.settings?.xeroTenantId)?.name ??
			status?.xero?.tenantName ??
			status?.xero?.connection ??
			'not set'
	);
	const inboxErrors = $derived(status?.errors ?? []);
	const degraded = $derived(
		status?.classifier && (!status.classifier.configured || status.classifier.circuitOpen)
	);

	// The draft on screen, which is the edited text once someone starts typing.
	const currentSubject = $derived(editing ? draftSubject : (detail?.draft?.subject ?? ''));
	const currentBody = $derived(editing ? draftBody : (detail?.draft?.body ?? ''));

	// Recomputed from the text in the box rather than read from the saved
	// draft: deleting a placeholder is the documented way to unblock a reply,
	// and it should unblock the button as you type.
	const openSlots = $derived([
		...new Set([...findSlots(currentSubject), ...findSlots(currentBody)])
	]);
	const alreadyDrafted = $derived(detail?.reply?.status === 'created');
	const canDraft = $derived(
		detail && !alreadyDrafted && openSlots.length === 0 && !!detail.email?.fromEmail
	);

	function findSlots(text) {
		return [...String(text ?? '').matchAll(/\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}/g)].map(
			(m) => m[1]
		);
	}

	function categoryLabel(key) {
		if (!key) return 'Unclassified';
		const found = (status?.categories ?? []).find((c) => c.key === key);
		return found?.label ?? key;
	}

	async function select(id) {
		selectedId = id;
		loadingDetail = true;
		error = '';
		notice = '';
		showThread = false;
		editing = false;
		try {
			detail = await api.inboxEmail(id);
		} catch (e) {
			error = e.message;
			detail = null;
		}
		loadingDetail = false;
	}

	async function refreshList() {
		const result = await api.inboxEmails();
		emails = result.emails;
		stats = result.stats;
	}

	async function sync() {
		syncing = true;
		error = '';
		notice = '';
		try {
			const result = await api.syncInbox();
			const r = result.report;
			const parts = [`${r.ingested} new`];
			if (r.suppressed) parts.push(`${r.suppressed} auto-replies ignored`);
			if (r.failed) parts.push(`${r.failed} failed`);
			notice = `Synced: ${parts.join(', ')}.`;
			if (r.moreWaiting) notice += ' There is more waiting — sync again.';
			for (const m of r.mailboxErrors) error = `${error} ${m}`.trim();
			status = { ...status, errors: result.errors };
			await refreshList();
			if (selectedId) await select(selectedId);
		} catch (e) {
			error = e.message;
		}
		syncing = false;
	}

	async function connectMailbox() {
		try {
			const { url } = await api.connectMailbox();
			window.location.assign(url);
		} catch (e) {
			error = e.message;
		}
	}

	async function openSettings() {
		showSettings = !showSettings;
		if (!showSettings) return;
		draftLookback = status?.settings?.lookbackDays ?? 7;
		draftModel = status?.settings?.classifierModel ?? '';
		// Both lists come from the providers themselves, so the choices offered
		// are ones that provably work rather than names someone typed.
		try {
			const orgs = await api.inboxXeroOrgs();
			xeroOrgs = orgs.organisations;
			draftXeroTenant = orgs.selected ?? '';
			xeroOrgError = orgs.error ?? '';
		} catch (e) {
			xeroOrgs = [];
			xeroOrgError = e.message;
		}
		try {
			const found = await api.inboxModels();
			models = found.models;
			modelError = found.error ?? '';
		} catch (e) {
			models = [];
			modelError = e.message;
		}
	}

	async function saveSettings() {
		busy = true;
		error = '';
		try {
			await api.saveInboxSettings({
				lookbackDays: Number(draftLookback),
				xeroConnection: status?.settings?.xeroConnection ?? 'default',
				xeroTenantId: draftXeroTenant || null,
				classifierModel: draftModel || null
			});
			status = await api.inboxStatus();
			notice = 'Settings saved. Sync again to apply them.';
			showSettings = false;
		} catch (e) {
			error = e.message;
		}
		busy = false;
	}

	async function retryClassifier() {
		busy = true;
		try {
			const result = await api.resetInboxClassifier();
			status = { ...status, classifier: result.classifier };
			notice = 'Classifier re-enabled. Sync to try it again.';
		} catch (e) {
			error = e.message;
		}
		busy = false;
	}

	async function removeMailbox(mailbox) {
		if (!confirm(`Stop reading ${mailbox.address}? Mail already ingested from it is removed too.`))
			return;
		try {
			await api.removeMailbox(mailbox.id);
			status = await api.inboxStatus();
			await refreshList();
			notice = `${mailbox.address} disconnected.`;
		} catch (e) {
			error = e.message;
		}
	}

	async function changeCategory(event) {
		const key = event.currentTarget.value;
		busy = true;
		error = '';
		try {
			detail = await api.setInboxCategory(selectedId, key);
			editing = false;
			await refreshList();
		} catch (e) {
			error = e.message;
		}
		busy = false;
	}

	function startEditing() {
		draftSubject = detail?.draft?.subject ?? '';
		draftBody = detail?.draft?.body ?? '';
		editing = true;
	}

	async function saveDraft() {
		busy = true;
		error = '';
		try {
			await api.saveInboxDraft(selectedId, draftSubject, draftBody);
			detail = await api.inboxEmail(selectedId);
			editing = false;
			notice = 'Draft saved.';
		} catch (e) {
			error = e.message;
		}
		busy = false;
	}

	async function createDraft() {
		busy = true;
		error = '';
		notice = '';
		try {
			if (editing) await api.saveInboxDraft(selectedId, draftSubject, draftBody);
			const result = await api.draftReply(selectedId);
			notice = result.alreadyDrafted
				? 'This reply was already drafted — nothing was duplicated.'
				: 'Draft created in Gmail. Open Gmail to read it and press send.';
			detail = await api.inboxEmail(selectedId);
			editing = false;
			await refreshList();
		} catch (e) {
			error = e.message;
			detail = await api.inboxEmail(selectedId);
		}
		busy = false;
	}

	async function dismiss() {
		busy = true;
		try {
			await api.dismissInboxEmail(selectedId);
			detail = null;
			selectedId = null;
			await refreshList();
		} catch (e) {
			error = e.message;
		}
		busy = false;
	}

	async function clearErrors() {
		await api.dismissInboxErrors();
		status = { ...status, errors: [] };
	}
</script>

<div class="head">
	<div>
		<h1>Invoice Inbox</h1>
		<p class="lede">
			Customer mail, checked against Xero and answered from your approved templates. Replies
			are created as Gmail drafts — nothing is sent from here.
		</p>
	</div>
	<div class="actions">
		<a class="ghost" href="/inbox/templates">Templates</a>
		<button class="ghost" onclick={openSettings}>Settings</button>
		<!-- Always offered, not only when nothing is connected. Several
		     mailboxes per organisation is the normal case — one per person,
		     plus whatever a Google Group delivers — and hiding this once the
		     first one was added made the second one impossible. -->
		<button class="ghost" onclick={connectMailbox}>
			{connected ? 'Add a mailbox' : 'Connect a mailbox'}
		</button>
		{#if connected}
			<button onclick={sync} disabled={syncing}>{syncing ? 'Syncing…' : 'Sync now'}</button>
		{/if}
	</div>
</div>

{#if !data.available}
	<p class="empty box">
		This organisation does not have the Invoice Inbox, or you are not signed in.
		{#if data.reason}<br /><span class="muted">{data.reason}</span>{/if}
	</p>
{:else}
	{#if stats}
		<div class="stats">
			<div><strong>{stats.received}</strong><span>arrived, last {stats.days} days</span></div>
			<div><strong>{stats.drafted}</strong><span>replies drafted</span></div>
			<div><strong>{stats.waiting}</strong><span>waiting on you</span></div>
			<div>
				<strong>{stats.medianHoursToDraft ?? '—'}</strong><span>median hours to reply</span>
			</div>
			<div><strong>{stats.suppressed}</strong><span>auto-replies ignored</span></div>
		</div>
	{/if}

	{#if degraded}
		<div class="banner warn">
			<div>
				{#if !status.classifier.configured}
					No classifier is configured, so nothing is being categorised automatically.
					Every email still arrives with a template attached — choose the category
					yourself.
				{:else}
					Classification is paused after {status.classifier.consecutiveFailures}
					consecutive failures. Everything is going straight to manual review.
					<!-- The reason, not just the symptom. A banner that says only
					     "degraded" tells someone they have a problem without
					     telling them which one. -->
					{#if status.classifier.lastError}
						<br /><span class="reason">{status.classifier.lastError}</span>
					{/if}
				{/if}
			</div>
			{#if status.classifier.configured}
				<button class="link" onclick={retryClassifier} disabled={busy}>Try again</button>
			{/if}
		</div>
	{/if}

	{#if inboxErrors.length}
		<div class="banner err">
			<div>
				<strong>{inboxErrors.length} problem(s) during the last sync.</strong>
				<ul>
					{#each inboxErrors.slice(0, 4) as problem (problem.id)}
						<li><code>{problem.code}</code> {problem.message}</li>
					{/each}
				</ul>
			</div>
			<button class="link" onclick={clearErrors}>Dismiss</button>
		</div>
	{/if}

	{#if notice}<p class="banner ok">{notice}</p>{/if}
	{#if error}<p class="banner err">{error}</p>{/if}

	{#if showSettings}
		<div class="box settings">
			<h3>What this inbox reads</h3>
			<div class="fields">
				<label for="xero-org">
					Xero organisation
					<select id="xero-org" bind:value={draftXeroTenant} disabled={busy}>
						{#if !xeroOrgs.length}
							<option value="">{xeroOrgError || 'Loading…'}</option>
						{/if}
						{#each xeroOrgs as organisation (organisation.tenantId)}
							<option value={organisation.tenantId}>{organisation.name}</option>
						{/each}
					</select>
					<span class="hint">
						Every organisation this Xero connection can reach. Invoice lookups run
						against the one selected here.
					</span>
				</label>

				<label for="model">
					Classifier model
					<select id="model" bind:value={draftModel} disabled={busy}>
						<option value="">Platform default ({status?.classifier?.model})</option>
						{#each models as name (name)}
							<option value={name}>{name}</option>
						{/each}
					</select>
					<span class="hint">
						{#if modelError}
							{modelError}
						{:else}
							Only models this API key can actually use are listed.
						{/if}
					</span>
				</label>

				<label for="lookback">
					Look back
					<select id="lookback" bind:value={draftLookback} disabled={busy}>
						{#each [1, 3, 7, 14, 30] as days (days)}
							<option value={days}>{days} day{days === 1 ? '' : 's'}</option>
						{/each}
					</select>
					<span class="hint">
						How far back a sync reads. One sync pulls up to 40 messages — press it
						again to keep walking backwards through the window.
					</span>
				</label>
			</div>
			<div class="row">
				<button onclick={saveSettings} disabled={busy}>Save</button>
				<button class="ghost" onclick={() => (showSettings = false)}>Close</button>
			</div>
		</div>
	{/if}

	{#if connected}
		<!-- Which mailboxes this screen is actually reading, and whether each
		     one still works. Without this, a mailbox whose token has been
		     revoked looks identical to a quiet week. -->
		<div class="mailboxes">
			<span class="label">Reading</span>
			{#each mailboxes as mailbox (mailbox.id)}
				<span class="mailbox" class:bad={mailbox.status !== 'ok'}>
					{mailbox.address}
					{#if mailbox.status !== 'ok'}
						<span title={mailbox.lastError}>· needs reconnecting</span>
					{/if}
					<button class="x" onclick={() => removeMailbox(mailbox)} title="Remove">×</button>
				</span>
			{/each}
			{#if status?.xero?.connected}
				<button class="mailbox xero" onclick={openSettings}>
					Xero: {selectedXeroName} · change
				</button>
			{:else}
				<span class="mailbox bad">Xero not connected — invoices cannot be checked</span>
			{/if}
		</div>
	{/if}

	{#if !connected}
		<div class="box">
			<h2>Connect the mailbox this reads</h2>
			<p class="muted">
				Read access to a Gmail account, plus permission to create drafts in it. It cannot
				send: replies land as drafts and a person presses send.
			</p>
			<p class="muted">
				A shared alias like <code>accounts@</code> is not a real account and cannot be
				connected directly — forward it into a mailbox you connect here, or add that mailbox
				to the Google Group.
			</p>
			<button onclick={connectMailbox}>Connect a mailbox</button>
		</div>
	{:else}
		<div class="split">
			<aside class="list">
				<div class="list-head">
					<span>{emails.length} waiting</span>
					<span class="muted">oldest first</span>
				</div>
				{#if emails.length === 0}
					<p class="empty">Nothing waiting. Press <em>Sync now</em> to check Gmail.</p>
				{:else}
					{#each emails as email (email.id)}
						<button
							class="card"
							class:selected={email.id === selectedId}
							onclick={() => select(email.id)}
						>
							<span class="card-top">
								<span class="from">{email.fromName || email.fromEmail}</span>
								<span class="age">{age(email.receivedAt)}</span>
							</span>
							<span class="subject">{email.subject || '(no subject)'}</span>
							<span class="chips">
								<span class="chip cat">{categoryLabel(email.category)}</span>
								{#if email.invoiceNumber}
									<span class="chip">{email.invoiceNumber}</span>
								{/if}
								{#if email.multiIntent}<span class="chip warn">2 requests</span>{/if}
								{#if email.threadCount > 1}
									<span class="chip">{email.threadCount} in thread</span>
								{/if}
								{#if email.state === 'drafted'}<span class="chip ok">drafted</span>{/if}
								{#if email.state === 'draft_failed'}
									<span class="chip bad">draft failed</span>
								{/if}
							</span>
						</button>
					{/each}
				{/if}
			</aside>

			<section class="pane">
				{#if loadingDetail}
					<p class="muted">Loading…</p>
				{:else if !detail}
					<p class="muted">Choose an email to review it.</p>
				{:else}
					<header class="pane-head">
						<div>
							<h2>{detail.email.subject || '(no subject)'}</h2>
							<p class="muted">
								{detail.email.fromName ? `${detail.email.fromName} · ` : ''}
								{detail.email.fromEmail} · {when(detail.email.receivedAt)}
							</p>
						</div>
						<button class="link" onclick={dismiss} disabled={busy}>Close without replying</button>
					</header>

					{#each detail.flags as flag (flag.code)}
						<p class="flag {flag.level}">{flag.message}</p>
					{/each}

					<div class="message">
						{detail.email.bodyStripped || detail.email.bodyText}
					</div>

					{#if detail.thread.length > 1}
						<button class="link" onclick={() => (showThread = !showThread)}>
							{showThread ? 'Hide' : 'Show'} the whole thread ({detail.thread.length} messages)
						</button>
						{#if showThread}
							<div class="thread">
								{#each detail.thread as item (item.id)}
									<article>
										<p class="muted">
											{item.fromEmail} · {when(item.receivedAt)}
										</p>
										<div class="message small">{item.bodyText}</div>
									</article>
								{/each}
							</div>
						{/if}
					{/if}

					{#if detail.senderHistory.length}
						<details class="history">
							<summary>Recent mail from this customer ({detail.senderHistory.length})</summary>
							<ul>
								{#each detail.senderHistory as item (item.id)}
									<li>
										<button class="link" onclick={() => select(item.id)}>
											{item.subject || '(no subject)'}
										</button>
										<span class="muted"> · {when(item.receivedAt)}</span>
									</li>
								{/each}
							</ul>
						</details>
					{/if}

					<div class="grid">
						<div class="box">
							<h3>Category</h3>
							<select
								value={detail.draft?.categoryKey ?? ''}
								onchange={changeCategory}
								disabled={busy || alreadyDrafted}
							>
								{#each detail.categories as category (category.key)}
									<option value={category.key}>{category.label}</option>
								{/each}
							</select>
							{#if detail.classification?.length}
								<p class="muted small">
									AI suggested <strong>{categoryLabel(detail.classification.at(-1).categoryKey)}</strong>
									at {Math.round((detail.classification.at(-1).confidence ?? 0) * 100)}%
									·
									{detail.classification.at(-1).modelVersion || 'no model'}
								</p>
								{#if detail.classification.length > 1}
									<p class="muted small">
										Changed by {detail.classification[0].createdBy ?? 'a person'}.
									</p>
								{/if}
							{/if}
						</div>

						<div class="box">
							<h3>Invoice</h3>
							{#if detail.lookup?.outcome === 'found'}
								<dl>
									<dt>Number</dt>
									<dd>{detail.lookup.invoiceNumber}</dd>
									<dt>Customer</dt>
									<dd>{detail.lookup.contactName || '—'}</dd>
									<dt>Total</dt>
									<dd>{detail.lookup.currency} {detail.lookup.amount}</dd>
									<dt>Outstanding</dt>
									<dd><strong>{detail.lookup.currency} {detail.lookup.outstandingBalance}</strong></dd>
									<dt>Due</dt>
									<dd>{detail.lookup.dueDate || '—'}</dd>
									<dt>Status</dt>
									<dd>{detail.lookup.invoiceStatus || '—'}</dd>
									<dt>For</dt>
									<dd>{detail.lookup.summary || detail.lookup.description || '—'}</dd>
								</dl>
								<p class="muted small">From Xero. These are the values in the draft.</p>
							{:else}
								<p class="muted">
									{detail.lookup?.error ?? 'No invoice record for this email.'}
								</p>
								{#if detail.lookup?.candidates?.length}
									<p class="small">Open invoices for this customer:</p>
									<ul class="small">
										{#each detail.lookup.candidates as candidate (candidate.invoiceId)}
											<li>
												{candidate.invoiceNumber} · {candidate.currency}
												{candidate.outstandingBalance} outstanding
											</li>
										{/each}
									</ul>
								{/if}
							{/if}

							{#if detail.extracted}
								<p class="muted small">
									The customer wrote: {detail.extracted.invoiceNumber ?? 'no number'}
									{#if detail.extracted.amount}, {detail.extracted.amount}{/if}
								</p>
							{/if}
						</div>
					</div>

					<div class="box draft">
						<div class="draft-head">
							<h3>Reply</h3>
							{#if !editing && !alreadyDrafted}
								<button class="link" onclick={startEditing}>Edit the wording</button>
							{/if}
						</div>

						{#if alreadyDrafted}
							<p class="banner ok">
								Drafted in Gmail{detail.reply.mailboxAddress
									? ` (${detail.reply.mailboxAddress})`
									: ''} at {when(detail.reply.completedAt)} by {detail.reply.actor}. Open
								Gmail to send it.
							</p>
						{/if}

						{#if detail.reply?.status === 'failed'}
							<p class="banner err">
								Gmail refused to create this draft after {detail.reply.attempts} attempts:
								{detail.reply.error}. Nothing was sent. Try again once Gmail is reachable.
							</p>
						{/if}

						{#if editing}
							<label for="subject">Subject</label>
							<input id="subject" bind:value={draftSubject} />
							<label for="body">Message</label>
							<textarea id="body" rows="14" bind:value={draftBody}></textarea>
							<div class="row">
								<button onclick={saveDraft} disabled={busy}>Save</button>
								<button class="ghost" onclick={() => (editing = false)}>Cancel</button>
							</div>
						{:else}
							<p class="subject-line">{currentSubject}</p>
							<pre class="body">{currentBody}</pre>
						{/if}

						{#if openSlots.length}
							<p class="banner warn">
								This draft still has {openSlots.map((s) => `{{${s}}}`).join(', ')} in it.
								Fill it from the invoice record, or edit the wording to remove it — a
								placeholder must never reach a customer.
							</p>
						{/if}

						<div class="row">
							<button onclick={createDraft} disabled={!canDraft || busy}>
								{busy ? 'Working…' : 'Create Gmail draft'}
							</button>
							<span class="muted small">
								Creates a draft in the customer's thread. You send it from Gmail.
							</span>
						</div>
					</div>
				{/if}
			</section>
		</div>
	{/if}
{/if}

<style>
	.head {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 24px;
		margin: 24px 0 16px;
		flex-wrap: wrap;
	}
	h1 {
		font-size: 1.4rem;
		margin: 0 0 4px;
	}
	h2 {
		font-size: 1.05rem;
		margin: 0 0 4px;
	}
	h3 {
		font-size: 0.78rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted);
		margin: 0 0 10px;
	}
	.lede {
		color: var(--muted);
		margin: 0;
		max-width: 60ch;
	}
	.actions {
		display: flex;
		gap: 8px;
		align-items: center;
	}
	.muted {
		color: var(--muted);
	}
	.small {
		font-size: 0.82rem;
	}
	.empty {
		color: var(--muted);
	}

	.stats {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
		gap: 12px;
		margin-bottom: 16px;
	}
	.stats div {
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 10px;
		padding: 12px 14px;
		display: grid;
		gap: 2px;
	}
	.stats strong {
		font-size: 1.35rem;
		font-variant-numeric: tabular-nums;
	}
	.stats span {
		font-size: 0.76rem;
		color: var(--muted);
	}

	.banner {
		border-radius: 8px;
		padding: 10px 14px;
		font-size: 0.88rem;
		margin: 0 0 12px;
		display: flex;
		justify-content: space-between;
		gap: 12px;
		align-items: flex-start;
	}
	.banner.warn {
		background: var(--warn-bg);
		color: var(--warn);
	}
	.banner.err {
		background: #fdecea;
		color: var(--danger);
	}
	.banner.ok {
		background: #e7f6ec;
		color: #116329;
	}
	.banner ul {
		margin: 6px 0 0;
		padding-left: 18px;
	}

	.mailboxes {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
		margin-bottom: 14px;
		font-size: 0.8rem;
	}
	.mailboxes .label {
		text-transform: uppercase;
		letter-spacing: 0.05em;
		font-size: 0.7rem;
		color: var(--muted);
	}
	.mailbox {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 999px;
		padding: 3px 6px 3px 12px;
		color: var(--muted);
	}
	.mailbox.xero {
		padding-right: 12px;
	}
	.mailbox.bad {
		border-color: var(--danger);
		color: var(--danger);
		padding-right: 12px;
	}
	.x {
		background: none;
		border: none;
		color: var(--muted);
		cursor: pointer;
		font-size: 1rem;
		line-height: 1;
		padding: 0 4px;
		border-radius: 999px;
	}
	.x:hover {
		color: var(--danger);
	}

	.settings h3 {
		margin-bottom: 12px;
	}
	.settings .fields {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
		gap: 16px;
	}
	.settings label {
		display: grid;
		gap: 4px;
		font-size: 0.82rem;
		font-weight: 600;
		margin: 0;
	}
	.settings .hint {
		font-weight: 400;
		font-size: 0.76rem;
		color: var(--muted);
		line-height: 1.4;
	}
	.reason {
		font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
		font-size: 0.78rem;
		opacity: 0.9;
	}

	.split {
		display: grid;
		grid-template-columns: minmax(260px, 340px) 1fr;
		gap: 16px;
		align-items: start;
	}
	@media (max-width: 860px) {
		.split {
			grid-template-columns: 1fr;
		}
	}

	.list {
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 10px;
		overflow: hidden;
		max-height: 78vh;
		overflow-y: auto;
	}
	.list-head {
		display: flex;
		justify-content: space-between;
		padding: 10px 14px;
		font-size: 0.76rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--muted);
		border-bottom: 1px solid var(--line);
	}
	.list .empty {
		padding: 16px 14px;
		font-size: 0.88rem;
	}
	.card {
		display: grid;
		gap: 4px;
		width: 100%;
		text-align: left;
		background: none;
		border: none;
		border-bottom: 1px solid var(--line);
		border-left: 3px solid transparent;
		padding: 12px 14px;
		cursor: pointer;
		font: inherit;
		color: inherit;
		border-radius: 0;
	}
	.card:hover {
		background: #f8f9fb;
	}
	.card.selected {
		border-left-color: var(--accent);
		background: #f4f8fd;
	}
	.card-top {
		display: flex;
		justify-content: space-between;
		gap: 8px;
		font-size: 0.85rem;
	}
	.from {
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.age {
		color: var(--muted);
		font-variant-numeric: tabular-nums;
		flex-shrink: 0;
	}
	.subject {
		font-size: 0.85rem;
		color: var(--muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.chips {
		display: flex;
		gap: 4px;
		flex-wrap: wrap;
		margin-top: 2px;
	}
	.chip {
		font-size: 0.68rem;
		padding: 1px 7px;
		border-radius: 999px;
		background: #eef1f5;
		color: var(--muted);
	}
	.chip.cat {
		background: #e7edf6;
		color: #24457a;
	}
	.chip.warn {
		background: var(--warn-bg);
		color: var(--warn);
	}
	.chip.ok {
		background: #e7f6ec;
		color: #116329;
	}
	.chip.bad {
		background: #fdecea;
		color: var(--danger);
	}

	.pane {
		display: grid;
		gap: 14px;
		align-content: start;
	}
	.pane-head {
		display: flex;
		justify-content: space-between;
		gap: 16px;
		align-items: flex-start;
	}
	.flag {
		margin: 0;
		font-size: 0.85rem;
		padding: 8px 12px;
		border-radius: 8px;
		border-left: 3px solid var(--muted);
		background: var(--card);
	}
	.flag.warn {
		border-left-color: var(--warn);
		background: var(--warn-bg);
		color: var(--warn);
	}
	.flag.info {
		border-left-color: var(--accent);
	}

	.box {
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 10px;
		padding: 16px;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
		gap: 14px;
	}
	.message {
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 10px;
		padding: 14px 16px;
		white-space: pre-wrap;
		font-size: 0.9rem;
		line-height: 1.55;
		max-height: 320px;
		overflow-y: auto;
	}
	.message.small {
		font-size: 0.82rem;
		max-height: 180px;
	}
	.thread {
		display: grid;
		gap: 10px;
	}
	.thread p {
		margin: 0 0 4px;
		font-size: 0.78rem;
	}
	.history ul {
		margin: 8px 0 0;
		padding-left: 18px;
		font-size: 0.85rem;
	}

	dl {
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 4px 12px;
		margin: 0;
		font-size: 0.86rem;
	}
	dt {
		color: var(--muted);
	}
	dd {
		margin: 0;
		font-variant-numeric: tabular-nums;
	}

	.draft-head {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
	}
	.subject-line {
		font-weight: 600;
		margin: 0 0 8px;
	}
	.body {
		white-space: pre-wrap;
		font: inherit;
		font-size: 0.9rem;
		line-height: 1.55;
		background: #f8f9fb;
		border: 1px solid var(--line);
		border-radius: 8px;
		padding: 14px 16px;
		margin: 0 0 12px;
		overflow-x: auto;
	}
	label {
		display: block;
		font-size: 0.8rem;
		font-weight: 600;
		margin: 8px 0 4px;
	}
	input,
	textarea,
	select {
		width: 100%;
		padding: 8px 10px;
		border: 1px solid var(--line);
		border-radius: 6px;
		font: inherit;
		font-size: 0.9rem;
		box-sizing: border-box;
	}
	textarea {
		line-height: 1.55;
		resize: vertical;
	}
	.row {
		display: flex;
		gap: 10px;
		align-items: center;
		margin-top: 12px;
		flex-wrap: wrap;
	}
	button {
		font: inherit;
		font-weight: 600;
		padding: 8px 16px;
		border: none;
		border-radius: 6px;
		background: var(--accent);
		color: #fff;
		cursor: pointer;
	}
	button:disabled {
		opacity: 0.5;
		cursor: default;
	}
	button.link {
		background: none;
		color: var(--accent);
		padding: 0;
		font-weight: 400;
		font-size: 0.85rem;
	}
	.ghost,
	button.ghost {
		background: none;
		border: 1px solid var(--line);
		color: var(--text);
		text-decoration: none;
		display: inline-block;
		padding: 8px 16px;
		border-radius: 6px;
		font-size: 0.9rem;
		font-weight: 600;
	}
	code {
		background: #eef1f5;
		padding: 1px 5px;
		border-radius: 4px;
		font-size: 0.85em;
	}
</style>
