<script>
	import { when } from '$lib/api.js';

	let { data } = $props();

	const statusLabel = {
		needs_approval: 'Awaiting approval',
		complete: 'Complete',
		empty: 'Nothing to pay',
		failed: 'Failed'
	};
</script>

<h1>Your workflows</h1>
<p class="lede">
	Every workflow your team has access to. No install, no local files — click one to run it.
</p>

<div class="grid">
	{#each data.workflows as wf (wf.key)}
		<a class="tile" href="/workflows/{wf.key}">
			<span class="icon" aria-hidden="true">£</span>
			<span class="name">{wf.name}</span>
			<span class="desc">{wf.description}</span>
			<span class="tags">
				{#each wf.integrations as integration}
					<span class="tag">{integration}</span>
				{/each}
				{#if wf.requiresApproval}
					<span class="tag approval">needs approval</span>
				{/if}
			</span>
		</a>
	{/each}
</div>

<h2>Recent runs</h2>
{#if data.runs.length === 0}
	<p class="empty">Nothing has been run yet.</p>
{:else}
	<table class="runs">
		<thead>
			<tr>
				<th>Run</th>
				<th>Started</th>
				<th>By</th>
				<th>Status</th>
				<th>Approved by</th>
			</tr>
		</thead>
		<tbody>
			{#each data.runs as run (run.id)}
				<tr>
					<td><a href="/runs/{run.id}">{run.workflow} · {run.params.region ?? ''}</a></td>
					<td>{when(run.createdAt)}</td>
					<td>{run.createdBy}</td>
					<td><span class="pill {run.status}">{statusLabel[run.status] ?? run.status}</span></td>
					<td>{run.approvedBy ?? '—'}</td>
				</tr>
			{/each}
		</tbody>
	</table>
{/if}

<style>
	h1 {
		font-size: 1.4rem;
		margin: 24px 0 4px;
	}
	h2 {
		font-size: 1.05rem;
		margin: 40px 0 12px;
	}
	.lede {
		color: var(--muted);
		margin: 0 0 24px;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
		gap: 16px;
	}
	.tile {
		display: grid;
		gap: 6px;
		padding: 18px;
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 10px;
		text-decoration: none;
		color: inherit;
	}
	.tile:hover {
		border-color: var(--accent);
	}
	.icon {
		width: 34px;
		height: 34px;
		display: grid;
		place-items: center;
		border-radius: 8px;
		background: var(--accent);
		color: #fff;
		font-weight: 700;
	}
	.name {
		font-weight: 600;
	}
	.desc {
		font-size: 0.85rem;
		color: var(--muted);
		line-height: 1.4;
	}
	.tags {
		display: flex;
		gap: 6px;
		margin-top: 4px;
		flex-wrap: wrap;
	}
	.tag {
		font-size: 0.7rem;
		padding: 2px 7px;
		border-radius: 999px;
		background: #eef1f5;
		color: var(--muted);
	}
	.tag.approval {
		background: var(--warn-bg);
		color: var(--warn);
	}
	.empty {
		color: var(--muted);
	}
	.runs {
		width: 100%;
		border-collapse: collapse;
		background: var(--card);
		border: 1px solid var(--line);
		border-radius: 10px;
		overflow: hidden;
		font-size: 0.88rem;
	}
	.runs th {
		text-align: left;
		font-weight: 600;
		color: var(--muted);
		font-size: 0.78rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}
	.runs th,
	.runs td {
		padding: 10px 14px;
		border-bottom: 1px solid var(--line);
	}
	.runs tr:last-child td {
		border-bottom: none;
	}
	.pill {
		font-size: 0.75rem;
		padding: 2px 8px;
		border-radius: 999px;
		background: #eef1f5;
	}
	.pill.needs_approval {
		background: var(--warn-bg);
		color: var(--warn);
	}
	.pill.complete {
		background: #e7f6ec;
		color: #116329;
	}
	.pill.failed {
		background: #fdecea;
		color: var(--danger);
	}
</style>
