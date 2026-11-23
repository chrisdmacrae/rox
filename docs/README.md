# rox docs

The rox documentation site, built with [Starlight](https://starlight.astro.build). Pages live in `src/content/docs/`, and the sidebar is in `astro.config.mjs`.

```sh
npm install
npm run dev      # http://localhost:4321
npm run build    # outputs dist/
```

Pushing changes under `docs/` to `main` deploys the site with `.github/workflows/docs.yml`. In the repository's **Settings → Pages**, set **Source** to **GitHub Actions**. The workflow supplies the site URL and base path, so you don't need to edit them here.
