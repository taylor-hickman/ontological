import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test } from '@playwright/test'
import { clickMenuItem, freshUserData, launch } from './app'

// Settings → Compile against the stub engine, which keeps the settings it was given in
// <appData>/settings.json (engine/tests/engine/fake_settings.py).

function stored(userData: string): unknown {
  const path = join(userData, 'settings.json')
  return existsSync(path) ? JSON.parse(readFileSync(path, 'utf8')) : {}
}

test('Settings → Compile turns auto-compile on and off through the engine, off by default', async () => {
  const userData = freshUserData()
  const first = await launch(userData)
  try {
    await clickMenuItem(first.app, 'settings')
    const toggle = first.page.getByRole('group', { name: 'Compile' }).getByRole('switch', { name: 'Auto-compile new tables' })
    await expect(toggle).toBeEnabled()
    await expect(toggle).not.toBeChecked()
    expect(stored(userData)).toEqual({})

    await toggle.click()
    await expect(toggle).toBeChecked()
    await expect.poll(() => stored(userData)).toEqual({ 'compile.auto_compile': true })
  } finally {
    await first.app.close()
  }

  // A new launch reads it back from the engine
  const second = await launch(userData)
  try {
    await clickMenuItem(second.app, 'settings')
    const toggle = second.page.getByRole('group', { name: 'Compile' }).getByRole('switch', { name: 'Auto-compile new tables' })
    await expect(toggle).toBeChecked()

    await toggle.click()
    await expect(toggle).not.toBeChecked()
    await expect.poll(() => stored(userData)).toEqual({ 'compile.auto_compile': false })
  } finally {
    await second.app.close()
  }
})
