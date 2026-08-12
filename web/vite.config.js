import { sveltekit } from '@sveltejs/kit/vite';

const proxy = {
	'/api': {
		target: process.env.FW_WORKER_URL ?? 'http://127.0.0.1:8000',
		changeOrigin: true
	}
};

export default {
	plugins: [sveltekit()],
	// `vite preview` serves the real production build; give it the same proxy so
	// the built artefact can be checked against a live worker before deploying.
	preview: { port: 4173, proxy },
	server: {
		port: 5174,
		// The worker is a separate long-running service — it cannot live on
		// Vercel, so the web app always talks to it over HTTP. Proxying in dev
		// keeps that boundary identical to production, where PUBLIC_API_BASE
		// points the browser straight at the worker's host.
		proxy
	}
};
