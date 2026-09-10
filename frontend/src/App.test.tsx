import { render, screen, cleanup, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CaptionView, SecretField } from './App';
import { caption } from './test-fixtures';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
describe('caption accessibility and honest states', () => {
  it('renders incoming HTML as inert text', () => { const data = caption(); data.translation.text = '<img src=x onerror=alert(1)>'; render(<CaptionView caption={data} />); expect(screen.getByText(data.translation.text)).toBeVisible(); expect(document.querySelector('img')).toBeNull(); });
  it('retains source text for translation errors even with originals hidden', () => { const data = caption(); data.translation.status = 'error'; render(<CaptionView caption={data} showOriginal={false} />); expect(screen.getByText('翻訳できませんでした。原文を確認してください。')).toBeVisible(); expect(screen.getByText('你好')).toBeVisible(); expect(screen.queryByText('こんにちは')).not.toBeInTheDocument(); });
  it('labels untranslated text explicitly when translation is off', () => { const data = caption(); data.translation.status = 'disabled'; render(<CaptionView caption={data} />); expect(screen.getByText('原文表示')).toBeVisible(); });
});
describe('secret handling', () => {
  it('clears input before a failed request and never stores credentials', async () => { const storage = vi.spyOn(Storage.prototype, 'setItem'); const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 503, json: async () => ({ error: { message: '保存できませんでした' } }) }); vi.stubGlobal('fetch', fetchMock); render(<SecretField provider="gladia" label="Gladia" configured={false} />); const input = screen.getByLabelText('API キー'); await userEvent.type(input, 'test-value-only'); await userEvent.click(screen.getByRole('button', { name: 'キーを設定' })); expect(input).toHaveValue(''); await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('保存できませんでした')); expect(storage).not.toHaveBeenCalled(); storage.mockRestore(); });
});
