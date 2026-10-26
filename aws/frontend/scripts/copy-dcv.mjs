import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const frontend = path.resolve(here, '..')
const sdk = path.resolve(
  frontend,
  'node_modules/bedrock-agentcore/dist/src/tools/browser/live-view/nice-dcv-web-client-sdk'
)
const dest = path.resolve(frontend, 'public/nice-dcv-web-client-sdk')

if (!fs.existsSync(sdk)) {
  console.error(`DCV SDK not found at ${sdk}. Run npm install first.`)
  process.exit(1)
}

fs.rmSync(dest, { recursive: true, force: true })
fs.mkdirSync(dest, { recursive: true })
fs.cpSync(path.join(sdk, 'dcvjs-esm'), path.join(dest, 'dcvjs-esm'), {
  recursive: true,
})
fs.cpSync(path.join(sdk, 'dcv-ui'), path.join(dest, 'dcv-ui'), {
  recursive: true,
})
console.log('Copied DCV web client SDK into public/nice-dcv-web-client-sdk')
