// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// The GitHub Pages workflow supplies SITE and BASE_PATH from actions/configure-pages,
// and Actions sets GITHUB_REPOSITORY, so the site needs no hardcoded owner or repo.
const repository = process.env.GITHUB_REPOSITORY;
const repoUrl = repository ? `https://github.com/${repository}` : undefined;

export default defineConfig({
	site: process.env.SITE,
	base: process.env.BASE_PATH || '/',
	integrations: [
		starlight({
			title: 'rox',
			description: 'Disposable project workspaces in Linux containers and Tart VMs, wired into Rex.',
			social: repoUrl ? [{ icon: 'github', label: 'GitHub', href: repoUrl }] : [],
			editLink: repoUrl ? { baseUrl: `${repoUrl}/edit/main/docs/` } : undefined,
			lastUpdated: Boolean(repoUrl),
			sidebar: [
				{
					label: 'Getting started',
					items: [
						{ label: 'Installation', slug: 'getting-started/installation' },
						{ label: 'Quick start', slug: 'getting-started/quick-start' },
					],
				},
				{
					label: 'Guides',
					items: [
						{ label: 'Project setup hook', slug: 'guides/setup-hook' },
						{ label: 'Project settings', slug: 'guides/project-settings' },
						{ label: 'Project images', slug: 'guides/project-images' },
						{ label: 'Private repositories', slug: 'guides/private-repositories' },
						{ label: 'Tart VMs', slug: 'guides/tart-vms' },
						{ label: 'Rex remote hosts', slug: 'guides/remote-hosts' },
						{ label: 'Reaching guests from other devices', slug: 'guides/tailnet' },
						{ label: 'Keeping guests running', slug: 'guides/keep-running' },
						{ label: 'Orchestrating agents', slug: 'guides/agents' },
					],
				},
				{
					label: 'Reference',
					items: [
						{ label: 'CLI', slug: 'reference/cli' },
						{ label: 'Configuration', slug: 'reference/configuration' },
						{ label: 'Hook environment', slug: 'reference/hook-environment' },
						{ label: 'How it works', slug: 'reference/how-it-works' },
					],
				},
			],
		}),
	],
});
