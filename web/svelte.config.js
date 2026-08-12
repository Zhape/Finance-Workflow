import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
export default {
	preprocess: vitePreprocess(),
	kit: {
		// Every route is client-rendered (`ssr = false`) and every byte of data
		// comes from the worker, so there is nothing for a server to render.
		// Building as a static SPA means no Node runtime on Vercel at all —
		// which is also what removed the adapter-vercel Node-version failure.
		adapter: adapter({ fallback: 'index.html', strict: false })
	}
};
