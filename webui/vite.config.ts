import { defineConfig, type Plugin } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createHash } from 'node:crypto'
import { mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { fileURLToPath, URL } from 'node:url'

const devTarget = process.env.PARROT_DEV_TARGET || 'http://127.0.0.1:22190'
const root = fileURLToPath(new URL('.', import.meta.url))
const outDir = fileURLToPath(new URL('../src/webui', import.meta.url))

// Must stay identical to src/tests/test_webui_bundle.py::source_digest.
const SOURCE_FILES = ['index.html', 'package.json', 'pnpm-lock.yaml', 'tsconfig.json', 'vite.config.ts']
const SOURCE_DIRS = ['src']

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name)
    return statSync(full).isDirectory() ? walk(full) : [full]
  })
}

function sourceDigest(): string {
  const files = [...SOURCE_FILES.map((f) => join(root, f)), ...SOURCE_DIRS.flatMap((d) => walk(join(root, d)))]
  const rel = files.map((f) => relative(root, f).split(sep).join('/')).sort()
  const hash = createHash('sha256')
  for (const r of rel) {
    hash.update(r)
    hash.update('\0')
    hash.update(readFileSync(join(root, r)))
    hash.update('\0')
  }
  return hash.digest('hex')
}

function buildMeta(): Plugin {
  let target = outDir
  return {
    name: 'parrot-build-meta',
    apply: 'build',
    configResolved(config) {
      target = config.build.outDir
    },
    writeBundle() {
      mkdirSync(target, { recursive: true })
      writeFileSync(join(target, 'build-meta.json'), JSON.stringify({ sourceDigest: sourceDigest() }, null, 2) + '\n')
    },
  }
}

export default defineConfig({
  base: '/ui/',
  plugins: [vue(), buildMeta()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir,
    emptyOutDir: true,
    assetsDir: 'assets',
    chunkSizeWarningLimit: 900,
    rolldownOptions: {
      output: {
        // echarts stays in the lazily loaded EChart chunk; a separate charts group produced a
        // circular import with the bundler runtime helper and failed at load time.
        codeSplitting: {
          groups: [{ name: 'vendor', test: /^(?!.*[\\/](echarts|zrender)[\\/]).*[\\/]node_modules[\\/]/ }],
        },
      },
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5190,
    proxy: {
      '/api': {
        target: devTarget,
        ws: true,
        changeOrigin: false,
        headers: process.env.PARROT_DEV_BEARER ? { Authorization: `Bearer ${process.env.PARROT_DEV_BEARER}` } : undefined,
      },
    },
  },
})
