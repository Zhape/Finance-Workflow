<script>
	import { onMount } from 'svelte';
	import { api, when } from '$lib/api.js';

	let data = $state(null);
	let title = $state('');
	let description = $state('');
	let busy = $state(false);
	let error = $state('');
	let outcome = $state(null);

	onMount(load);

	async function load() {
		try {
			data = await api.workflowRequests();
		} catch (e) {
			error = e.message;
		}
	}

	async function submit(event) {
		event.preventDefault();
		busy = true;
		error = '';
		outcome = null;
		try {
			outcome = await api.requestWorkflow(title.trim(), description.trim());
			if (outcome.status === 'pr_opened') {
				title = '';
				description = '';
			}
			await load();
		} catch (e) {
			error = e.message;
		}
		busy = false;
	}

	const STATUS = {
		pr_opened: 'Proposed',
		failed: 'Failed',
		submitted: 'Submitted'
	};
</script>

<p class="crumb"><a href="/">← All workflows</a></p>
<h1>Request a workflow</h1>
<p class="lede">
	Describe the manual process you want automated. Your request becomes a proposal for the
	platform's maintainers to build, review and release — nothing runs from your description
	directly, so be as specific as you like.
</p>

{#if data && !data.configured}
	<div class="banner warn">
		This deployment isn't connected to its code repository yet, so requests will be recorded
		but no proposal can be opened. An administrator needs to set
		<code>FW_GITHUB_TOKEN</code> on the worker.
	</div>
{/if}

<form onsubmit={submit}>
	<label for="title">What should it be called?</label>
	<input
		id="title"
		type="text"
		bind:value={title}
		maxlength="120"
		placeholder="e.g. Weekly supplier statement reconciliation"
		required
	/>

	<label for="description">Describe it like you'd brief a colleague</label>
	<textarea id="description" rows="10" bind:value={description} required
		placeholder="What system does it read from? What decides which rows matter? What does a person review before anything happens? What comes out the other end — a file, drafted emails, something else? Any amounts, day counts or edge cases that matter."
	></textarea>

	<button type="submit" disabled={busy || (data && !data.configured)}>
		{busy ? 'Preparing the proposal — this can take a minute or two…' : 'Submit request'}
	</button>
	{#if data?.generation}
		<p class="help">
			A first draft of the workflow will be generated from your description and attached to
			the proposal for human review. It never runs before a maintainer has read it, tested
			it against real data, and released it.
		</p>
	{:else}
		<p class="help">
			The request is filed for a maintainer to build. You'll see it appear on your dashboard
			once it has been reviewed, built and released to your organisation.
		</p>
	{/if}
</form>

{#if error}<div class="banner danger">{error}</div>{/if}
{#if outcome?.status === 'pr_opened'}
	<div class="banner ok">
		Proposal opened{outcome.kind === 'generated' ? ' with a generated draft' : ''}. It now
		goes through review, testing against real data, and release.
	</div>
{:else if outcome?.status === 'failed'}
	<div class="banner danger">The request was recorded, but the proposal could not be opened: {outcome.error}</div>
{/if}

{#if data?.requests?.length}
	<h2>Your organisation's requests</h2>
	<table>
		<thead>
			<tr><th>Request</th><th>By</th><th>When</th><th>Status</th></tr>
		</thead>
		<tbody>
			{#each data.requests as r (r.id)}
				<tr>
					<td>
						{r.title}
						{#if r.prUrl}
							· <a href={r.prUrl} target="_blank" rel="noreferrer noopener">proposal</a>
						{/if}
					</td>
					<td>{r.requestedBy}</td>
					<td>{when(r.createdAt)}</td>
					<td><span class="pill {r.status}">{STATUS[r.status] ?? r.status}</span></td>
				</tr>
			{/each}
		</tbody>
	</table>
{/if}

<style>
	.crumb {
		margin: 20px 0 0;
		font-size: 0.85rem;
	}
	.crumb a {
		text-decoration: none;
	}
	h1 {
		font-size: 1.35rem;
		margin: 12px 0 4px;
	}
	h2 {
		font-size: 1rem;
		margin: 32px 0 12px;
	}
	.lede {
		color: var(--muted);
		margin: 0 0 20px;
		max-width: 65ch;
		line-height: 1.5;
	}
	form {
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 10px;
		padding: 20px;
		max-width: 640px;
		display: grid;
		gap: 6px;
	}
	label {
		font-size: 0.84rem;
		font-weight: 600;
	}
	input,
	textarea {
		padding: 9px 10px;
		border: 1px solid var(--line);
		border-radius: 6px;
		font: inherit;
		font-size: 0.88rem;
		box-sizing: border-box;
		margin-bottom: 12px;
	}
	textarea {
		line-height: 1.5;
		resize: vertical;
	}
	button {
		font: inherit;
		font-weight: 600;
		padding: 9px 18px;
		border: none;
		border-radius: 6px;
		background: var(--accent);
		color: #fff;
		cursor: pointer;
		justify-self: start;
	}
	button:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.help {
		font-size: 0.8rem;
		color: var(--muted);
		margin: 10px 0 0;
		line-height: 1.5;
		max-width: 58ch;
	}
	.banner {
		padding: 11px 14px;
		border-radius: 8px;
		font-size: 0.87rem;
		margin: 14px 0;
		max-width: 640px;
		line-height: 1.5;
	}
	.banner.ok {
		background: #e7f6ec;
		color: #116329;
	}
	.banner.warn {
		background: var(--warn-bg);
		color: var(--warn);
	}
	.banner.danger {
		background: #fdecea;
		color: var(--danger);
	}
	table {
		width: 100%;
		border-collapse: collapse;
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 10px;
		overflow: hidden;
		font-size: 0.87rem;
	}
	th {
		text-align: left;
		font-size: 0.74rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--muted);
	}
	th,
	td {
		padding: 10px 14px;
		border-bottom: 1px solid var(--line);
	}
	tbody tr:last-child td {
		border-bottom: none;
	}
	.pill {
		font-size: 0.74rem;
		padding: 2px 8px;
		border-radius: 999px;
		background: #eef1f5;
	}
	.pill.pr_opened {
		background: #e7f6ec;
		color: #116329;
	}
	.pill.failed {
		background: #fdecea;
		color: var(--danger);
	}
	code {
		background: #eef1f5;
		padding: 1px 5px;
		border-radius: 4px;
		font-size: 0.85em;
	}
</style>
