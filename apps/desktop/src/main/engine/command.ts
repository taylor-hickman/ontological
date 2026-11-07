// Which command runs the engine. The packaged app's bundled engine runtime comes with M7.1 (§10);
// until then the app runs the engine from the repo's engine/ project through uv.

import { join, resolve } from 'node:path'

export interface CommandOptions {
  env: NodeJS.ProcessEnv
  isPackaged: boolean
  /** The app's folder, apps/desktop when it runs from the repo. */
  appPath: string
  /** Contents/Resources in the packaged app: what electron-builder.yml's extraResources copies. */
  resourcesPath: string
}

/**
 * The folders the packaged app bundles beside the engine (packaging/resources.sh): S4's Postgres
 * tree, the embedding model's pinned files and DuckDB's source extensions.
 */
export function bundledArgs(resourcesPath: string): string[] {
  return [
    '--pg-bin',
    join(resourcesPath, 'postgres', 'bin'),
    '--model-dir',
    join(resourcesPath, 'model'),
    '--duckdb-extensions',
    join(resourcesPath, 'duckdb-extensions')
  ]
}

/**
 * `ONTOLOGICAL_ENGINE_CMD`, a JSON array of strings, overrides the runtime: the end-to-end tests run
 * the engine with a fake store that way. The packaged app passes the engine its bundled folders,
 * after an override too; the supervisor appends `--app-data`.
 */
export function engineCommand({ env, isPackaged, appPath, resourcesPath }: CommandOptions): string[] {
  const runtime = engineRuntime(env, isPackaged, appPath)
  return isPackaged ? [...runtime, ...bundledArgs(resourcesPath)] : runtime
}

function engineRuntime(env: NodeJS.ProcessEnv, isPackaged: boolean, appPath: string): string[] {
  const override = env.ONTOLOGICAL_ENGINE_CMD
  if (override !== undefined) {
    let parsed: unknown
    try {
      parsed = JSON.parse(override)
    } catch {
      throw new Error('ONTOLOGICAL_ENGINE_CMD must be a JSON array of strings')
    }
    if (!Array.isArray(parsed) || parsed.length === 0 || !parsed.every((a) => typeof a === 'string')) {
      throw new Error('ONTOLOGICAL_ENGINE_CMD must be a JSON array of strings')
    }
    return parsed as string[]
  }
  if (isPackaged) throw new Error('This build has no engine runtime yet; it is bundled from M7.1')
  return ['uv', 'run', '--quiet', '--project', resolve(appPath, '../../engine'), 'ontological-engine']
}
