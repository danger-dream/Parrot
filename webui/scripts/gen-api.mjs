// Regenerates src/api/schema.d.ts from the management router's OpenAPI document.
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const webui = resolve(here, '..')
const repo = resolve(webui, '..')
const python =
  process.env.PARROT_PYTHON ||
  (existsSync(join(repo, 'venv/bin/python')) ? join(repo, 'venv/bin/python') : 'python3')

const dump = `
import json, sys
from fastapi import FastAPI
from src.management_api.router import create_management_router
app = FastAPI(title="Parrot Management API", version="v1")
app.include_router(create_management_router())
json.dump(app.openapi(), sys.stdout)
`
const spec = execFileSync(python, ['-c', dump], { cwd: repo, maxBuffer: 64 * 1024 * 1024 })
const dir = mkdtempSync(join(tmpdir(), 'parrot-openapi-'))
const specPath = join(dir, 'openapi.json')
writeFileSync(specPath, spec)
execFileSync(
  join(webui, 'node_modules/.bin/openapi-typescript'),
  [specPath, '-o', join(webui, 'src/api/schema.d.ts')],
  { stdio: 'inherit' },
)
