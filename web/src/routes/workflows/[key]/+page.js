import { error } from '@sveltejs/kit';
import { api } from '$lib/api.js';

export const ssr = false;

export async function load({ params, fetch }) {
	let workflows = [];
	try {
		({ workflows } = await api.workflows(fetch));
	} catch {
		return { workflow: null };
	}
	const workflow = workflows.find((w) => w.key === params.key);
	if (!workflow) throw error(404, `Unknown workflow ${params.key}`);
	return { workflow };
}
