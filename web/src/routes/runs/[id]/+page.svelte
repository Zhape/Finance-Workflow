<script>
	import { api, money, when } from '$lib/api.js';

	let { data } = $props();

	// The review gate. This is the tkinter ReviewDialog, except the URL can be
	// sent to whoever actually signs the payments off.
	let run = $state(data.run ?? { rows: [], columns: [], warnings: [], params: {}, status: '' });
	let selected = $state(new Set(data.run?.rows?.filter((r) => r.matched).map((r) => r.id) ?? []));
	let busy = $state(false);
	let error = $state('');
	let showLog = $state(false);

	const total = $derived(
		run.rows.filter((r) => selected.has(r.id)).reduce((sum, r) => sum + Number(r.amount ?? 0), 0)
	);
	const allSelected = $derived(run.rows.length > 0 && selected.size === run.rows.length);

	function toggle(id) {
		const next = new Set(selected);
		next.has(id) ? next.delete(id) : next.add(id);
		selected = next;
	}

	function toggleAll() {
		selected = allSelected ? new Set() : new Set(run.rows.map((r) => r.id));
	}

	async function approve() {
		busy = true;
		error = '';
		try {
			run = await api.approve(run.id, [...selected]);
		} catch (e) {
			error = e.message;
		}
		busy = false;
	}

	function cell(row, column) {
		const value = row[column.key];
		if (column.type === 'money') return money(value);
		if (column.type === 'flag') return value ? 'ok' : 'missing';
		return value ?? '';
	}
</script>

<p class="crumb"><a href="/">← All workflows</a></p>
<h1>{run.workflow} · {run.params.region ?? ''}</h1>
<p class="summary">{run.summary}</p>

{#if run.error}
	<div class="banner danger">
		<strong>Run failed.</strong>
		{run.error}
	</div>
{/if}

{#each run.warnings as warning}
	<div class="banner warn">{warning}</div>
{/each}

{#if run.status === 'complete'}
	<div class="banner ok">
		<div>
			Approved by <strong>{run.approvedBy}</strong> at {when(run.approvedAt)}.
		</div>
		<button class="download" onclick={() => api.downloadArtifact(run.id, run.artifactName)}>
			Download {run.artifactName}
		</button>
	</div>
	{#if run.workflow === 'weekly-payrun'}
		<p class="note">
			Upload this file to your bank as usual. This app cannot move money — it only prepares
			the file.
		</p>
	{:else}
		<p class="note">
			Nothing has been sent. Review each draft in Gmail and send it yourself.
		</p>
	{/if}
{/if}

{#if run.rows.length > 0}
	<div class="tablewrap">
		<table>
			<thead>
				<tr>
					<th class="tick">
						<input
							type="checkbox"
							checked={allSelected}
							onchange={toggleAll}
							disabled={run.status !== 'needs_approval'}
							aria-label="Select all"
						/>
					</th>
					{#each run.columns as column (column.key)}
						<th class:num={column.type === 'money'}>{column.label}</th>
					{/each}
				</tr>
			</thead>
			<tbody>
				{#each run.rows as row (row.id)}
					<tr class:excluded={!selected.has(row.id)}>
						<td class="tick">
							<input
								type="checkbox"
								checked={selected.has(row.id)}
								onchange={() => toggle(row.id)}
								disabled={run.status !== 'needs_approval'}
								aria-label="Include {row.name}"
							/>
						</td>
						{#each run.columns as column (column.key)}
							<td class:num={column.type === 'money'}>
								{#if column.type === 'flag'}
									<span class="flag" class:bad={!row[column.key]}>{cell(row, column)}</span>
								{:else}
									{cell(row, column)}
								{/if}
							</td>
						{/each}
					</tr>
				{/each}
			</tbody>
		</table>
	</div>

	{#if run.status === 'needs_approval'}
		{#if run.action?.hint}
			<p class="actionhint">{run.action.hint}</p>
		{/if}
		<div class="approve">
			<div class="count">
				<strong>{selected.size}</strong> of {run.rows.length} selected · total
				<strong>{money(total)}</strong>
			</div>
			<button onclick={approve} disabled={busy || selected.size === 0}>
				{busy ? 'Working…' : (run.action?.label ?? 'Approve')}
			</button>
		</div>
	{/if}
	{#if error}<p class="err">{error}</p>{/if}
{/if}

{#if run.log?.length}
	<button class="link" onclick={() => (showLog = !showLog)}>
		{showLog ? 'Hide' : 'Show'} run log ({run.log.length} lines)
	</button>
	{#if showLog}
		<pre>{run.log.join('\n')}</pre>
	{/if}
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
	.summary {
		color: var(--muted);
		margin: 0 0 20px;
	}
	.banner {
		padding: 11px 14px;
		border-radius: 8px;
		font-size: 0.87rem;
		margin-bottom: 12px;
		display: flex;
		justify-content: space-between;
		gap: 16px;
		align-items: center;
	}
	.banner.warn {
		background: var(--warn-bg);
		color: var(--warn);
	}
	.banner.danger {
		background: #fdecea;
		color: var(--danger);
	}
	.banner.ok {
		background: #e7f6ec;
		color: #116329;
	}
	button.download {
		background: none;
		color: #116329;
		font-weight: 600;
		white-space: nowrap;
		padding: 0;
		text-decoration: underline;
	}
	.tablewrap {
		overflow-x: auto;
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 10px;
	}
	table {
		border-collapse: collapse;
		width: 100%;
		font-size: 0.85rem;
	}
	th {
		text-align: left;
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--muted);
		white-space: nowrap;
	}
	th,
	td {
		padding: 9px 12px;
		border-bottom: 1px solid var(--line);
		white-space: nowrap;
	}
	tbody tr:last-child td {
		border-bottom: none;
	}
	.num {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}
	.tick {
		width: 34px;
	}
	tr.excluded td:not(.tick) {
		opacity: 0.4;
	}
	.flag {
		font-size: 0.72rem;
		padding: 2px 7px;
		border-radius: 999px;
		background: #e7f6ec;
		color: #116329;
	}
	.flag.bad {
		background: #fdecea;
		color: var(--danger);
	}
	.actionhint {
		font-size: 0.82rem;
		color: var(--muted);
		margin: 14px 0 0;
		line-height: 1.5;
	}
	.approve {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 20px;
		margin-top: 16px;
		flex-wrap: wrap;
	}
	.count {
		color: var(--muted);
		font-size: 0.88rem;
	}
	button {
		font: inherit;
		font-weight: 600;
		padding: 9px 20px;
		border: none;
		border-radius: 6px;
		background: var(--accent);
		color: #fff;
		cursor: pointer;
	}
	button:disabled {
		opacity: 0.55;
		cursor: default;
	}
	button.link {
		background: none;
		color: var(--accent);
		padding: 0;
		font-weight: 400;
		font-size: 0.82rem;
		margin-top: 24px;
	}
	pre {
		background: #14181d;
		color: #d6dde5;
		padding: 14px;
		border-radius: 8px;
		font-size: 0.78rem;
		overflow-x: auto;
		line-height: 1.5;
	}
	.err {
		color: var(--danger);
		font-size: 0.85rem;
	}
	.note {
		font-size: 0.8rem;
		color: var(--muted);
	}
</style>
