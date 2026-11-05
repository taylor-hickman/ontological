import { expect, test } from '@playwright/test'
import { clickMenuItem, freshUserData, launch } from './app'

const bounds = (app: Awaited<ReturnType<typeof launch>>['app']) =>
  app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.getBounds())

test('size, position and the open screen survive a relaunch', async () => {
  const userData = freshUserData()
  const first = await launch(userData)
  await first.app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.setBounds({ x: 120, y: 90, width: 1111, height: 777 }))
  await clickMenuItem(first.app, 'screen-jobs')
  await expect(first.page.locator('h1')).toHaveText('Jobs')
  const saved = await bounds(first.app)
  await first.app.close()

  const second = await launch(userData)
  try {
    await expect(second.page.locator('h1')).toHaveText('Jobs')
    const restored = await bounds(second.app)
    expect({ width: restored.width, height: restored.height }).toEqual({ width: saved.width, height: saved.height })
    expect({ x: restored.x, y: restored.y }).toEqual({ x: saved.x, y: saved.y })
  } finally {
    await second.app.close()
  }
})

test('the window never opens below 960×640', async () => {
  const { app } = await launch(freshUserData())
  try {
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.setSize(500, 400))
    const { width, height } = await bounds(app)
    expect(width).toBeGreaterThanOrEqual(960)
    expect(height).toBeGreaterThanOrEqual(640)
  } finally {
    await app.close()
  }
})
