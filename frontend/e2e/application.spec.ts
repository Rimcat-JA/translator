import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

async function openHost(page: Page) {
  const instance = JSON.parse(readFileSync(resolve('../.translator-test/frontend-e2e/instance.json'), 'utf8'));
  const response = await page.request.post(`${instance.origin}/api/local/instance`, { headers: { Origin: instance.origin, 'X-Translator-Instance': instance.secret }, data: { action: 'open' } });
  expect(response.ok()).toBeTruthy();
  await page.goto((await response.json()).url);
  await expect(page.getByRole('heading', { name: '通訳をはじめる' })).toBeVisible();
  await expect(page).not.toHaveURL(/bootstrap=/);
  const bootstrap = await (await page.request.get('/api/local/bootstrap')).json();
  if (bootstrap.session && bootstrap.session.status !== 'ended') {
    const stopped = await page.request.post(`/api/local/sessions/${bootstrap.session.session_id}/stop`, { headers: { Origin: instance.origin, 'X-CSRF-Token': bootstrap.csrf_token }, data: { request_id: crypto.randomUUID() } });
    expect(stopped.ok()).toBeTruthy();
  }
  await expect(page.getByRole('button', { name: '通訳を開始', exact: true })).toBeEnabled();
}

test('local demo, settings, responsive themes and reconnect snapshot', async ({ page }) => {
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  await openHost(page);
  await page.screenshot({ path: 'test-results/home-desktop.png', fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: 'デモを試す', exact: true }).click();
  await expect(page.getByText('デモ：実際の音声認識・翻訳ではありません。', { exact: false })).toBeVisible();
  await expect(page.locator('.translated-text').first()).toBeVisible();
  await expect(page.locator('.translated-text').first()).not.toHaveText('');
  await expect(page.locator('.caption-meta').last()).toContainText('確定');
  await page.screenshot({ path: 'test-results/captions-demo.png', fullPage: true, animations: 'disabled' });
  await page.context().setOffline(true);
  await page.context().setOffline(false);
  await page.getByRole('link', { name: '診断', exact: true }).click();
  await page.getByRole('button', { name: '画面の接続をやり直す' }).click();
  await expect(page.locator('.diagnostic-card').first()).toContainText('接続済み');
  await page.getByRole('link', { name: '設定', exact: true }).click();
  await expect(page.getByRole('heading', { name: '音声認識と翻訳', exact: true })).toBeVisible();
  await page.getByRole('combobox', { name: 'テーマ', exact: true }).selectOption('light');
  await page.getByRole('button', { name: '設定を保存', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: '設定を保存しました。' })).toBeVisible();
  await page.screenshot({ path: 'test-results/settings.png', fullPage: true, animations: 'disabled' });
  await page.setViewportSize({ width: 320, height: 800 });
  await page.getByRole('button', { name: 'テーマを切り替え' }).click();
  await page.screenshot({ path: 'test-results/settings-mobile-dark.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole('link', { name: 'ホーム', exact: true }).click();
  await page.getByRole('button', { name: '会話の画面に戻る' }).click();
  await page.getByRole('button', { name: '通訳を終了', exact: true }).click();
  expect(errors).toEqual([]);
});

test('independent B browser sends microphone PCM, receives A PCM, stops and leaves', async ({ page, browser }) => {
  await openHost(page);
  await page.getByRole('button', { name: '通訳を開始', exact: true }).click();
  await page.getByRole('button', { name: '招待リンクを作成' }).click();
  await expect(page.getByLabel('招待リンク', { exact: true })).toHaveValue(/^http:\/\/127\.0\.0\.1:18000\/join#invite=.+/);
  const invite = await page.getByLabel('招待リンク', { exact: true }).inputValue();
  expect(invite).toMatch(/^http:\/\/127\.0\.0\.1:18000\/join#invite=.+/);
  await page.getByRole('button', { name: '閉じる', exact: true }).click();
  const participant = await browser.newContext({ permissions: ['microphone'], viewport: { width: 1280, height: 1000 } });
  const speaker = await participant.newPage(); const errors: string[] = []; speaker.on('pageerror', error => errors.push(error.message));
  await speaker.goto(invite);
  await expect(speaker.getByRole('button', { name: '参加してマイクを開始' })).toBeEnabled();
  await expect(speaker).not.toHaveURL(/invite=/);
  await speaker.getByRole('button', { name: '参加してマイクを開始' }).click();
  await expect(speaker.getByRole('button', { name: 'マイクを停止', exact: true })).toBeVisible();
  await expect(page.locator('.translated-text').last()).toBeVisible();
  await page.getByRole('button', { name: '音声共有を開始', exact: true }).click();
  await expect(speaker.locator('.playback-controls')).toContainText('再生中');
  await expect(speaker.getByRole('heading', { name: 'あなたの声を届けています' })).toBeVisible();
  await expect(speaker.locator('.caption-meta').last()).toContainText('確定');
  await speaker.screenshot({ path: 'test-results/speaker-streaming.png', fullPage: true, animations: 'disabled' });
  await speaker.getByRole('button', { name: '一時ミュート', exact: true }).click();
  await expect(speaker.getByRole('button', { name: 'ミュートを解除' })).toBeVisible();
  await speaker.getByRole('button', { name: 'マイクを停止', exact: true }).click();
  await expect(speaker.getByRole('button', { name: '参加してマイクを開始' })).toBeVisible();
  await speaker.getByRole('button', { name: '会話から退出する' }).click();
  await expect(speaker.getByRole('heading', { name: '会話から退出しました' })).toBeVisible();
  await page.getByRole('button', { name: '通訳を終了', exact: true }).click();
  expect(errors).toEqual([]);
  await participant.close();
});

test('denied microphone keeps playback available and shows a useful explanation', async ({ page, browser }) => {
  await openHost(page);
  await page.getByRole('button', { name: '通訳を開始', exact: true }).click();
  await page.getByRole('button', { name: '招待リンクを作成' }).click();
  await expect(page.getByLabel('招待リンク', { exact: true })).toHaveValue(/^http:\/\/127\.0\.0\.1:18000\/join#invite=.+/);
  const invite = await page.getByLabel('招待リンク', { exact: true }).inputValue();
  await page.getByRole('button', { name: '閉じる', exact: true }).click();
  const participant = await browser.newContext(); const speaker = await participant.newPage();
  await speaker.addInitScript(() => { navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('Denied for fixture', 'NotAllowedError'); }; });
  await speaker.goto(invite); await speaker.getByRole('button', { name: '参加してマイクを開始' }).click();
  await expect(speaker.getByRole('alert')).toContainText('マイクが許可されていません');
  await expect(speaker.getByRole('button', { name: '音声受信を開始' })).toBeEnabled();
  await participant.close(); await page.getByRole('button', { name: '通訳を終了', exact: true }).click();
});
