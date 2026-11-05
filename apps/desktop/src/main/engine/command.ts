// Which command runs the engine. The packaged app's bundled runtime comes with M7 (§10); until
// then the app runs the engine from the repo's engine/ project through uv.

import { resolve } from 'node:path'

export interface CommandOptions {
  env: NodeJS.ProcessEnv
  isPackaged: boolean
  /** The app's folder, apps/desktop when it runs from the repo. */
  appPath: string
}

/**
 * `ONTOLOGICAL_ENGINE_CMD`, a JSON array of strings, overrides it: the end-to-end tests run the
 * engine with a fake store that way. The supervisor appends `--app-data`.
 */
export function engineCommand({ env, isPackaged, appPath }: CommandOptions): string[] {
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
  if (isPackaged) throw new Error('This build has no engine runtime yet; it is bundled from M7')
  return ['uv', 'run', '--quiet', '--project', resolve(appPath, '../../engine'), 'ontological-engine']
}
